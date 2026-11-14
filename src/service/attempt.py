r"""src/service/attempt.py — 1 件の取得を取り持つ ``_Attempt`` と、失敗時の履歴1行。

``download_scheduled()`` は ``run.lock`` を取得してから ``_download_scheduled_locked()``
に移譲する薄いラッパー。1 件単位の ``_download()`` は ``_Attempt`` を組み立て、
``save.save_report()`` と ``routes.fetch_report()`` をオーケストレートする。
``_Attempt`` は 1 件分の「開始時刻・履歴パス・スケジュールキー」をまとめ、
``record_*()`` で成功 / 失敗の履歴を書く。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import time
from collections.abc import Callable
from pathlib import Path

from comken.core.dates import now as clock_now
from comken.core.table.model import Table
from comken.core.timer import measure
from comken.exceptions import (
    ComkenError,
    CSVError,
    HistoryLockTimeoutError,
    HistoryWriteError,
)
from comken.services.salesforce_downloader.history import (
    HistoryRow,
)
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock

from src import paths
from src.exceptions import (
    ColumnNamesCSVFormatError,
    EmptyReportError,
    GroupNotRegisteredError,
    OverwriteTimeMissingError,
    ReportFolderNotFoundError,
    ReportNotRegisteredError,
    SavedFileEmptyError,
    ScheduledDownloadFailedError,
    SoqlColumnNameConflictError,
    SoqlCountMismatchError,
    SoqlIncompleteError,
    SoqlReportNotRegisteredError,
    SoqlTransformError,
)
from src.history import record, resolved_folder
from src.service._utils import history_path
from src.service.routes import (
    check_browser_filters,
    check_browser_run_filters,
    fetch_report,
    planned_route,
)
from src.service.save import require_folder, save_report
from src.service.scheduler import select_targets, warn_shared_reports
from src.sheets.master import ReportEntry, load_master
from src.sheets.schedule import ScheduleRule, load_schedule

logger = logging.getLogger("src.service")

# 履歴の「原因区分」列に出す5値。抽象クラス名ではなく誰が直すかで分ける
# （設計判断は docs/開発/仕様書.md 4.28 参照）
CAUSE_CONFIG = "設定"
CAUSE_SALESFORCE = "Salesforce"
CAUSE_EMPTY_DATA = "データなし"
CAUSE_FILE = "ファイル"
CAUSE_PROGRAM = "プログラム"

# 失敗の例外型 → (Salesforce取得結果, 保存結果, 原因区分) の対応表。
# **上から順に判定し、最初に一致した行を使う。**
# 「Salesforce取得結果」「保存結果」は ``True``（成功）/ ``False``（失敗）/
# ``None``（その段階まで到達しなかった）を表す。
_FAILURE_STAGES: tuple[
    tuple[tuple[type[BaseException], ...], bool | None, bool | None, str], ...
] = (
    # 取得段階に入らない設定の誤り（保存先フォルダ・グループ・SOQL の登録・
    # 未完成の印・上書き○の時刻）
    (
        (
            ReportFolderNotFoundError,
            GroupNotRegisteredError,
            SoqlReportNotRegisteredError,
            SoqlIncompleteError,
            OverwriteTimeMissingError,
        ),
        None,
        None,
        CAUSE_CONFIG,
    ),
    # 取得は終わったが、列名（見出し）に問題が起きた。取得自体は成功している
    ((SoqlColumnNameConflictError, ColumnNamesCSVFormatError), True, None, CAUSE_CONFIG),
    # 取得は終わったが、SOQL の加工で問題が起きた。取得自体は成功している
    ((SoqlCountMismatchError, SoqlTransformError), True, None, CAUSE_PROGRAM),
    # 取得は成功したが 0 件
    ((EmptyReportError,), True, None, CAUSE_EMPTY_DATA),
    # 取得も保存も終わったが、ファイルが 0 バイトだった
    ((SavedFileEmptyError,), True, False, CAUSE_FILE),
)


def _history_path() -> Path:
    """呼び出し時点の履歴CSVのパスを ``Path`` で返す。

    ``src/service/_utils.py`` の ``history_path()`` への薄いラッパー。
    テストが comken 側の ``HISTORY_PATH`` を差し替えるので、呼び出し時点で読む。
    """
    return history_path()


@measure
def download_scheduled(
    project: str = "定期実行",
    *,
    filters_by_report: dict[str, list[dict]] | None = None,
    fetch_browser: Callable[[ReportEntry], Table] | None = None,
) -> list[Path]:
    """管理表で有効なレポートをまとめて取得する。

    定期実行のプロジェクトから呼ぶ。**1件失敗しても残りは続ける**。戻り値は `list[Path]`
    のままで `CSV` を返さない（定期取得の呼び出し側は中身を読まないため）。

    ``filters_by_report`` は、管理番号ごとに Salesforce Report API の実行時フィルタを
    指定する。指定のない管理番号は、Salesforce に保存されているレポート条件のまま実行する。
    URL やレポート ID ではなく管理番号をキーにするため、管理表で参照先を差し替えても
    呼び出し側のコードは変えなくてよい。

    **API・SOQLのどれで取るかは、管理表の「2000件超」「SOQL」列で決まる**
    （呼び出し側は管理番号を意識しない）。SOQL経由のレポートは同じ管理番号の `SoqlReport` が
    `src.reports` に登録されている必要がある（`fetch_via_soql()` 参照）。

    **ブラウザ経由はこの本体では取らない。** 管理表の「2000件超」が `○` の行は
    別リポジトリ ``salesforce-report-browser`` の担当で、このリポジトリの
    通常の実行（``fetch_browser`` を渡さない）では **取らない**。1 実行につき
    1 行の INFO ログを出して対象から外す（履歴には書かない）。

    ``fetch_browser`` を渡すと、**対象選定で選ばれた行を全部** ブラウザで取る
    （経路が SOQL でも「2000件超」でも通常でも、1 件ずつ ``fetch_browser(entry)``
    を呼ぶ。履歴の「取得経路」は全部 ``ROUTE_BROWSER``）。渡された関数は 1 件だけ
    取りに行く責務を持つ（ブラウザの起動・ログイン・使い回しは
    ``salesforce-report-browser`` 側の責任）。

    **本体の後に実行する前提**の運用。ブラウザ側は本体の取り残しを全部拾う役で、
    本体で失敗した行（Report API の打ち切り・0 件・SOQL の失敗など）や、本体が
    飛ばした「2000件超」○ の行は、当日の成功履歴が無いので対象選定でそのまま
    選ばれ、ブラウザで取られる。

    **「スケジュール」シート**にこのレポートの行が無いレポートは取得しない。
    1 回の実行につき、取らなかった管理番号を並べた警告を 1 件出す（黙って取らなくなると
    気づけないため）。管理表で「有効」が `×` のレポートは対象外。

    **同時起動の扱い:** 二重取得を防ぐため、この関数全体（対象選定〜履歴追記）を
    `HistoryFileLock` でプロセス間排他する。ロックが取れない場合は**例外にせず**ログだけ
    出して空リスト ``[]`` を返す（別の実行が今回の分も担当し、失敗扱いにすると誤報になる）。

    **実行日時の扱い:** 日付をまたぐ実行でも判定・出力ファイル名・履歴の「実行日時」が
    揃うよう、関数冒頭で 1 回取った `clock_now()` を全工程で共有する。

    Args:
        project: 履歴の「プロジェクト」列へ残す呼び出し元の名前。
        filters_by_report: ``{管理番号: [Report APIフィルタ, ...]}``。各フィルタは
            ``{"column": ..., "operator": ..., "value": ...}`` の形で指定する。
            ブラウザ経由・SOQL経由のレポートには指定できない（``ValueError``）。
            ``fetch_browser`` を渡す実行にも指定できない（``ValueError``）。
        fetch_browser: ブラウザ経由で 1 件取得する関数。渡すと、対象選定で選ばれた
            行を **全部** ブラウザで取る（経路は問わない）。``None``（既定）なら
            管理表の「2000件超」が `○` の行は取らない。

    Returns:
        保存したファイルのパス一覧。1件も取得対象が無いとき・別の実行が
        進行中でロックが取れなかったときは空リスト ``[]``。

    Raises:
        ReportNotRegisteredError: ``filters_by_report`` に管理表未登録の管理番号がある場合。
        SoqlReportNotRegisteredError: 管理表の「SOQL」列が「○」なのに、同じ管理番号の
            `SoqlReport` が登録されていない場合。
        ValueError: ``fetch_browser`` と ``filters_by_report`` を同時に指定した場合。
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。**取得できたものは
            保存したうえで**送出する。
    """
    # 実行時フィルタはブラウザ経由では使えない。**接続する前に**落とす
    # （Salesforce へ問い合わせる前に気づくようにするため）
    check_browser_run_filters(filters_by_report, fetch_browser)
    # 履歴CSVとは別のロックファイル（``{history_path}.run.lock``）で、対象選定〜履歴追記を
    # プロセス間排他する。取れなければ別プロセスが進行中なので ``[]`` を返す。
    # ロック取得**だけ**を try/except で囲う: 本体の ``HistoryLockTimeoutError`` は
    # 履歴読み書きの障害なので、``[]`` で握りつぶさず伝播させる。
    hp = _history_path()
    run_lock_path = Path(f"{hp}.run")
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(HistoryFileLock(run_lock_path, timeout=0))
        except HistoryLockTimeoutError:
            # 進行中の実行が今回の分も担当する。失敗扱いにすると誤報になるので正常終了
            logger.warning("別の実行が進行中のため今回はスキップします: %s", run_lock_path)
            return []
        return _download_scheduled_locked(
            project,
            filters_by_report=filters_by_report,
            fetch_browser=fetch_browser,
        )


def _validate_filters_by_report(
    filters_by_report: dict[str, list[dict]], entries: dict[str, ReportEntry]
) -> None:
    """実行時フィルタの管理番号がすべて管理表に存在することを確認する。"""
    for report_key in filters_by_report:
        if report_key not in entries:
            raise ReportNotRegisteredError(report_key, list(entries), paths.MASTER_PATH)


def _download_scheduled_locked(
    project: str,
    *,
    filters_by_report: dict[str, list[dict]] | None,
    fetch_browser: Callable[[ReportEntry], Table] | None,
) -> list[Path]:
    """``download_scheduled()`` のロック取得後に動く本体。

    薄いラッパー ``download_scheduled()`` から切り出した本体。流れは「入力準備 →
    対象選定とログ → 経路別の対象分け → 1 件ずつ実行 → 終了ログ / 失敗通知」。
    """
    filters_by_report = filters_by_report or {}
    entries, rules_by_report, current = _prepare_run_inputs()
    _validate_filters_by_report(filters_by_report, entries)
    warn_shared_reports(entries)

    run_started = time.perf_counter()
    targets, skipped = _select_and_log_targets(entries, rules_by_report, current)
    # 1 件ごとの理由は DEBUG（障害調査時に ``--log-level=DEBUG`` で見る）
    _log_skipped_details(skipped)
    # ブラウザ経由の行と、それ以外の行を分ける。どちらを取るかは
    # ``fetch_browser`` の有無だけで決まる
    targets = _split_browser_targets(targets, filters_by_report, fetch_browser)

    saved, failed, last_exception = _execute_each_target(
        targets,
        project,
        filters_by_report,
        current,
        fetch_browser,
    )

    run_seconds = time.perf_counter() - run_started
    return _report_run_results(targets, saved, failed, last_exception, run_seconds)


def _prepare_run_inputs() -> tuple[
    dict[str, ReportEntry], dict[str, list[ScheduleRule]], dt.datetime
]:
    """管理表 / スケジュール表を読み、 この実行の基準日時を 1 か所で固定する。

    スケジュール判定・ファイル名・履歴の「実行日時」が同じ ``current`` を見ることで、
    日付をまたぐ実行でも開始日で揃う。

    スケジュールは **有効行だけ** を索引化する。シートが無い場合は空リストを返す。
    """
    entries = load_master(paths.MASTER_PATH)
    schedule_rules = load_schedule(paths.MASTER_PATH)
    rules_by_report: dict[str, list[ScheduleRule]] = {}
    for rule in schedule_rules:
        if rule.enabled:
            rules_by_report.setdefault(rule.report_key, []).append(rule)
    current = clock_now()
    return entries, rules_by_report, current


def _select_and_log_targets(
    entries: dict[str, ReportEntry],
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
) -> tuple[list, dict[str, list[str]]]:
    """対象レポートを選定し、 内訳をログにまとめて返す。

    ``select_targets()`` の戻り値 ``_already_failed`` は常に空なので使わない。
    """
    targets, _already_failed, skipped = select_targets(
        entries, rules_by_report, current, _history_path()
    )
    enabled_count = sum(1 for entry in entries.values() if entry.enabled)
    logger.info(
        "定期取得を開始します: 管理表=%s, 全 %d 件 / 有効 %d 件 / 対象 %d 件",
        paths.MASTER_PATH,
        len(entries),
        enabled_count,
        len(targets),
    )
    logger.info(
        "対象から外した内訳: 有効× %d 件 / スケジュール行なし %d 件 / 予定外 %d 件 / "
        "当日取得済 %d 件",
        len(skipped["有効×"]),
        len(skipped["スケジュール行なし"]),
        len(skipped["予定外"]),
        len(skipped["当日取得済"]),
    )
    return targets, skipped


def _log_skipped_details(skipped: dict[str, list[str]]) -> None:
    """対象外になった理由を DEBUG で 1 件ずつ出す。

    既定では INFO だけ。障害調査時に ``--log-level=DEBUG`` で「なぜ対象外か」を追える。
    """
    for key in skipped["有効×"]:
        logger.debug("対象外: 管理番号=%s, 理由=有効×", key)
    for key in skipped["スケジュール行なし"]:
        logger.debug("対象外: 管理番号=%s, 理由=スケジュール行なし", key)
    for key in skipped["予定外"]:
        logger.debug("対象外: 管理番号=%s, 理由=予定外", key)
    for key in skipped["当日取得済"]:
        logger.debug("対象外: 管理番号=%s, 理由=当日取得済", key)


def _is_browser_target(entry: ReportEntry) -> bool:
    """この行が「ブラウザ経由」の行かどうか。

    管理表の「2000件超」が `○`、かつ「SOQL」が `×` の行がブラウザ経由。
    「SOQL」が `○` の行は SOQL 経由が優先されるので、ブラウザの行ではない。
    """
    return entry.exceeds_row_limit and not entry.use_soql


def _split_browser_targets(
    targets: list,
    filters_by_report: dict[str, list[dict]],
    fetch_browser: Callable[[ReportEntry], Table] | None,
) -> list:
    """ブラウザ経由の行と、それ以外の行に分け、**この実行で取る方だけ**を返す。

    ``fetch_browser`` が ``None``（本体の通常の実行）なら、ブラウザ経由の行は
    **取らない**。取得は別リポジトリ ``salesforce-report-browser`` の担当なので、
    1 実行につき 1 行の INFO ログを出して対象から外す（履歴には書かない）。

    ``fetch_browser`` が渡されている（別リポジトリからの実行）なら、**選ばれた
    targets を全部** 返す。経路（SOQL / 2000件超 / 通常）に関係なくブラウザで
    取るので、ブラウザ経由以外の行を外す処理はしない。当日の成功履歴がある行は
    対象選定の段階で選ばれているので、ここには現れない（取り残しだけを取る）。

    実行時フィルタを ``fetch_browser`` と併用した場合は ``download_scheduled()``
    が **接続前** に ``ValueError`` で止めている（フィルタは Report API 専用）。

    Raises:
        ValueError: ブラウザ経由の行に実行時フィルタが指定されているとき。
    """
    if fetch_browser is not None:
        # 対象選定で選ばれた行は全部ブラウザで取る（取り残しを全部拾う役）
        return targets
    browser_targets = [target for target in targets if _is_browser_target(target[0])]
    other_targets = [target for target in targets if not _is_browser_target(target[0])]
    for entry, _schedule_key, _rule in browser_targets:
        check_browser_filters(entry, filters_by_report.get(entry.key))
    if browser_targets:
        logger.info(
            "ブラウザ経由の行は salesforce-report-browser で取るため飛ばします: %s",
            "、".join(entry.key for entry, _key, _rule in browser_targets),
        )
    return other_targets


def _execute_each_target(
    targets: list,
    project: str,
    filters_by_report: dict[str, list[dict]],
    current: dt.datetime,
    fetch_browser: Callable[[ReportEntry], Table] | None,
) -> tuple[list[Path], list[str], BaseException | None]:
    """1 件ずつ ``_download()`` を呼び、 成功 / 失敗を集計する。

    失敗時に ``ScheduledDownloadFailedError`` から ``__cause__`` で辿れるよう、最後に捕捉した
    例外だけを覚えておく。
    """
    saved: list[Path] = []
    failed: list[str] = []
    last_exception: BaseException | None = None
    total_targets = len(targets)
    for index, (entry, schedule_key, schedule_rule) in enumerate(targets, start=1):
        try:
            saved.append(
                _download(
                    entry,
                    project,
                    _history_path(),
                    schedule_key,
                    schedule_run_time=(
                        schedule_rule.desired_time if schedule_rule is not None else None
                    ),
                    schedule_start_time=(
                        schedule_rule.start_time if schedule_rule is not None else None
                    ),
                    filters=filters_by_report.get(entry.key),
                    current=current,
                    fetch_browser=fetch_browser,
                    progress=(index, total_targets),
                )
            )
        except (ComkenError, OSError) as e:
            # **想定した失敗は続ける。想定していない失敗は止める。**
            # - `ComkenError` は `docs/ERRORS.md` に対処法が載っている想定内の失敗
            # - `OSError` は共有サーバー断・権限・パスなど運用上の失敗（保存先はレポートごとに違う）
            # - それ以外（`TypeError` などのバグ）は捕捉せず、その場で落として気づかせる
            #   （`ScheduledDownloadFailedError` の顔で出ると「もう一度実行」を繰り返すだけになる）
            logger.error("取得に失敗しました: %s（%s）", entry.key, e)
            failed.append(entry.key)
            last_exception = e
    return saved, failed, last_exception


def _report_run_results(
    targets: list,
    saved: list[Path],
    failed: list[str],
    last_exception: BaseException | None,
    run_seconds: float,
) -> list[Path]:
    """終了ログを出したうえで、 失敗があれば ``ScheduledDownloadFailedError`` を送出する。

    1 件でも失敗した場合は、続けたうえで例外を上げる（RPA 基盤から失敗と分かるよう
    終了コード 1 にする）。直近の失敗を ``__cause__`` に乗せる。
    """
    logger.info(
        "定期取得: %d 件中 %d 件を取得しました。（%.1f 秒）",
        len(targets),
        len(saved),
        run_seconds,
    )
    if failed:
        raise ScheduledDownloadFailedError(failed, _history_path()) from last_exception
    return saved


class _Attempt:
    """1件の取得（フォルダ確認 → 取得 → 保存）を取り持つ文脈。

    履歴を書く関数に毎回同じ値の組を渡す煩雑さを消すための箱。
    開始時刻はコンストラクタで `time.perf_counter()` に記録する。`record_*()` は履歴と
    ログを書くだけで、失敗の段階と原因区分は `_failure_row()` が例外の型だけから決める。

    ``schedule_key`` は ``_matched_schedule_key()`` の戻り値の第2要素をそのまま受け取り、
    ``HistoryRow.schedule_key`` に詰めて履歴へ書く。
    """

    def __init__(
        self,
        entry: ReportEntry,
        project: str,
        history_path: Path,
        schedule_key: str = "",
        *,
        executed_at: dt.datetime,
    ) -> None:
        self._entry = entry
        self._project = project
        self._history_path = history_path
        self._schedule_key = schedule_key
        # ``_download_scheduled_locked()`` で固定した実行開始時刻（日付をまたいでも履歴が揃う）
        self._executed_at = executed_at
        self._started = time.perf_counter()

    def record_failure(self, exc: BaseException, *, route: str = "") -> None:
        """失敗時の履歴とログ。"""
        row = _failure_row(
            exc,
            time.perf_counter() - self._started,
            schedule_key=self._schedule_key,
            route=route,
        )
        # 値を計算した場所でログにも書く
        logger.error("取得に失敗しました: %s（%s / 区分=%s）", self._entry.key, exc, row.cause)
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=self._executed_at,
            # ファイル未保存のケース。``resolved_folder()`` は
            # ``GroupNotRegisteredError`` を文字列化する（履歴記録中の失敗で元の原因を失わないため）
            folder=resolved_folder(self._entry),
        )

    def record_success(
        self,
        path: Path,
        rows: Table,
        *,
        route: str = "",
    ) -> None:
        """成功時の履歴とログ。"""
        seconds = time.perf_counter() - self._started
        row = HistoryRow(
            succeeded=True,
            fetched_from_salesforce=True,
            saved_to_file=True,
            file_name=path.name,
            row_count=len(rows),
            seconds=seconds,
            schedule_key=self._schedule_key,
            route=route,
        )
        if rows:
            logger.info("取得しました: %s（%d 行 / %.1f 秒）", path, len(rows), seconds)
        else:
            logger.info("取得しました: %s（0 件 / 0件ありのため正常）", path)
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=self._executed_at,
            # 履歴の「保存先」は実際にファイルを置いたフォルダ（``path.parent``）。
            # 渡さないと概要の決定ロジックが履歴側に漏れる
            folder=path.parent,
        )


def _log_fetch_start(entry: ReportEntry, route: str, progress: tuple[int, int] | None) -> None:
    """1 件の取得開始を INFO で 1 行出す。

    ``_download()`` の冒頭（Salesforce へ問い合わせる前）に呼び、 ``[i/n]`` の進み具合と
    管理番号・概要・**予定していた経路**を 1 行で残す。取得中に固まったとき
    「どのレポートで止まったか」を分かるようにするのが目的。
    """
    if progress is not None:
        i, n = progress
        logger.info(
            "[%d/%d] 取得開始: 管理番号=%s, 概要=%s, 経路=%s",
            i,
            n,
            entry.key,
            entry.summary,
            route,
        )
    else:
        # 単体テストや直接 ``_download()`` を呼ぶ経路向け
        logger.info(
            "取得開始: 管理番号=%s, 概要=%s, 経路=%s",
            entry.key,
            entry.summary,
            route,
        )


def _download(
    entry: ReportEntry,
    project: str,
    history_path: Path,
    schedule_key: str = "",
    *,
    schedule_run_time: dt.time | None = None,
    schedule_start_time: dt.time | None = None,
    filters: list[dict] | None = None,
    current: dt.datetime,
    fetch_browser: Callable[[ReportEntry], Table] | None = None,
    progress: tuple[int, int] | None = None,
) -> Path:
    """1件を取得して保存し、成否を履歴に残す。

    ``schedule_run_time`` はスケジュール行の「取得時刻」（記録用の希望時刻）、
    ``schedule_start_time`` は「取得開始時刻」。出力ファイル名に埋め込む時刻は
    ``_resolve_run_time()`` が決める（``save_report()`` まで運ぶ）。

    ``current`` は ``_download_scheduled_locked()`` 冒頭で固定した ``clock_now()``。
    ``save_report()`` 経由でファイル名に、``_Attempt`` 経由で履歴の「実行日時」に渡す。

    ``fetch_browser`` はブラウザ経由で 1 件取得する関数（=別リポジトリ
    ``salesforce-report-browser`` が渡すもの）。渡されているときは **経路がどう
    であれ** その関数で取る（履歴の「取得経路」も ``ROUTE_BROWSER``）。

    **取得の途中で取り直しはしない。** Report API が 2000 件超で打ち切られた
    ときは ``SalesforceReportTruncatedError`` がそのまま失敗になる（管理表の
    「SOQL」か「2000件超」を `○` にして対処する）。0 行 + ``allow_empty=False``
    のときも ``EmptyReportError`` がそのまま失敗になる。
    """
    attempt = _Attempt(entry, project, history_path, schedule_key, executed_at=current)
    # 経路は取得前に決まる（取り直しが無いので、予定経路＝実際経路）。
    # 失敗時の履歴にもこの値を書く。``fetch_browser`` を渡した実行では
    # 経路に関係なくブラウザになる
    route = planned_route(entry, fetch_browser=fetch_browser)
    try:
        # 「取得 → 保存」を順に呼ぶ。取得と保存のどちらで失敗しても、
        # 下の except で 1 か所に履歴に残す
        _log_fetch_start(entry, route, progress)
        # ファイル名に使う時刻を決める。「上書き」○ で時刻が両方とも空欄なら
        # ここで止める（Salesforce へは問い合わせない）
        run_time = _resolve_run_time(entry, schedule_run_time, schedule_start_time, schedule_key)
        require_folder(entry)
        route, table = fetch_report(entry, filters, fetch_browser=fetch_browser)
        path = save_report(entry, table, schedule_run_time=run_time, current=current)
    except Exception as exc:
        _record_failure_safely(attempt, exc, route, history_path)
        raise
    attempt.record_success(path, table, route=route)
    return path


def _resolve_run_time(
    entry: ReportEntry,
    desired_time: dt.time | None,
    start_time: dt.time | None,
    schedule_key: str,
) -> dt.time | None:
    """出力ファイル名に埋め込む時刻（``HHMM``）を決める。

    - **「上書き」×**: スケジュールの「取得時刻」（記録用の希望時刻）をそのまま
      使う。空欄なら ``None`` を返し、``output_path()`` 側で実行時刻に
      フォールバックさせる（従来の動き）
    - **「上書き」○**: 「取得時刻」が空欄なら「取得開始時刻」を使う。両方が
      空欄だとファイル名を決められない（実行時刻で毎回別のファイルができる）ため、
      ``OverwriteTimeMissingError`` で取得前に止める

    Raises:
        OverwriteTimeMissingError: 「上書き」○ で「取得時刻」も「取得開始時刻」も
            空欄のとき。
    """
    if not entry.overwrite:
        return desired_time
    if desired_time is not None:
        return desired_time
    if start_time is not None:
        return start_time
    raise OverwriteTimeMissingError(entry.key, schedule_key)


def _record_failure_safely(
    attempt: _Attempt,
    exc: BaseException,
    route: str,
    history_path: Path,
) -> None:
    """``record_failure()`` 時の履歴エラーを ``HistoryWriteError`` に変換する。

    履歴書き込み中の失敗は、元の原因（``exc``）を ``original`` に持たせて
    1 件の失敗として記録されるようにする。
    """
    try:
        attempt.record_failure(exc, route=route)
    except (
        HistoryWriteError,
        HistoryLockTimeoutError,
        CSVError,
    ) as history_exc:
        raise HistoryWriteError(history_path, str(history_exc), original=exc) from exc


def _failure_row(
    exc: BaseException,
    seconds: float,
    schedule_key: str = "",
    *,
    route: str = "",
) -> HistoryRow:
    """失敗時の履歴1行を、**例外の型だけから**組み立てる。

    `fetched` / `saved` は `_download()` の何処で失敗したかを例外で判別する。
    判定は ``_FAILURE_STAGES`` の対応表（**上から順に判定する**）と、
    表に無い例外の次の3つで決める。

    1. 表の1行目: 取得段階の前（保存先フォルダ・グループ・SOQL の登録・
       未完成の印・上書き○の時刻）→ 取得も保存も未到達・設定
    2. 表の2行目: 取得は終わったが列名に問題が起きた → 取得成功・保存未到達・設定
    3. 表の3行目: 取得は終わったが SOQL の加工で問題が起きた
       → 取得成功・保存未到達・プログラム
    4. 表の4行目: 取得は成功したが 0 件 → 取得成功・保存未到達・データなし
    5. 表の5行目: 保存は終わったがファイルが 0 バイト
       → 取得成功・保存失敗・ファイル
    6. `OSError` → 保存段階（書き込み・権限・共有サーバー断）
       → 取得成功・保存失敗・ファイル
    7. その他の `ComkenError` → 取得段階（Salesforce 通信と認証）
       → 取得失敗・保存未到達・Salesforce
    8. それ以外 → 取得失敗・保存未到達・プログラム（想定外）

    7. は `download_scheduled()` が捕捉する範囲と一致させている。

    `schedule_key` は履歴の「スケジュールキー」列へ書く（`record_success()` と対称）。

    `route` は取得経路（``ROUTE_API`` / ``ROUTE_SOQL`` / ``ROUTE_BROWSER``）。
    例外の型から判別できないので、``_download()`` が確定した値をキーワード引数で受ける。
    """
    fetched, saved, cause = _failure_stage(exc)
    return HistoryRow(
        succeeded=False,
        fetched_from_salesforce=fetched,
        saved_to_file=saved,
        seconds=seconds,
        cause=cause,
        error_code=type(exc).__name__,
        error=str(exc),
        schedule_key=schedule_key,
        route=route,
    )


def _failure_stage(exc: BaseException) -> tuple[bool | None, bool | None, str]:
    """例外から ``(Salesforce取得結果, 保存結果, 原因区分)`` を求める。

    ``_FAILURE_STAGES`` の対応表を上から順に当たり、最初に一致した行の
    値を返す。表に無い例外は、``OSError`` → ファイル、その他の ``ComkenError``
    → Salesforce、それ以外 → プログラムで決める。

    Returns:
        ``(Salesforce取得結果, 保存結果, 原因区分)``。取得結果・保存結果は
        ``True``（成功）/ ``False``（失敗）/ ``None``（未到達）。
    """
    for exception_types, fetched, saved, cause in _FAILURE_STAGES:
        if isinstance(exc, exception_types):
            return fetched, saved, cause
    if isinstance(exc, OSError):
        return True, False, CAUSE_FILE
    if isinstance(exc, ComkenError):
        return False, None, CAUSE_SALESFORCE
    return False, None, CAUSE_PROGRAM

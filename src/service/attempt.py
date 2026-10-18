r"""src/service/attempt.py — 1 件の取得を取り持つ ``_Attempt`` と、失敗時の履歴1行。

``download_scheduled()`` は ``run.lock`` を取得してから ``_download_scheduled_locked()``
に移譲する薄いラッパー。1 件単位の ``_download()`` は ``_Attempt`` を組み立て、
``save.save_report()`` と ``routes.fetch_with_auto_fallback()`` をオーケストレートする。
``_Attempt`` は 1 件分の「開始時刻・履歴パス・スケジュールキー」をまとめ、
``record_*()`` で成功 / 失敗 / 自動切替の履歴を書く。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

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
    ROUTE_API,
    ROUTE_BROWSER_FALLBACK_EMPTY,
    HistoryRow,
)
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock

from src import paths
from src.exceptions import (
    BrowserFallbackFailedError,
    EmptyReportError,
    ReportFolderNotFoundError,
    ReportNotRegisteredError,
    ScheduledDownloadFailedError,
)
from src.history import record, resolved_folder
from src.service._utils import history_path
from src.service.browser import fetch_via_browser
from src.service.routes import (
    fetch_with_auto_fallback,
    is_api_path,
    planned_route,
)
from src.service.save import require_folder, save_report
from src.service.scheduler import select_targets, warn_shared_reports
from src.sheets.master import ReportEntry, load_master
from src.sheets.schedule import ScheduleRule, load_schedule

if TYPE_CHECKING:
    from comken.toolbox.browser.sites.salesforce.base import SalesforceReportBrowser

logger = logging.getLogger("src.service")

# 履歴の「原因区分」列に出す5値。抽象クラス名ではなく誰が直すかで分ける
# （設計判断は docs/開発/仕様書.md 4.28 参照）
CAUSE_CONFIG = "設定"
CAUSE_SALESFORCE = "Salesforce"
CAUSE_EMPTY_DATA = "データなし"
CAUSE_FILE = "ファイル"
CAUSE_PROGRAM = "プログラム"

# Report API の上限。2000件超で打ち切られたレポートをブラウザで取り直して、
# この行数以下しか取れなければ全件取れていない（``routes.py`` でも同じ値を使う）。
_REPORT_API_ROW_LIMIT = 2000


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
) -> list[Path]:
    """管理表で有効なレポートをまとめて取得する。

    定期実行のプロジェクトから呼ぶ。**1件失敗しても残りは続ける**。戻り値は `list[Path]`
    のままで `CSV` を返さない（定期取得の呼び出し側は中身を読まないため）。

    ``filters_by_report`` は、管理番号ごとに Salesforce Report API の実行時フィルタを
    指定する。指定のない管理番号は、Salesforce に保存されているレポート条件のまま実行する。
    URL やレポート ID ではなく管理番号をキーにするため、管理表で参照先を差し替えても
    呼び出し側のコードは変えなくてよい。

    **API・ブラウザ経由・SOQLのどれで取るかは、管理表の「2000件超」「SOQL」列で決まる**
    （呼び出し側は管理番号を意識しない）。ブラウザ経由のレポートは、ブラウザを開いた直後に
    ``login_with_credentials()`` で DPAPI から自動入力し、MFA をスマホで承認する（承認
    されなければ ``LoginFailedError``、認証情報が未登録なら ``CredentialNotFoundError``。
    `fetch_via_browser()` 参照）。SOQL経由のレポートは同じ管理番号の `SoqlReport` が
    `src.soql_reports` に登録されている必要がある（`fetch_via_soql()` 参照）。

    **ブラウザの使い回し:** 1 回の実行の中で組織ごとにブラウザを 1 つだけ開いて使い回す。
    取得中に例外が出たブラウザはその場で閉じて dict から外す（壊れたブラウザを引き継がない）。

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

    Returns:
        保存したファイルのパス一覧。1件も取得対象が無いとき・別の実行が
        進行中でロックが取れなかったときは空リスト ``[]``。

    Raises:
        ReportNotRegisteredError: ``filters_by_report`` に管理表未登録の管理番号がある場合。
        SoqlReportNotRegisteredError: 管理表の「SOQL」列が「○」なのに、同じ管理番号の
            `SoqlReport` が登録されていない場合。
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。**取得できたものは
            保存したうえで**送出する。
    """
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
        return _download_scheduled_locked(project, filters_by_report=filters_by_report)


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
) -> list[Path]:
    """``download_scheduled()`` のロック取得後に動く本体。

    薄いラッパー ``download_scheduled()`` から切り出した本体。流れは「入力準備 →
    対象選定とログ → 1 件ずつ実行 → ブラウザ後始末 → 終了ログ / 失敗通知」。
    """
    filters_by_report = filters_by_report or {}
    entries, rules_by_report, current = _prepare_run_inputs()
    _validate_filters_by_report(filters_by_report, entries)
    warn_shared_reports(entries)

    run_started = time.perf_counter()
    targets, skipped = _select_and_log_targets(entries, rules_by_report, current)
    # 1 件ごとの理由は DEBUG（障害調査時に ``--log-level=DEBUG`` で見る）
    _log_skipped_details(skipped)

    # 同じ組織のブラウザを 1 回の実行で使い回すための入れ物（``site_for()`` の ``type`` がキー）。
    # ``finally`` で全部閉じる。取得中に例外が出たものは ``fetch_via_browser()`` 側で外す。
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] = {}
    try:
        saved, failed, last_exception = _execute_each_target(
            targets,
            project,
            filters_by_report,
            current,
            browser_sessions,
        )
    finally:
        _close_browser_sessions(browser_sessions)

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


def _execute_each_target(
    targets: list,
    project: str,
    filters_by_report: dict[str, list[dict]],
    current: dt.datetime,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser],
) -> tuple[list[Path], list[str], BaseException | None]:
    """1 件ずつ ``_download()`` を呼び、 成功 / 失敗を集計する。

    ``_already_failed`` は常に空リストなので使わない。失敗時に
    ``ScheduledDownloadFailedError`` から ``__cause__`` で辿れるよう、最後に捕捉した
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
                    filters=filters_by_report.get(entry.key),
                    current=current,
                    browser_sessions=browser_sessions,
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


def _close_browser_sessions(
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser],
) -> None:
    """使い回していたブラウザを全部閉じる。

    ``fetch_via_browser()`` 側で取得中に例外が出たものは既に dict から外して閉じているので、
    ここに残っているのは成功裡に使い終わったブラウザだけ。

    ``__exit__()`` に ``None`` を渡すのは、例外情報を渡すと正常終了でも
    エラー時スクリーンショットが撮られるため。
    """
    for sf in browser_sessions.values():
        try:
            sf.__exit__(None, None, None)
        except Exception as close_exc:
            logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)


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

    def record_success_with_route(
        self,
        path: Path,
        rows: Table,
        *,
        route: str,
        executed_at: dt.datetime,
    ) -> None:
        """成功履歴の記録（自動切替で経路を上書きする）。

        「ブラウザに切り替えて成功」専用の経路。``HistoryRow.route`` に
        ``ROUTE_BROWSER_FALLBACK_*`` を入れて記録する（SOQL 化や管理表の修正が要る
        レポートを履歴から拾えるようにするため）。
        """
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
        logger.info(
            "取得しました（自動切替で成功）: %s（%d 行 / %.1f 秒 / route=%s）",
            path,
            len(rows),
            seconds,
            route,
        )
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=executed_at,
            folder=path.parent,
        )


def _log_fetch_start(entry: ReportEntry, route: str, progress: tuple[int, int] | None) -> None:
    """1 件の取得開始を INFO で 1 行出す。

    ``_download()`` の冒頭（Salesforce へ問い合わせる前）に呼び、 ``[i/n]`` の進み具合と
    管理番号・概要・**予定していた経路**を 1 行で残す。取得中に固まったとき
    「どのレポートで止まったか」を分かるようにするのが目的。自動切替の WARNING は
    別に出ているので、ここでは重複させない。
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
    filters: list[dict] | None = None,
    current: dt.datetime,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
    progress: tuple[int, int] | None = None,
) -> Path:
    """1件を取得して保存し、成否を履歴に残す。

    ``schedule_run_time`` は出力ファイル名にスケジュール時刻を埋め込むために
    ``save_report()`` まで運ぶ（``select_targets()`` が必ず渡す）。

    ``current`` は ``_download_scheduled_locked()`` 冒頭で固定した ``clock_now()``。
    ``save_report()`` 経由でファイル名に、``_Attempt`` 経由で履歴の「実行日時」に渡す。

    ``browser_sessions`` は同じ組織のブラウザを共有するための dict。``None`` のときは
    その場で開いて閉じる。

    **自動切替のオーケストレーション**: Report API 経路（``use_soql=False`` かつ
    ``exceeds_row_limit=False``）のとき、

    - ``SalesforceReportTruncatedError`` → ブラウザ経由で取り直し
      （``route=ROUTE_BROWSER_FALLBACK_TRUNCATED``）
    - 0 行 + ``allow_empty=False`` → ブラウザ経由で取り直し
      （``route=ROUTE_BROWSER_FALLBACK_EMPTY``）。ブラウザでも 0 行なら
      ``EmptyReportError`` で失敗扱い（``Data=""`` のまま）

    を取り直す。「実行時フィルタ」 (``filters``) 付きのときはブラウザに
    機能制限があるので取り直さず、今どおりのエラーをそのまま上げる。
    """
    attempt = _Attempt(entry, project, history_path, schedule_key, executed_at=current)
    # ``route`` は ``record_failure`` でも参照するため ``try`` の外に置く。
    # 初期値の空文字は「不明」（``HistoryRow.route`` の既定値）
    route = ""
    try:
        try:
            # 「取得 → 保存」を順に呼ぶ。``save_report()`` が ``EmptyReportError`` を投げても
            # ``route`` には ``fetch_with_auto_fallback()`` が確定した値が入っているよう、別々に呼ぶ
            _log_fetch_start(entry, planned_route(entry), progress)
            require_folder(entry)
            route, table = fetch_with_auto_fallback(
                entry,
                filters=filters,
                browser_sessions=browser_sessions,
            )
            path = save_report(entry, table, schedule_run_time=schedule_run_time, current=current)
        except EmptyReportError as exc:
            # 0 行 + ``allow_empty=False`` の自動切替が走りうる唯一の場所。ブラウザでも 0 行なら
            # ``EmptyReportError`` をそのまま上げる（``route`` は API のまま）
            fallback_path = _try_empty_report_browser_fallback(
                entry,
                browser_sessions,
                filters=filters,
                schedule_run_time=schedule_run_time,
                current=current,
                attempt=attempt,
                route=route,
            )
            if fallback_path is not None:
                return fallback_path
            _record_failure_safely(attempt, exc, route, history_path)
            raise
        except Exception as exc:
            # ``BrowserFallbackFailedError`` は外側の except 段で受ける
            if not isinstance(exc, BrowserFallbackFailedError):
                _record_failure_safely(attempt, exc, route, history_path)
            raise
    except BrowserFallbackFailedError as exc:
        # ``except`` 内で ``raise`` した例外は同じ ``try`` の別の ``except`` に飛ばず素通りする
        # ため、0 件取り直しの失敗も ``2000件超`` の失敗もここで受けて ``route`` を揃える
        if exc.route and route != exc.route:
            route = exc.route
        _record_failure_safely(attempt, exc, route, history_path)
        raise
    attempt.record_success(path, table, route=route)
    return path


def _try_empty_report_browser_fallback(
    entry: ReportEntry,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None,
    *,
    filters: list[dict] | None,
    schedule_run_time: dt.time | None,
    current: dt.datetime,
    attempt: _Attempt,
    route: str,
) -> Path | None:
    """``EmptyReportError`` 時のブラウザ自動切替を試みる。

    0 行 + ``allow_empty=False`` の自動切替が走りうる唯一の場所。ブラウザで 0 行以外が
    取れれば ``record_success_with_route`` まで済ませてパスを返す。ブラウザでも 0 行、
    または前提条件を満たさないときは ``None`` を返し、``_download()`` が
    ``EmptyReportError`` をそのまま失敗として扱う。

    ブラウザ取得自体が失敗した場合は ``BrowserFallbackFailedError``（2000 件超の取り直し失敗と
    同じ型。``_failure_row()`` で「Salesforce」区分）。
    """
    if not (
        route == ROUTE_API
        and not entry.allow_empty
        and filters is None
        and browser_sessions is not None
        and is_api_path(entry)
    ):
        return None

    logger.warning(
        "Report API が 0 行を返したため、ブラウザ経由に自動で切り替えます: "
        "%s（%s）。管理表の「0件あり」を「○」にするか SOQL 化を検討してください",
        entry.key,
        entry.summary,
    )
    try:
        browser_table = fetch_via_browser(entry, browser_sessions=browser_sessions)
    except Exception as browser_exc:
        # 取り直しのブラウザ取得自体が失敗 → ``BrowserFallbackFailedError`` で表現する。
        # 下流の「取得に失敗しました」より手前で WARNING を 1 行出す
        logger.warning(
            "0 件取り直しのブラウザ取得に失敗しました: %s（%s）",
            entry.key,
            browser_exc,
        )
        raise BrowserFallbackFailedError(
            entry.key, entry.summary, "0件", ROUTE_BROWSER_FALLBACK_EMPTY
        ) from browser_exc

    if not browser_table:
        # ブラウザでも 0 行
        logger.warning(
            "ブラウザ経由でも 0 行でした。EmptyReportError で失敗します: %s（%s）",
            entry.key,
            entry.summary,
        )
        return None

    # ブラウザで取れたので保存し直し、履歴は「成功・自動切替」で記録する。
    # 自動切替の WARNING は出済みなので ``_log_fetch_start()`` は再度呼ばない
    path = save_report(
        entry,
        browser_table,
        schedule_run_time=schedule_run_time,
        current=current,
    )
    attempt.record_success_with_route(
        path,
        browser_table,
        route=ROUTE_BROWSER_FALLBACK_EMPTY,
        executed_at=current,
    )
    return path


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
    判定順（狭い条件から評価）:

    1. `ReportFolderNotFoundError` → 取得段階の前（保存先フォルダ検査で停止）
    2. `EmptyReportError` → 取得は成功、保存は未到達
    3. `OSError` → 保存段階（書き込み・権限・共有サーバー断）
    4. その他の `ComkenError` → 取得段階（Salesforce 通信と認証）
    5. それ以外 → プログラム（想定外）

    4. は `download_scheduled()` が捕捉する範囲と一致させている。

    `schedule_key` は履歴の「スケジュールキー」列へ書く（`record_success()` と対称）。

    `route` は取得経路（``ROUTE_API`` / ``ROUTE_SOQL`` / ``ROUTE_BROWSER`` / 自動切替2種）。
    例外の型から判別できないので、``_download()`` が確定した値をキーワード引数で受ける。
    """
    if isinstance(exc, ReportFolderNotFoundError):
        fetched, saved, cause = None, None, CAUSE_CONFIG
    elif isinstance(exc, EmptyReportError):
        fetched, saved, cause = True, None, CAUSE_EMPTY_DATA
    elif isinstance(exc, OSError):
        fetched, saved, cause = True, False, CAUSE_FILE
    elif isinstance(exc, ComkenError):
        fetched, saved, cause = False, None, CAUSE_SALESFORCE
    else:
        fetched, saved, cause = False, None, CAUSE_PROGRAM
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

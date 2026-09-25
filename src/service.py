r"""src/service.py — 取得の本体。

**2026-09 に comken から切り出した。** 管理表・履歴の形式（列定義・読み取り関数）は
引き続き comken 側（`comken.services.salesforce_downloader`）の共有契約で、ここでは
それに従って Salesforce へ実際に取りに行き、保存し、履歴へ書く実行部分だけを持つ。
経緯は comken の `salesforce_downloader/__init__.py` の履歴メモを参照。

    from comken.services.salesforce_downloader import cached_report
    from src.service import download_scheduled

    CUSTOMER_LIST = "1001"        # 各プロジェクトで、意味の分かる名前を付ける

    by_code = cached_report(CUSTOMER_LIST).index("顧客コード")

**2つの関数の意味をはっきり分ける。**

- `download_scheduled()` は「**今この瞬間にまとめて取りに行く**」。管理表で
  `有効` になっているレポートを全て対象に、定期実行のプロジェクトから呼ばれる
- `cached_report()` は「**取っておいたものを受け取る**」。取りに行く関数ではない。
  まだ取れていなければ例外にする。ここで自動的に取りに行くと、**定期取得が動いて
  いないことに誰も気づかなくなる**

戻り値は `Table`。行の検索・抽出・索引化は Table の API でできる。
パスだけ欲しい場合は `cached_report_path()` を使う。
`download_scheduled()` は定期取得したパスのリストを `list[Path]` で返すが、これは
定期取得の呼び出し側が中身を読まず「取らせる」のが目的なので、reader を並べても
使い道がないため（役割の違いが戻り値の型に出ている）。

**急いでその場の最新値が必要なときは、`download_scheduled()` をスケジュール外に
直接実行する。** それ専用のAPIは用意していない。実行タイミングを分ける必要が
あれば、呼び出し側でスケジューラを増やす。Downloader 側に「今すぐ取りに行く」
だけの関数を残すと、定期取得が動いていないことに誰も気づかなくなるため。

プロジェクト側のコードに Salesforce の URL もレポート ID も現れない。管理表の
参照先を差し替えても、`CUSTOMER_LIST = "1001"` はそのままでよい。

このファイルが持つもの:
- 1件を取得して保存し履歴に残す流れ
- Salesforce への問い合わせ

ここに書かないもの:
- 「このプロジェクトのときは」という分岐 → main.py / src/run.py
- 管理表にどんな列があるか → comken の master.py
- 履歴にどんな列があるか・読み取り方 → comken の history.py
- 履歴への書き込み方 → history_writer.py
- Salesforce の認証・API の叩き方 → comken/toolbox/salesforce/
- 取得済みファイルの取り出し（`cached_report` / `output_path`）→ comken の provider.py
- 管理表・履歴の置き場所（`MASTER_PATH` / `HISTORY_PATH`）→ comken の paths.py
- 管理表から1行を引く `_find()` → comken の provider.py（`requests` を経由しない側に置く）
"""

import contextlib
import datetime as dt
import logging
import tempfile
import time
from pathlib import Path

from comken.core.dates import now as clock_now
from comken.core.files import atomic_write
from comken.core.table.model import Table
from comken.core.timer import measure
from comken.exceptions import (
    ComkenError,
    CSVError,
    EmptyReportError,
    HistoryLockTimeoutError,
    HistoryWriteError,
    ReportFolderNotFoundError,
    ReportNotRegisteredError,
    ReportReservePathLimitError,
    ScheduledDownloadFailedError,
)
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock
from comken.services.salesforce_downloader.paths import (
    HISTORY_PATH,
    MASTER_PATH,
)
from comken.services.salesforce_downloader.provider import output_path
from comken.services.salesforce_downloader.sheets import history
from comken.services.salesforce_downloader.sheets.history import HistoryRow
from comken.services.salesforce_downloader.sheets.master import (
    ReportEntry,
    load_master,
    shared_report_ids,
)
from comken.services.salesforce_downloader.sheets.schedule import ScheduleRule, load_schedule
from comken.services.salesforce_downloader.soql_reports import soql_report_for
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.sites import site_for

from src.history_writer import record

logger = logging.getLogger(__name__)

# 履歴の「原因区分」列に出す5値。運用する人が履歴からすぐ「誰が動くか」を判断できるように、
# 抽象クラス名ではなく誰が直すかで分ける（設計判断は docs/開発/仕様書.md 4.28 参照）
CAUSE_CONFIG = "設定"
CAUSE_SALESFORCE = "Salesforce"
CAUSE_EMPTY_DATA = "データなし"
CAUSE_FILE = "ファイル"
CAUSE_PROGRAM = "プログラム"

# ``_reserve_path`` が連番を足して空きファイル名を探索する回数の上限。
# ``comken.core.holidays.WORKDAY_SEARCH_LIMIT`` と同じ理由で、
# 共有サーバーの同期・権限異常などで ``FileExistsError`` が返り続けると無限
# ループになるため、必ず上限を切る。
RESERVE_PATH_LIMIT = 1000


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

    **API・ブラウザ経由・SOQLのどれで取るかは、呼び出し側のコードではなく管理表の
    「2000件超」「SOQL」列で決まる**（`_fetch()` を参照）。呼び出し側は管理番号を
    意識せずに `download_scheduled()` を呼ぶだけでよい。ブラウザ経由のレポートは
    事前に人が一度だけ手動ログインしておく必要がある（詳しくは `_fetch_via_browser()`
    を参照）。SOQL経由のレポートは同じ管理番号の `SoqlReport` が
    `comken.services.salesforce_downloader.soql_reports` に登録されている必要がある
    （詳しくは `_fetch_via_soql()` を参照）。

    **「スケジュール」シート**にこのレポートの行が無いときは、
    「有効」だけで毎回対象にする（後方互換）。
    この機能追加を境に既存のレポートが突然取得されなくなる事故を防ぐため。

    **同時起動の扱い:** WinActor からの呼び出しが重なったり、前回の取得が
    長引いて 2 回目のポーリングが食い違うと、同じレポートを 2 プロセスが
    「今日は未取得」と判定して二重に取得する事故が起きる。これを防ぐため、
    この関数全体（対象選定〜履歴追記の完了まで）を `HistoryFileLock` で
    プロセス間排他する。ロックが取れない場合は**例外にせず**ログだけ出して
    空リスト ``[]`` を返す（終了コード0）。別の実行が進行中なら、それが
    今回の分の取得も担当するため、次のポーリングで追従できる。失敗扱いに
    すると運用者に誤報が飛ぶため、敢えて正常終了にする。

    **実行日時の扱い:** 23:59 に始まった取得が日付をまたいで終わると、
    判定・出力ファイル名・履歴の「実行日時」がバラバラの日付になり、
    翌日のスケジュールキーが「昨日成功済み」で飛ぶ事故が起きる。これを
    防ぐため、関数冒頭で 1 回取った `clock_now()` の値を全工程（スケジュール
    判定・``output_path``・履歴の「実行日時」）で共有する。

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
            保存したうえで**送出する。ログだけに出して正常終了すると、スケジューラや
            RPA 基盤から見て成功と区別が付かない。
    """
    # **同時起動防止:** 履歴CSVとは別のロックファイル
    # （``{HISTORY_PATH}.run.lock``）で、対象選定〜履歴追記をひとまとまりに
    # プロセス間排他する。ロックが取れないときは別プロセスが進行中なので、
    # 例外にせず警告ログだけ出して ``[]`` を返す。
    # ``HistoryFileLock`` は名前のとおり履歴用ロックの部品だが、Windows の
    # msvcrt ロックはファイル単位なので、``HISTORY_PATH`` とは違う
    # ファイルを渡せば履歴読み書き（``{HISTORY_PATH}.lock``）と衝突しない。
    # ``ExitStack`` を使い、ロック取得**だけ**を try/except で囲う。本体側で
    # ``HistoryLockTimeoutError`` が上がっても、それは履歴読み書きの障害なので
    # そのまま伝播させる（``[]`` で握りつぶすと「履歴の障害」が別の実行に紛れて
    # 隠れる）。
    run_lock_path = Path(f"{HISTORY_PATH}.run")
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(HistoryFileLock(run_lock_path, timeout=0))
        except HistoryLockTimeoutError:
            # 進行中の実行が今回の分の取得も担当する。失敗扱いにすると
            # WinActor / RPA 基盤から見て誤報になるため、空リスト + 正常終了
            logger.warning("別の実行が進行中のため今回はスキップします: %s", run_lock_path)
            return []
        return _download_scheduled_locked(project, filters_by_report=filters_by_report)


def _download_scheduled_locked(
    project: str,
    *,
    filters_by_report: dict[str, list[dict]] | None,
) -> list[Path]:
    """``download_scheduled()`` のロック取得後に動く本体。

    実行ロックを ``download_scheduled()`` で取得し、その内側で動くコア部分を
    切り出したヘルパー。``download_scheduled()`` 自体は薄いラッパーに
    留めて、本体の長さ・複雑度を上げないようにする。
    """
    entries = load_master(MASTER_PATH)
    filters_by_report = filters_by_report or {}
    _validate_filters_by_report(filters_by_report, entries)
    _warn_shared_reports(entries)

    # スケジュール管理表を読んで、レポートキーで引けるように索引化。**有効行だけ**を
    # 評価対象にする（無効行は曜日・時刻を足切りする材料にならない）。
    # シートが無い場合は空リスト（後方互換）
    schedule_rules = load_schedule(MASTER_PATH)
    rules_by_report: dict[str, list[ScheduleRule]] = {}
    for rule in schedule_rules:
        if rule.enabled:
            rules_by_report.setdefault(rule.report_key, []).append(rule)

    # **この実行の基準日時を 1 か所で固定。** スケジュール判定・ファイル名・
    # 履歴の「実行日時」の 3 か所が同じ ``current`` を見ることで、23:59 に
    # 始まった取得が日付をまたいでも、開始日で揃う（翌日 00:01 に終わった
    # として履歴が残ると、翌日の同じスケジュールキーが「成功済み」で飛ぶ
    # 事故を防ぐ）。
    current = clock_now()

    targets, already_failed = _select_targets(entries, rules_by_report, current)
    logger.info("定期取得の対象: %d 件", len(targets))

    saved: list[Path] = []
    # 本日すでに2000件超で失敗済みのレポートは Salesforce へ再問い合わせしないが、
    # 「今回も未取得だった」という事実は失敗として残す。ここで空のまま黙って
    # 進むと、2回目以降の定期実行が常に成功扱い（終了コード0）になり、
    # 「失敗をRPA基盤が終了コードで判断できるようにする」という契約に反する。
    failed: list[str] = list(already_failed)
    # 失敗時に ``ScheduledDownloadFailedError`` から ``__cause__`` で辿れるよう、
    # 直近の捕捉した例外を覚えておく（同じ失敗が複数件あっても、最後の1件だけを
    # 連鎖させる）。``None`` のままだと「原因例外が無い」ことを示す
    last_exception: BaseException | None = None
    for entry, schedule_key, schedule_rule in targets:
        try:
            saved.append(
                _download(
                    entry,
                    project,
                    HISTORY_PATH,
                    schedule_key,
                    schedule_run_time=(
                        schedule_rule.desired_time if schedule_rule is not None else None
                    ),
                    filters=filters_by_report.get(entry.key),
                    current=current,
                )
            )
        except (ComkenError, OSError) as e:
            # **想定した失敗は続ける。想定していない失敗は止める。**
            # - `ComkenError` は `docs/ERRORS.md` に対処法が載っている想定内の失敗なので続行する
            # - `OSError` は共有サーバー断・権限・パスなど運用上の失敗。保存先は
            #   レポートごとに違うので、1本ダメでも他は書けるので続行する
            # - それ以外（`TypeError` などプログラムのバグ）は想定していない。
            #   `ScheduledDownloadFailedError`（＝「1件取れませんでした」）の顔で
            #   出てくると、非エンジニアが「もう一度実行してみる」を繰り返すだけなので、
            #   ここでは捕捉せず、その場で落として気づかせる
            # KeyboardInterrupt など処理中断を示す例外は `ComkenError` / `OSError` の
            #   どちらでもないので、これもそのまま抜ける
            logger.error("取得に失敗しました: %s（%s）", entry.key, e)
            failed.append(entry.key)
            last_exception = e

    logger.info("定期取得: %d 件中 %d 件を取得しました。", len(targets), len(saved))
    if failed:
        # 続けたぶん、最後に必ず知らせる（終了コードで落ちたことが分かるように）。
        # 直近の失敗を ``__cause__`` に乗せて送出する（呼び出し側が
        # ``raise X from original`` 相当の診断情報を得られるようにする）
        raise ScheduledDownloadFailedError(failed, HISTORY_PATH) from last_exception
    return saved


class _Attempt:
    """1件の取得（フォルダ確認 → 取得 → 保存）を取り持つ文脈。

    履歴を書く関数に毎回同じ値の組を渡す煩雑さを消すための箱。
    コンストラクタで文脈を1回だけ受け取り、開始時刻もここで `time.perf_counter()`
    で記録する。`record_*()` は履歴とログを書くだけ。`_download()` 側の
    `try/except` はそのまま（失敗の段階と原因区分は `_failure_row()` が
    例外の型だけから決める）。

    ``schedule_key`` はスケジュール行に紐付く取得で値が入り、スケジュール行が無い
    レポートの取得（後方互換）は空文字。``_matched_schedule_key()`` が
    戻り値の第2要素として返した値をそのまま受け取り、``HistoryRow.schedule_key``
    に詰めて履歴へ書く。
    """

    def __init__(
        self,
        entry: ReportEntry,
        project: str,
        history_path: Path,
        schedule_key: str = "",
        *,
        executed_at: dt.datetime | None = None,
    ) -> None:
        self._entry = entry
        self._project = project
        self._history_path = history_path
        self._schedule_key = schedule_key
        # この実行の開始時刻。``_download_scheduled_locked()`` で 1 回取った
        # ``clock_now()`` の値をそのまま運ぶ。日付をまたぐ長い実行でも、
        # 履歴の「実行日時」が開始時刻で揃う（=翌日分のスケジュールキーが
        # 「昨日成功済み」として誤判定されるのを防ぐ）
        self._executed_at = executed_at
        self._started = time.perf_counter()

    def record_failure(self, exc: BaseException) -> None:
        """失敗時の履歴とログ。"""
        row = _failure_row(
            exc,
            time.perf_counter() - self._started,
            schedule_key=self._schedule_key,
        )
        # 値を計算した場所（ここ）で、ログにも書く。`_download()` 抜けたあと
        # 別の層で「同じ値」を再利用することはない（後付けで属性を渡さない）
        logger.error("取得に失敗しました: %s（%s / 区分=%s）", self._entry.key, exc, row.cause)
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=self._executed_at,
        )

    def record_success(self, path: Path, rows: Table) -> None:
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
        )


def _download(
    entry: ReportEntry,
    project: str,
    history_path: Path,
    schedule_key: str = "",
    *,
    schedule_run_time: dt.time | None = None,
    filters: list[dict] | None = None,
    current: dt.datetime | None = None,
) -> Path:
    """1件を取得して保存し、成否を履歴に残す。

    ``schedule_run_time`` は唯一の出力ファイル名にスケジュール時刻を埋め込むために
    ``_save()`` まで運ぶ（``output_path(entry, schedule_run_time)`` で
    ``%Y%m%d_%H%M`` のタイムスタンプ値として使われる）。
    スケジュール行が無いレポート（後方互換、``schedule_key == ""``）は ``None``
    のままでよく、``_save()`` 側で現在時刻にフォールバックする。

    ``current`` は ``_download_scheduled_locked()`` 冒頭で固定した ``clock_now()``
    の値。``_save()`` 経由で ``output_path(now=current)`` に渡し、
    ``_Attempt`` 経由で履歴の「実行日時」に渡す。日付をまたぐ長い実行でも
    判定・ファイル名・履歴が同じ「開始日」で揃うために 1 実行で同じ値を
    共有する。
    """
    attempt = _Attempt(entry, project, history_path, schedule_key, executed_at=current)
    try:
        _require_folder(entry)
        table = _fetch(entry, filters)
        path = _save(entry, table, schedule_run_time=schedule_run_time, current=current)
    except Exception as exc:
        try:
            attempt.record_failure(exc)
        except (
            HistoryWriteError,
            HistoryLockTimeoutError,
            CSVError,
        ) as history_exc:
            # 元の取得失敗 (`exc`) を ``__cause__`` に乗せて送出する。
            # 履歴書込み失敗の ``history_exc`` はメッセージに含めて、
            # ``ScheduledDownloadFailedError`` の連鎖には元の失敗を
            # 残す（`raise X from history_exc` だと ``history_exc`` が
            # ``__cause__`` を埋めて元の失敗が辿れなくなる）
            raise HistoryWriteError(history_path, str(history_exc), original=exc) from exc
        raise
    attempt.record_success(path, table)
    return path


def _require_folder(entry: ReportEntry) -> None:
    """保存先フォルダが無ければ `ReportFolderNotFoundError`。**勝手に作らない。**

    作らずに失敗させる。無いのは書き間違いのことが多く、勝手に作ると
    誰も読まない場所へ置き続けることになる。

    保存先フォルダは `output_path()` が内部で `report_folder()` 経由で
    組み立てる（管理表の「グループ」＋設定シートの「ベースURL」）。
    `entry.group` が設定シートに無い場合はここより先に
    `GroupNotRegisteredError` が上がる（フォルダの有無より先に、
    そもそも出力先を決められないという、より根本的なエラーとして扱う）。
    """
    folder = output_path(entry, schedule_run_time=None).parent
    if not folder.is_dir():
        raise ReportFolderNotFoundError(entry.key, folder)


def _fetch(entry: ReportEntry, filters: list[dict] | None = None) -> Table:
    """Salesforce へ問い合わせて明細表を返す。

    つなぐ組織は URL のドメインで決まる（`site_for()`）。管理表に組織を選ぶ列は
    作らない——人が選ぶ形にすると、URL と食い違ったときに別の組織へ問い合わせて
    「レポートが見つからない」という分かりにくい失敗になる。

    管理表の「SOQL」「2000件超」列で経路を切り替える（優先順位順）:

    1. ``entry.use_soql`` が真 → SOQL経由（`_fetch_via_soql()`）
    2. ``entry.exceeds_row_limit`` が真 → ブラウザ経由（`_fetch_via_browser()`）
    3. どちらも偽 → 通常の Report API

    SOQL経由・ブラウザ経由のどちらも `filters` は使えない（実行時フィルタの
    仕組みが無い）。
    """
    if entry.use_soql:
        if filters is not None:
            raise ValueError(f"SOQL経由のレポートには実行時フィルタを指定できません: {entry.key}")
        return _fetch_via_soql(entry)
    if entry.exceeds_row_limit:
        if filters is not None:
            raise ValueError(
                f"ブラウザ経由のレポートには実行時フィルタを指定できません: {entry.key}"
            )
        return _fetch_via_browser(entry)
    site = site_for(entry.url)
    with site() as salesforce:
        if filters is None:
            return salesforce.report.get(entry.report_id)
        return salesforce.report.get(entry.report_id, filters=filters)


def _fetch_via_soql(entry: ReportEntry) -> Table:
    """SOQL経由で entry を取得し、`_fetch()` と同じ Table を返す。

    ``entry.key`` と同じ ``KEY`` を持つ ``SoqlReport`` を comken の
    ``soql_report_for()`` で探す。管理表の「SOQL」列が「○」なのに登録が無い
    場合は ``SoqlReportNotRegisteredError``（設定ミス）で止まる。

    つなぐ組織は ``SoqlReport.URL`` ではなく ``entry.url``（管理表側）を使う。
    参照先の単一の正は管理表で、``SoqlReport.URL`` は
    ``download_soql_reports()``（管理表を経由しない独立した経路）専用の値
    だからここでは読まない。
    """
    report_cls = soql_report_for(entry.key)
    instance = report_cls()
    site = site_for(entry.url)
    with site() as salesforce:
        return salesforce.query(instance.soql())


def _fetch_via_browser(entry: ReportEntry) -> Table:
    """ブラウザ経由で entry を取得し、`_fetch()` と同じ Table を返す。

    定期実行（無人）から呼ばれる前提のため、ここではログインを行わない。
    `OPTIONS.PROFILE_ROOT` に永続化された既存のログイン状態をそのまま使う
    （事前に人が一度だけ ``go_login()`` + ``wait_for_manual_login()`` または
    ``login_with_credentials()`` で手動ログインしておく）。ログイン状態が
    切れている場合はエクスポートがHTMLを返し、`SalesforceReportExportError`
    （`ComkenError` のサブクラス）になる。`download_scheduled()` は
    既存の `ComkenError` 処理でそのまま次のレポートへ続行する。

    `selenium` 依存を、実際にブラウザ経由のレポートを使うときだけ読み込むよう、
    ここで初めて import する（管理表で誰も「2000件超」を「○」にしていない
    運用では `service.py` を import しても `selenium` は要らない）。
    """
    from comken.toolbox.browser.sites.salesforce import site_for as browser_site_for

    site_class = browser_site_for(entry.url)
    with site_class() as sf:
        sf.go_login()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
            dict(sf.export_reports({entry.url: tmp_path}))
            with CSV(tmp_path, read_only=True) as source:
                return source.read()


def _validate_filters_by_report(
    filters_by_report: dict[str, list[dict]], entries: dict[str, ReportEntry]
) -> None:
    """実行時フィルタの管理番号がすべて管理表に存在することを確認する。"""
    for report_key in filters_by_report:
        if report_key not in entries:
            raise ReportNotRegisteredError(report_key, list(entries), MASTER_PATH)


def _save(
    entry: ReportEntry,
    table: Table,
    *,
    schedule_run_time: dt.time | None = None,
    current: dt.datetime | None = None,
) -> Path:
    """行数と `allow_empty` に応じて保存先へ書き込み、書き終わったパスを返す。

    0 行・`allow_empty` × → `EmptyReportError`（=失敗）／○ → 空 CSV を置く。
    ``report.get()`` が返す ``Table.columns`` を使うため、0 行でも Salesforce の
    メタデータから得た見出しを保存する。

    保存先は ``output_path()`` が返す単一のパス（``{管理番号}_{スケジュール時刻}.csv``）。
    衝突回避は ``_reserve_unique_path()`` で常に（連番 ``_1`` / ``_2`` … を付けて）。
    同じパスへの上書きは想定しない — ``cached_report()`` 側はフォルダ内検索で
    当日分の最新ファイルを返すので、何度実行しても履歴と当日の最新状態は崩れない。

    ``current`` は ``_download_scheduled_locked()`` で固定した ``clock_now()`` の値。
    ``output_path(now=current)`` に渡すことで、23:59 に始まった実行が日付をまたいで
    終わっても、出力ファイル名が開始日で揃う。``None`` のときは ``output_path()``
    側で ``clock_now()`` にフォールバックする（既存の単体テスト経路を残すため）。

    **失敗時の後始末について:** 書き込みが例外の原因になった場合は、既存の
    ``_download()`` の ``except Exception`` 経路で ``_Attempt.record_failure()``
    を経由して ``ScheduledDownloadFailedError`` に変換される。書きかけのファイルも
    ``unlink(missing_ok=True)`` で後始末する。
    """
    path = _reserve_unique_path(output_path(entry, schedule_run_time, now=current), entry.key)
    try:
        # 問い合わせは成功したが明細が無い。**0件あり × なら失敗扱い、○ なら正常終了**
        if not table and not entry.allow_empty:
            raise EmptyReportError(entry.key, entry.summary, entry.url)
        _write_csv(path, table)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _reserve_unique_path(base_path: Path, report_key: str) -> Path:
    """排他的な新規作成でパスを予約し、既存ファイルを上書きしない。

    同じフォルダに既存ファイルがあると連番（ ``_1`` / ``_2`` …）を足して別の
    ファイル名を探す。 ``RESERVE_PATH_LIMIT`` を超えると ``ReportReservePathLimitError``
    を送出する（権限・同期の異常で ``FileExistsError`` が返り続ける無限ループを
    避けるため）。 ``WORKDAY_SEARCH_LIMIT`` と同じ考え方で上限を切っている。

    ``report_key`` はエラーメッセージに含めるためだけで、ファイル名の組み立てに
    は使わない（呼び出し側が ``base_path`` を組み立てる時点で決定済みのため）。
    """
    candidate = base_path
    sequence = 0
    for _ in range(RESERVE_PATH_LIMIT):
        try:
            candidate.open("x").close()
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = base_path.with_stem(f"{base_path.stem}_{sequence}")
    raise ReportReservePathLimitError(report_key, base_path, RESERVE_PATH_LIMIT)


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ書いてから置き換える。

    複数のプロジェクトが同時に呼ぶので、直接書くと**読んでいる最中のファイルが
    半端な状態**になりうる。同じフォルダ内の置き換えは一度に入れ替わる。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)


def _warn_shared_reports(entries: dict[str, ReportEntry]) -> None:
    """同じ Salesforce レポートを複数の管理番号が指していればログに出す。

    エラーにはしない（意図している場合もある）。**気づけるようにするだけ**。
    """
    for report_id, keys in shared_report_ids(entries).items():
        logger.info(
            "同じ Salesforce レポートを %d 件の管理番号が指しています: %s（%s）",
            len(keys),
            "、".join(str(key) for key in keys),
            report_id,
        )


def _matched_schedule_key(
    entry: ReportEntry,
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
) -> tuple[bool, str]:
    """このレポートを今取得すべきか、すべきなら根拠のスケジュールキーを返す。

    戻り値は ``(取得すべきか, スケジュールキー)``。スケジュールキーは、
    取得すべきだった場合に「どのスケジュール行が根拠になったか」を表し、
    履歴の ``スケジュールキー`` 列にそのまま記録する。スケジュール行が無い
    レポート（後方互換）の取得時は空文字を返す。

    判定ロジック:

    - スケジュール行が無いレポートは ``downloaded_today()`` ベースで「今日まだ
      取れていなければ」取得（後方互換）
    - スケジュール行がある場合、いずれかの行が ``is_due()`` True で、かつ
      ``schedule_succeeded_today()`` が False（=今日まだ成功していない）なら取得。
      複数の行が True を返す場合は取得時刻が一番遅い行のキーを採用（それより早い
      時刻の行は無視する）。``start_time is None`` の行は最も早い扱いとし、具体的な
      時刻を持つ行がある限りそちらを優先する
    - いずれの行も ``is_due()`` False なら False, ""
    - いずれかの行が ``is_due()`` True でも、今日すでに成功済みなら False, ""

    祝日判定は ``ScheduleRule.is_due()`` が ``comken.core.holidays`` の統一
    カレンダーを直接見るため、呼び出し側でカレンダーを用意する必要はない。

    ``current`` は呼び出し元で固定した基準日時。dedup 判定にも ``current.date()``
    を渡し、スケジュール判定と履歴チェックが同じ「日」を見るようにする
    （23:59 をまたぐ実行で「判定は開始日・履歴は翌日」となる事故を防ぐ）。
    """
    rules = rules_by_report.get(entry.key)
    if not rules:
        # スケジュール行が無いレポート: ``downloaded_today()`` で 1 日 1 回までに
        # 制限する（後方互換）。戻り値のキーは空文字
        return not history.downloaded_today(HISTORY_PATH, entry.key, date=current.date()), ""
    due_rules = [
        rule
        for rule in rules
        if rule.is_due(current)
        and not history.schedule_succeeded_today(
            HISTORY_PATH, rule.schedule_key, date=current.date()
        )
    ]
    if not due_rules:
        return False, ""
    # ``start_time is None`` の行は具体的な時刻より優先度が低い（時刻条件なしの行で
    # 取得すると、後の時刻の行を再評価する余地がなくなるため）。
    latest = max(due_rules, key=lambda rule: rule.start_time or dt.time.min)
    return True, latest.schedule_key


def _select_targets(
    entries: dict[str, ReportEntry],
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
) -> tuple[list[tuple[ReportEntry, str, ScheduleRule | None]], list[str]]:
    """定期取得の対象を「有効」かつ「取得すべき」かつ「当日未失敗」のレポートに絞る。

    ``download_scheduled()`` から対象選定ロジックだけを抜き出したヘルパー。
    関数本体が複雑にならないように分離している（``download_scheduled`` 自体は
    既に10近くの分岐があり、複雑度の上限に近い）。

    2000件超で失敗したレポートは、当日中の再実行では Salesforce へ再問い合わせ
    しない。``is_due`` が True になったときだけ履歴を確認し、無駄な履歴読み込みを
    避ける。翌日になれば履歴の日付フィルタが外れて再試行される
    （``truncated_today()`` 側の責任）。

    **ただし、再問い合わせしないことと「成功扱いにする」ことは別。** 戻り値の
    2つ目（``already_failed``）に、この定期実行でスキップした管理番号を積んで
    返す。``download_scheduled()`` はこれを ``failed`` の初期値に使い、
    「今回も未取得だった」という事実を終了コードへ反映させる（そうしないと
    2回目以降の定期実行が常に成功扱いになり、RPA基盤が失敗に気づけない）。

    戻り値の ``targets`` は ``(ReportEntry, schedule_key, ScheduleRule | None)``
    の3要素タプル。``ScheduleRule`` を一緒に運ぶのは、唯一の出力ファイル名に
    スケジュール時刻を埋め込むために ``start_time`` が必要だから。``ScheduleRule`` が
    ``None`` のときはスケジュール行が無いレポート（後方互換）で、その場合は
    ``output_path()`` 側で現在時刻にフォールバックする。

    Returns:
        ``(targets, already_failed)``。``targets`` は実際に取得を試みる
        ``(ReportEntry, schedule_key, ScheduleRule | None)`` のリスト。
        ``already_failed`` は本日すでに2000件超で失敗済みのためスキップした
        管理番号のリスト。
    """
    targets: list[tuple[ReportEntry, str, ScheduleRule | None]] = []
    already_failed: list[str] = []
    for entry in entries.values():
        if not entry.enabled:
            continue
        is_due, schedule_key = _matched_schedule_key(entry, rules_by_report, current)
        if not is_due:
            continue
        if history.truncated_today(HISTORY_PATH, entry.key, current.date()):
            logger.info(
                "本日は2000件超で失敗済みのため、この定期実行ではスキップします"
                "（失敗としては記録します）: %s",
                entry.key,
            )
            already_failed.append(entry.key)
            continue
        # ``schedule_key`` は取得後に履歴へ記録し、``schedule_succeeded_today()``
        # が再判定に使う。スケジュール行が無いレポート（後方互換）は空文字 +
        # ``ScheduleRule = None``
        matched_rule: ScheduleRule | None = None
        if schedule_key:
            for rule in rules_by_report.get(entry.key, ()):
                if rule.schedule_key == schedule_key:
                    matched_rule = rule
                    break
        targets.append((entry, schedule_key, matched_rule))
    return targets, already_failed


def _failure_row(exc: BaseException, seconds: float, schedule_key: str = "") -> HistoryRow:
    """失敗時の履歴1行を、**例外の型だけから**組み立てる。

    `fetched` / `saved` は `_download()` の何処で失敗したかを例外で判別する。
    判定順は上から5行（狭い条件から順に評価）:

    1. `ReportFolderNotFoundError` → 取得段階の前（保存先フォルダ検査で停止）
    2. `EmptyReportError` → 取得は成功、保存は未到達（0件 × 一意）
    3. `OSError` → 保存段階（書き込み・権限・共有サーバー断）
    4. その他の `ComkenError` → 取得段階（Salesforce 通信と認証）
    5. それ以外 → プログラム（comken 側の想定外）

    4. は `download_scheduled()` が捕捉する範囲と一致。1か所の判断で
    「握りつぶす範囲」と「履歴側でバグと書く範囲」を揃える。

    `schedule_key` は成功時と同じく履歴の「スケジュールキー」列へ書くためのもの。
    dedup 判定は成功行しか見ないが、履歴を後から追ったときに「どのスケジュール行が
    いつ失敗したか」が追えるよう、`record_success()` と対称にここで渡す
    （後方互換のため既定値は空文字）。
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
    )

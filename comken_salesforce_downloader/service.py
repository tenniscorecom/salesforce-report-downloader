r"""comken_salesforce_downloader/service.py — 取得の本体。

    from comken_salesforce_downloader import cached_report, download_report

    CUSTOMER_LIST = "1001"        # 各プロジェクトで、意味の分かる名前を付ける

    rows = download_report(CUSTOMER_LIST).read_rows()       # 今すぐ Salesforce から取る
    by_code = cached_report(CUSTOMER_LIST).index("顧客コード")

**2つの関数の意味をはっきり分ける。**

- `download_report()` は「**今この瞬間に取りに行く**」。管理表で定期になっていても、
  今日すでに取っていても、必ず Salesforce へ問い合わせる。呼んだ側が最新を求めている
  のだから、黙って前のものを返さない
- `cached_report()` は「**取っておいたものを受け取る**」。取りに行く関数ではない。
  まだ取れていなければ例外にする。ここで自動的に取りに行くと、**定期取得が動いて
  いないことに誰も気づかなくなる**

戻り値は `Table`。行の検索・抽出・索引化は Table の API でできる。
パスだけ欲しい場合は `download_report_path()` / `cached_report_path()` を使う。
`download_scheduled()` は定期取得したパスのリストを `list[Path]` で返すが、これは
定期取得の呼び出し側が中身を読まず「取らせる」のが目的なので、reader を並べても
使い道がないため（役割の違いが戻り値の型に出ている）。

プロジェクト側のコードに Salesforce の URL もレポート ID も現れない。管理表の
参照先を差し替えても、`CUSTOMER_LIST = "1001"` はそのままでよい。

このファイルが持つもの:
- 1件を取得して保存し履歴に残す流れ
- Salesforce への問い合わせ

ここに書かないもの:
- 「このプロジェクトのときは」という分岐 → 利用プロジェクト
- スケジュール判定（毎日・平日・月末など）→ 呼び出す側
- 管理表にどんな列があるか → master.py
- 履歴にどんな列があるか → history.py
- Salesforce の認証・API の叩き方 → comken/toolbox/salesforce/
- 取得済みファイルの取り出し（`cached_report` / `file_path_of`）→ provider.py
- 管理表・履歴の置き場所（`MASTER_PATH` / `HISTORY_PATH`）→ _paths.py
- 管理表から1行を引く `_find()` → provider.py（`requests` を経由しない側に置く）
"""

import logging
import shutil
import time
from pathlib import Path

from comken.core.files import atomic_write
from comken.core.table.model import Table
from comken.core.timer import measure
from comken.exceptions import ComkenError
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.sites import site_for

from comken_salesforce_downloader import history
from comken_salesforce_downloader._paths import HISTORY_PATH, MASTER_PATH
from comken_salesforce_downloader.exceptions import (
    EmptyReportError,
    HistoryHeaderMismatchError,
    HistoryLockTimeoutError,
    HistoryWriteError,
    ReportFolderNotFoundError,
    ReportReservePathLimitError,
    ScheduledDownloadFailedError,
)
from comken_salesforce_downloader.history import HistoryRow
from comken_salesforce_downloader.master import ReportEntry, load_master, shared_report_ids
from comken_salesforce_downloader.provider import _daily_cache_path_of, _find, file_path_of

logger = logging.getLogger(__name__)

# 履歴の「原因区分」列に出す5値。運用する人が履歴からすぐ「誰が動くか」を判断できるように、
# 抽象クラス名ではなく誰が直すかで分ける（設計判断は docs/開発/仕様書.md 4.28 参照）
CAUSE_CONFIG = "設定"
CAUSE_SALESFORCE = "Salesforce"
CAUSE_EMPTY_DATA = "データなし"
CAUSE_FILE = "ファイル"
CAUSE_PROGRAM = "プログラム"

# ``_reserve_path`` が連番を足して空きファイル名を探索する回数の上限。
# ``comken.core.holidays.calendar.BUSINESS_DAY_SEARCH_LIMIT`` と同じ理由で、
# 共有サーバーの同期・権限異常などで ``FileExistsError`` が返り続けると無限
# ループになるため、必ず上限を切る。
RESERVE_PATH_LIMIT = 1000


@measure
def download_report(report_key: str, project: str = "") -> Table:
    """今すぐ Salesforce から取得して保存し、中身を `Table` で返す。

    **必ず Salesforce へ問い合わせる。** 今日すでに取っていても取り直す。

    Args:
        report_key: 管理表の管理番号（例: "1001"）。
        project: 呼び出し元の名前。履歴に残るので、入れておくと後から追える。

    Returns:
        保存したファイルから読み取った `Table`。

    Raises:
        ReportNotRegisteredError: 管理表に無い管理番号の場合。
        ReportDisabledError: 管理表で無効になっている場合。
        ReportFolderNotFoundError: 保存先のフォルダが無い場合。
        EmptyReportError: 明細が 0 行だった場合。
    """
    entry = _find(report_key, MASTER_PATH)
    path = _download(entry, project, history.TRIGGER_ON_DEMAND, HISTORY_PATH)
    with CSV(path, read_only=True) as csv_file:
        return csv_file.read()


@measure
def download_report_path(report_key: str) -> Path:
    """今すぐ Salesforce から取得して保存し、そのパスを返す。中身は読まない。

    ファイル自体を別のツールに渡したいときに使う。**必ず Salesforce へ問い合わせる。**

    Args:
        report_key: 管理表の管理番号（例: "1001"）。

    Returns:
        保存した CSV の `Path`。

    Raises:
        ReportNotRegisteredError: 管理表に無い管理番号の場合。
        ReportDisabledError: 管理表で無効になっている場合。
        ReportFolderNotFoundError: 保存先のフォルダが無い場合。
        EmptyReportError: 明細が 0 行だった場合。
    """
    entry = _find(report_key, MASTER_PATH)
    return _download(entry, "", history.TRIGGER_ON_DEMAND, HISTORY_PATH)


@measure
def download_scheduled(project: str = "定期実行") -> list[Path]:
    """管理表で「定期」かつ有効なレポートをまとめて取得する。

    定期実行のプロジェクトから呼ぶ。**1件失敗しても残りは続ける**。戻り値は `list[Path]`
    のままで `CSV` を返さない（定期取得の呼び出し側は中身を読まないため）。

    Raises:
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。**取得できたものは
            保存したうえで**送出する。ログだけに出して正常終了すると、スケジューラや
            RPA 基盤から見て成功と区別が付かない。
    """
    entries = load_master(MASTER_PATH)
    _warn_shared_reports(entries)

    targets = [entry for entry in entries.values() if entry.is_scheduled and entry.enabled]
    logger.info("定期取得の対象: %d 件", len(targets))

    saved: list[Path] = []
    failed: list[str] = []
    for entry in targets:
        try:
            saved.append(_download(entry, project, history.TRIGGER_SCHEDULED, HISTORY_PATH))
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
            # どちらでもないので、これもそのまま抜ける
            logger.error("取得に失敗しました: %s（%s）", entry.key, e)
            failed.append(entry.key)

    logger.info("定期取得: %d 件中 %d 件を取得しました。", len(targets), len(saved))
    if failed:
        # 続けたぶん、最後に必ず知らせる（終了コードで落ちたことが分かるように）
        raise ScheduledDownloadFailedError(failed, HISTORY_PATH)
    return saved


class _Attempt:
    """1件の取得（フォルダ確認 → 取得 → 保存）を取り持つ文脈。

    履歴を書く関数に毎回同じ5つの値を渡す煩雑さを消すための箱。
    コンストラクタで文脈を1回だけ受け取り、開始時刻もここで `time.perf_counter()`
    で記録する。`record_*()` は履歴とログを書くだけ。`_download()` 側の
    `try/except` はそのまま（失敗の段階と原因区分は `_failure_row()` が
    例外の型だけから決める）。
    """

    def __init__(
        self,
        entry: ReportEntry,
        project: str,
        trigger: str,
        history_path: Path,
    ) -> None:
        self._entry = entry
        self._project = project
        self._trigger = trigger
        self._history_path = history_path
        self._started = time.perf_counter()

    def record_failure(self, exc: BaseException) -> None:
        """失敗時の履歴とログ。"""
        row = _failure_row(exc, time.perf_counter() - self._started)
        # 値を計算した場所（ここ）で、ログにも書く。`_download()` 抜けたあと
        # 別の層で「同じ値」を再利用することはない（後付けで属性を渡さない）
        logger.error("取得に失敗しました: %s（%s / 区分=%s）", self._entry.key, exc, row.cause)
        history.record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            trigger=self._trigger,
            row=row,
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
        )
        if rows:
            logger.info("取得しました: %s（%d 行 / %.1f 秒）", path, len(rows), seconds)
        else:
            logger.info("取得しました: %s（0 件 / 0件ありのため正常）", path)
        history.record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            trigger=self._trigger,
            row=row,
        )


def _download(entry: ReportEntry, project: str, trigger: str, history_path: Path) -> Path:
    """1件を取得して保存し、成否を履歴に残す。"""
    attempt = _Attempt(entry, project, trigger, history_path)
    try:
        _require_folder(entry)
        table = _fetch(entry)
        path = _save(entry, table)
        if trigger == history.TRIGGER_SCHEDULED:
            _update_daily_cache(entry, path)
    except Exception as exc:
        try:
            attempt.record_failure(exc)
        except (
            HistoryWriteError,
            HistoryLockTimeoutError,
            HistoryHeaderMismatchError,
        ) as history_exc:
            raise HistoryWriteError(history_path, str(history_exc), original=exc) from history_exc
        raise
    attempt.record_success(path, table)
    return path


def _require_folder(entry: ReportEntry) -> None:
    """保存先フォルダが無ければ `ReportFolderNotFoundError`。**勝手に作らない。**

    作らずに失敗させる。無いのは書き間違いのことが多く、勝手に作ると
    誰も読まない場所へ置き続けることになる。
    """
    if not entry.folder.is_dir():
        raise ReportFolderNotFoundError(entry.key, entry.folder)


def _fetch(entry: ReportEntry) -> Table:
    """Salesforce へ問い合わせて明細表を返す。

    つなぐ組織は URL のドメインで決まる（`site_for()`）。管理表に組織を選ぶ列は
    作らない——人が選ぶ形にすると、URL と食い違ったときに別の組織へ問い合わせて
    「レポートが見つからない」という分かりにくい失敗になる。
    """
    site = site_for(entry.url)
    with site() as salesforce:
        return salesforce.report.get(entry.report_id)


def _save(entry: ReportEntry, table: Table) -> Path:
    """行数と `allow_empty` に応じて保存先へ書き込み、書き終わったパスを返す。

    0 行・`allow_empty` × → `EmptyReportError`（=失敗）／○ → 空 CSV を置く。
    ``report.get()`` が返す ``Table.columns`` を使うため、0 行でも Salesforce の
    メタデータから得た見出しを保存する。
    """
    path = _reserve_path(entry)
    try:
        # 問い合わせは成功したが明細が無い。**0件あり × なら失敗扱い、○ なら正常終了**
        if not table and not entry.allow_empty:
            raise EmptyReportError(entry.key, entry.summary, entry.url)
        _write_csv(path, table)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _reserve_path(entry: ReportEntry) -> Path:
    """排他的な新規作成で保存名を予約し、既存ファイルを上書きしない。

    同じフォルダに既存ファイルがあると連番（ ``_1`` / ``_2`` …）を足して別の
    ファイル名を探す。 ``RESERVE_PATH_LIMIT`` を超えると ``ReportReservePathLimitError``
    を送出する（権限・同期の異常で ``FileExistsError`` が返り続ける無限ループを
    避けるため）。 ``BUSINESS_DAY_SEARCH_LIMIT`` と同じ考え方で上限を切っている。
    """
    base_path = file_path_of(entry)
    candidate = base_path
    sequence = 0
    for _ in range(RESERVE_PATH_LIMIT):
        try:
            candidate.open("x").close()
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = base_path.with_stem(f"{base_path.stem}_{sequence}")
    raise ReportReservePathLimitError(entry.key, base_path, RESERVE_PATH_LIMIT)


def _update_daily_cache(entry: ReportEntry, saved_path: Path) -> None:
    """時刻付き保管ファイルを残したまま、当日最新キャッシュを原子的に更新する。"""
    cache_path = _daily_cache_path_of(entry)
    with atomic_write(cache_path) as temporary_path:
        # レポート全体をメモリへ読み込むと、大きなCSVでメモリ不足になりうる。
        # ファイル同士をストリームコピーし、書き終わったあとでatomicに入れ替える。
        shutil.copyfile(saved_path, temporary_path)


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


def _failure_row(exc: BaseException, seconds: float) -> HistoryRow:
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
    )


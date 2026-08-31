"""comken_salesforce_downloader/provider.py — 取得済みファイルの取り出し。

「取りに行く」関数ではなく「既に取ってあるものを受け取る」関数を置く場所。
**import 時に `requests` を読まない**（BO 環境で動かす前提）。
`comken.toolbox.salesforce` に依存しないことで、`requests` の入っていない環境でも
このモジュールだけを使えるようにする。

    from comken_salesforce_downloader import cached_report

    rows = cached_report("1001").read_rows()

戻り値は `Table`。行の検索・抽出・索引化は Table の API でできる。
パスだけ欲しい場合は `cached_report_path()` を使う。

このファイルが持つもの:
- 定期取得済みのファイルを返す
- 保存先パスの組み立て

ここに書かないもの:
- Salesforce への問い合わせ → service.py
- 履歴への記録 → history.py
- 管理表の読み込み（列定義・雛形・検査）→ master.py
"""

import logging
from collections import OrderedDict
from pathlib import Path

from comken.core.files import DateNameBuilder
from comken.core.table.model import Table
from comken.core.timer import measure
from comken.toolbox.csv import CSV

from comken_salesforce_downloader._paths import MASTER_PATH
from comken_salesforce_downloader.exceptions import (
    CachedReportNotFoundError,
    CachedReportNotRegisteredError,
    ReportDisabledError,
    ReportNotRegisteredError,
)
from comken_salesforce_downloader.master import ReportEntry, load_master

logger = logging.getLogger(__name__)

# ── 管理表のプロセス内キャッシュ ──────────────────────────────────────
# `_find()` が `cached_report` / `cached_report_path` / `download_report` などの
# 公開 API から呼ばれるたびに `load_master()` を実行すると、**ループの中で
# レポートを N 件取ると管理表 Excel を N 回開く**ことになる。 管理表は 1 回の
# 実行中に変わらないので、**解決済み絶対パス**をキーにプロセス内キャッシュを
# 持つ（``Config(path)`` と同じ考え方:  相対パスと絶対パスを同一視し、
# ``Path.resolve()`` の Windows UNC / 8.3 形式の揺れも吸収する）。
#
# キャッシュの無効化条件:
# - **同じパスのファイルを書き換えても反映されない**。 ``stat()`` による更新
#   確認は意図的に行わない（``Config`` と同じ理由:  Windows で 1 回 25
#   マイクロ秒、共有サーバーではネットワーク往復。 業務ツールは実行中の
#   管理表の書き換えを想定しない）。
# - 反映が必要なときは ``_reset_cached_master()`` を呼んでから再アクセスする。
# - ファイルが存在しないパスはキャッシュせず ``ExcelFileNotFoundError`` がそのまま上がる
#   （メッセージにパスが含まれるので、業務担当者が共有サーバーの障害 /
#   パス変更 / 権限喪失に気づける。プロバイダ側で業務例外への変換はしない）。
# - ``_MASTER_CACHE_MAXSIZE``（既定 16）で **FIFO 退避** する:  業務利用では
#   管理表は 1〜数種類しかなく到達はまず無いが、テストで大量の ``tmp_path`` を
#   読むケースでエントリが膨らまないための安全弁。
_MASTER_CACHE_MAXSIZE = 16
_master_cache: OrderedDict[str, dict[str, ReportEntry]] = OrderedDict()

# ── service.py から移動してきた定数 ──
SUFFIX = ".csv"
# ファイル名に使えない文字。概要をファイル名に混ぜるので、ここで落とす
_FORBIDDEN_IN_NAME = '\\/:*?"<>|'
# 概要が長いとパスが伸びすぎるので、ファイル名に使うのはこの長さまで
_SUMMARY_LIMIT = 30


@measure
def cached_report(report_key: str, project: str = "") -> Table:
    """本日の定期取得キャッシュを `Table` で返す。**取りに行かない。**

    Args:
        report_key: 管理表の管理番号（例: "1001"）。
        project: 呼び出し元の名前（履歴には残さないが、例外の調査に使えるよう受け取る）。

    Returns:
        本日の最新キャッシュから読み取った `Table`。

    Raises:
        ReportNotRegisteredError: 管理表に無い管理番号の場合。
        ReportDisabledError: 管理表で無効になっている場合。
        CachedReportNotRegisteredError: 管理表で「個別」になっている場合。
        CachedReportNotFoundError: 本日のキャッシュが無い場合。
    """
    entry = _find(report_key, MASTER_PATH)
    if not entry.is_scheduled:
        raise CachedReportNotRegisteredError(entry.key, entry.summary, entry.schedule, MASTER_PATH)
    path = _daily_cache_path_of(entry)
    if not path.is_file():
        raise CachedReportNotFoundError(entry.key, entry.summary, path)
    logger.info("本日の定期取得キャッシュを使います: %s", path)
    with CSV(path, read_only=True, columns=[] if path.stat().st_size == 0 else None) as csv_file:
        return csv_file.read()


@measure
def cached_report_path(report_key: str) -> Path:
    """本日の定期取得キャッシュが置かれるパスを返す。中身は読まない。

    ファイル自体を別のツールに渡したいときに使う。**取りに行かない。**

    Args:
        report_key: 管理表の管理番号（例: "1001"）。

    Returns:
        本日の最新キャッシュとして使われる `Path`。

    Raises:
        ReportNotRegisteredError: 管理表に無い管理番号の場合。
        ReportDisabledError: 管理表で無効になっている場合。
        CachedReportNotRegisteredError: 管理表で「個別」になっている場合。
    """
    entry = _find(report_key, MASTER_PATH)
    if not entry.is_scheduled:
        raise CachedReportNotRegisteredError(entry.key, entry.summary, entry.schedule, MASTER_PATH)
    return _daily_cache_path_of(entry)


def file_path_of(entry: ReportEntry) -> Path:
    """そのレポートを保存するパス。

    ファイル名は「管理番号_概要_日付_時刻_マイクロ秒」。**管理番号を先頭に置く**のは、概要や
    参照先の Salesforce レポートが変わっても、番号は変わらないため。概要を入れるのは、
    保存先を人が直接見たときに何のファイルか分かるようにするため。
    """
    name = f"{entry.key}_{_safe_summary(entry.summary)}{SUFFIX}"
    return entry.folder / DateNameBuilder(name).suffix("%Y%m%d_%H%M%S_%f")


def _daily_cache_path_of(entry: ReportEntry) -> Path:
    """定期取得の当日最新キャッシュに使う固定パスを返す。

    時刻を含めないことで、同日に何度取得しても読む側が同じパスを直接確認できる。
    """
    name = f"{entry.key}_{_safe_summary(entry.summary)}{SUFFIX}"
    return entry.folder / DateNameBuilder(name).suffix("%Y%m%d")


def _find(report_key: str, master_path: Path) -> ReportEntry:
    """管理表から1行を引く。無効なものはここで止める。

    管理表 Excel を読む ``load_master()`` はキャッシュ経由で呼ばれる
    （プロセス内で同じ解決済みパスなら 1 度しか開かない）。 詳細は
    モジュール冒頭のキャッシュ設計コメント参照。
    """
    entries = _load_master_cached(master_path)
    entry = entries.get(report_key)
    if entry is None:
        raise ReportNotRegisteredError(report_key, sorted(entries), master_path)
    if not entry.enabled:
        raise ReportDisabledError(entry.key, entry.summary, master_path)
    return entry


def _load_master_cached(master_path: Path) -> dict[str, ReportEntry]:
    """``load_master()`` を ``Path.resolve()`` 後の絶対パスでキャッシュする。

    公開 API の ``load_master()`` は Excel を毎回読むシグネチャを残す
    （呼び出し側がテストで生のアクセスをすることがあるため）。 その
    公開シグネチャを持ちながら、 ``_find()`` 経由での呼び出しでは
    Excel を開かないように、この内部関数で ``OrderedDict`` ベースの
    1段キャッシュに流す。
    """
    resolved_key = str(master_path) if master_path.is_absolute() else str(master_path.resolve())
    cache = _master_cache
    if resolved_key in cache:
        cache.move_to_end(resolved_key)
        return cache[resolved_key]
    entries = load_master(Path(master_path))
    cache[resolved_key] = entries
    cache.move_to_end(resolved_key)
    if len(cache) > _MASTER_CACHE_MAXSIZE:
        cache.popitem(last=False)
    return entries


def _reset_cached_master() -> None:
    """**すべての管理表キャッシュを破棄する**（テスト用）。

    テストでは ``tmp_path`` がテストごとに違う管理表 Excel を指すため、
    前のテストのキャッシュが残ると別テストの設定が漏れる。 各テストの
    冒頭で呼ぶ想定（``tests/test_provider.py`` の
    autouse fixture から）。 利用者向けの公開 API ではない。
    """
    _master_cache.clear()


def _safe_summary(summary: str) -> str:
    """概要をファイル名に使える形にする。"""
    cleaned = "".join(char for char in summary if char not in _FORBIDDEN_IN_NAME).strip()
    return cleaned[:_SUMMARY_LIMIT] or "レポート"


r"""src/service/routes.py — 取得経路の選択と「Report API 経路の自動切替」。

``download_scheduled()`` の 1 件単位で、管理表の「SOQL」「2000件超」列に応じて
Report API / SOQL / ブラウザの 3 経路を選ぶ。 そこから先が `_fetch()` で、
Report API 経路での自動切替（``SalesforceReportTruncatedError`` → ブラウザ）は
``fetch_with_auto_fallback()`` が担当する。

    from src.service.routes import (
        _fetch,
        fetch_with_auto_fallback,
        is_api_path,
        planned_route,
    )

``is_api_path()`` / ``planned_route()`` は予定経路の判定用。
``_download()`` が ``_log_fetch_start()`` の前に「予定していた経路」を出す
ためのヘルパで、 ``_fetch()`` と同じ優先順位（SOQL > ブラウザ > API）を
そのまま写している。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from comken.core.table.model import Table
from comken.exceptions import SalesforceReportTruncatedError
from comken.services.salesforce_downloader.history import (
    ROUTE_API,
    ROUTE_BROWSER,
    ROUTE_BROWSER_FALLBACK_TRUNCATED,
    ROUTE_SOQL,
)

from src.exceptions import (
    BrowserFallbackFailedError,
    BrowserFallbackIncompleteError,
)
from src.service.api import fetch_via_api
from src.service.browser import fetch_via_browser
from src.service.soql import fetch_via_soql
from src.sheets.master import ReportEntry

if TYPE_CHECKING:
    from comken.toolbox.browser.sites.salesforce.base import SalesforceReportBrowser

logger = logging.getLogger("src.service")

# Report API の上限。2000件超で打ち切られたレポートをブラウザで取り直して、
# この行数以下しか取れなければ全件取れていない（``fetch_with_auto_fallback()`` で
# 判定して ``BrowserFallbackIncompleteError`` に流す） — 値は公開しないので
# パッケージ private として保持する。
_REPORT_API_ROW_LIMIT = 2000


def planned_route(entry: ReportEntry) -> str:
    """``_fetch()`` と同じ優先順位で「予定していた経路」を返す。

    「取得開始」ログは ``require_folder()`` の前に出すので、Salesforce へ
    問い合わせる前の段階では「実際に何を使うか」は未確定。``_fetch()`` の
    分岐ロジック（``use_soql`` → SOQL / ``exceeds_row_limit`` → ブラウザ /
    どちらも偽 → API）をそのまま写して、予定していた経路の文字列だけを
    先に出しておく。Report API 経路で「自動切替」が起きた事実は別途既存の
    WARNING で出る（= ここでの出力は予定であり、最終結果ではない）。
    """
    if entry.use_soql:
        return ROUTE_SOQL
    if entry.exceeds_row_limit:
        return ROUTE_BROWSER
    return ROUTE_API


def is_api_path(entry: ReportEntry) -> bool:
    """このレポートが「Report API」経路かどうか。

    SOQL 経由・ブラウザ経由（最初から）は自動切替対象外。``use_soql=False`` かつ
    ``exceeds_row_limit=False`` のときだけ Report API 経路。
    """
    return not entry.use_soql and not entry.exceeds_row_limit


def _fetch(
    entry: ReportEntry,
    filters: list[dict] | None = None,
    *,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
) -> Table:
    """Salesforce へ問い合わせて明細表を返す。

    つなぐ組織は URL のドメインで決まる（`site_for()`）。管理表に組織を選ぶ列は
    作らない——人が選ぶ形にすると、URL と食い違ったときに別の組織へ問い合わせて
    「レポートが見つからない」という分かりにくい失敗になる。

    管理表の「SOQL」「2000件超」列で経路を切り替える（優先順位順）:

    1. ``entry.use_soql`` が真 → SOQL経由（`fetch_via_soql()`）
    2. ``entry.exceeds_row_limit`` が真 → ブラウザ経由（`fetch_via_browser()`）
    3. どちらも偽 → 通常の Report API（``fetch_via_api()``）

    **Report API 経路での「自動切替」は `_download()` 側でオーケストレーションする。
    この関数は与えられた経路をそのまま実行するだけのシンプルな役割に留め、
    ``SalesforceReportTruncatedError`` が出た／0件 + ``allow_empty`` × という
    2 つの失敗条件の検知と、ブラウザ経由の取り直し + 経路記録 (``route``) は
    ``_download()`` に集約する。``_fetch`` 自体は「経路を選んで取得する」
    だけを知っていればよい（``_download()`` のテスト容易性を上げるため）。

    SOQL経由・ブラウザ経由のどちらも `filters` は使えない（実行時フィルタの
    仕組みが無い）。

    ``browser_sessions`` は ``_download_scheduled_locked()`` で作られた
    使い回し用の dict。``None`` のときは従来通り「その場で開いて閉じる」
    （``fetch_via_browser()`` 側のキーワード既定値）。
    """
    if entry.use_soql:
        if filters is not None:
            raise ValueError(f"SOQL経由のレポートには実行時フィルタを指定できません: {entry.key}")
        return fetch_via_soql(entry)
    if entry.exceeds_row_limit:
        if filters is not None:
            raise ValueError(
                f"ブラウザ経由のレポートには実行時フィルタを指定できません: {entry.key}"
            )
        return fetch_via_browser(entry, browser_sessions=browser_sessions)
    return fetch_via_api(entry, filters)


def fetch_with_auto_fallback(
    entry: ReportEntry,
    *,
    filters: list[dict] | None,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None,
) -> tuple[str, Table]:
    """Report API 経路での自動切替（``SalesforceReportTruncatedError``）を扱う。

    戻り値は ``(route, table)`` の2要素タプル:

    - ``route``: 最終的に使った取得経路（``ROUTE_API`` / ``ROUTE_SOQL`` /
      ``ROUTE_BROWSER`` / 自動切替2種）
    - ``table``: 取得できた Table

    Report API 経路以外（SOQL・最初からブラウザ）のときは自動切替せず、``_fetch()`` の
    戻り値をそのまま ``route=ROUTE_SOQL`` / ``route=ROUTE_BROWSER`` で返す。
    0行+``allow_empty=False`` の自動切替は ``_download()`` 側で ``save_report()`` が
    ``EmptyReportError`` を投げた後に行う。
    """
    if not is_api_path(entry):
        # SOQL / 最初からブラウザ経由は自動切替しない
        table = _fetch(entry, filters, browser_sessions=browser_sessions)
        route = ROUTE_SOQL if entry.use_soql else ROUTE_BROWSER
        return route, table

    # Report API 経路。``SalesforceReportTruncatedError`` が出たら自動でブラウザに切り替え
    try:
        table = _fetch(entry, filters, browser_sessions=browser_sessions)
        return ROUTE_API, table
    except SalesforceReportTruncatedError:
        if filters is not None:
            # 実行時フィルタ付きはブラウザに機能制限があり取り直し無意味。
            # そのまま呼び出し側へ例外を上げる（``_download()`` 側の ``except Exception``
            # で ``ScheduledDownloadFailedError`` に変換される）
            raise
        if browser_sessions is None:
            # ``browser_sessions`` が無い経路（単体テスト・直接呼び出し）はブラウザを
            # 開く契約ではないので、自動切替せず例外を上げる
            raise
        logger.warning(
            "Report API が 2000件超で打ち止められたため、ブラウザ経由に自動で切り替えます: "
            "%s（%s）。管理表の2000件超を○にするか SOQL 化を検討してください",
            entry.key,
            entry.summary,
        )
        try:
            browser_table = fetch_via_browser(entry, browser_sessions=browser_sessions)
        except Exception as browser_exc:
            # ブラウザ取り直し自体に失敗 → 自動切替は成功しなかった扱いとし、
            # ``BrowserFallbackFailedError`` で「ブラウザ取り直し失敗」を表現する。
            # ``SalesforceReportTruncatedError.__init__(self, report_id, row_limit)``
            # のシグネチャに合わせようとすると ``TypeError`` になり、原因区分が
            # 「プログラム」化けるため、ダウンローダー側で専用例外を新設する。
            # ``DownloaderError`` 系なので ``_failure_row()`` で「Salesforce」区分に入る
            # フォールバック自体が落ちたことはこの分岐でしか分からないので、
            # 下流の「取得に失敗しました」より一段手前で WARNING を 1 行出す
            logger.warning(
                "2000件超取り直しのブラウザ取得に失敗しました: %s（%s）",
                entry.key,
                browser_exc,
            )
            raise BrowserFallbackFailedError(
                entry.key, entry.summary, "2000件超", ROUTE_BROWSER_FALLBACK_TRUNCATED
            ) from browser_exc
        # ブラウザ取得は成功したが、その結果も Report API の上限以下しか取れなかった
        # 場合は、全件取れていない（2000件超のはずなので）。
        # 空ファイル（``allow_empty=True``）でも取り返さずにエラー扱い（空ファイルを
        # 残すと利用側が「データが無い日」と「取得失敗」を区別できなくなるため）。
        # ``except`` の外で判定・送出する（上の ``except Exception as browser_exc`` で
        # ``BrowserFallbackFailedError`` に化けないように）。 件数は 1 回だけ数える
        browser_row_count = len(browser_table)
        if browser_row_count <= _REPORT_API_ROW_LIMIT:
            logger.warning(
                "Report API では 2000 件超でしたが、ブラウザで取り直した結果も %d 行でした。"
                "全件取れていないため失敗にします: %s（%s）",
                browser_row_count,
                entry.key,
                entry.summary,
            )
            # 直接の原因は「ブラウザの取得件数が少なかった」というデータ起因の状態。
            # ``SalesforceReportTruncatedError`` を ``__cause__`` に乗せると
            # 「ブラウザ取り直しで例外が起きた」ように見えるので、 ``from None`` で
            # 切り離す（=この例外は自動切替の判定結果であって、原因例外は無い）
            raise BrowserFallbackIncompleteError(
                entry.key, entry.summary, browser_row_count
            ) from None
        return ROUTE_BROWSER_FALLBACK_TRUNCATED, browser_table

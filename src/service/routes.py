r"""src/service/routes.py — 取得経路の選択。

``download_scheduled()`` の 1 件単位で、管理表の「SOQL」「2000件超」列に応じて
Report API / SOQL / ブラウザの 3 経路を選ぶ。ブラウザ経路は **外から渡された**
``fetch_browser`` 関数が担当する（本体はブラウザを操作しない）。

    from src.service.routes import (
        fetch_report,
        planned_route,
    )

``planned_route()`` は予定経路の判定用。
``_download()`` が ``_log_fetch_start()`` の前に「予定していた経路」を出す
ためのヘルパで、 ``fetch_report()`` と同じ優先順位（SOQL > ブラウザ > API）を
そのまま写している。

``fetch_browser`` を渡した実行では **経路がどうであれブラウザで取る**
（対象選定で選ばれた、当日まだ取れていない行を全部取るのが目的）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from comken.core.table.model import Table
from comken.exceptions import SalesforceReportTruncatedError
from comken.services.salesforce_downloader.history import (
    ROUTE_API,
    ROUTE_BROWSER,
    ROUTE_SOQL,
)

from src.service.api import fetch_via_api
from src.service.soql import fetch_via_soql
from src.sheets.master import ReportEntry

logger = logging.getLogger("src.service")


def planned_route(
    entry: ReportEntry,
    *,
    fetch_browser: Callable[[ReportEntry], Table] | None = None,
) -> str:
    """``fetch_report()`` と同じ優先順位で「予定していた経路」を返す。

    「取得開始」ログは ``require_folder()`` の前に出すので、Salesforce へ
    問い合わせる前の段階では「実際に何を使うか」は未確定。``fetch_report()`` の
    分岐ロジック（``use_soql`` → SOQL / ``exceeds_row_limit`` → ブラウザ /
    どちらも偽 → API）をそのまま写して、予定していた経路の文字列だけを
    先に出しておく。

    ``fetch_browser`` が渡されているときは **経路に関係なくブラウザ** を返す
    （``fetch_report()`` も同じ順にブラウザを取る）。
    """
    if fetch_browser is not None:
        return ROUTE_BROWSER
    if entry.use_soql:
        return ROUTE_SOQL
    if entry.exceeds_row_limit:
        return ROUTE_BROWSER
    return ROUTE_API


def check_browser_filters(entry: ReportEntry, filters: list[dict] | None) -> None:
    """ブラウザ経由のレポートに実行時フィルタが指定されていれば ``ValueError``。

    ブラウザ経由の取得（``fetch_browser``）は実行時フィルタを受け付けない。
    SOQL 経由と同じ制約で、フィルタ付きの指定は黙って無視しない。

    Raises:
        ValueError: ``filters`` が ``None`` でないとき。
    """
    if filters is not None:
        raise ValueError(f"ブラウザ経由のレポートには実行時フィルタを指定できません: {entry.key}")


def check_browser_run_filters(
    filters_by_report: dict[str, list[dict]] | None,
    fetch_browser: Callable[[ReportEntry], Table] | None,
) -> None:
    """``fetch_browser`` と実行時フィルタを同時に指定していれば ``ValueError``。

    ブラウザ経由の取得は実行時フィルタを受け付けない。**接続する前**に止める
    （Salesforce へ問い合わせる前に落とす）。

    Raises:
        ValueError: ``fetch_browser`` が渡され、かつ実行時フィルタが 1 件でも
            あるとき。
    """
    if fetch_browser is None or not filters_by_report:
        return
    keys = "、".join(sorted(filters_by_report))
    raise ValueError(
        "fetch_browser を渡す実行には実行時フィルタを指定できません"
        f"（フィルタは Report API 専用）: {keys}"
    )


def fetch_report(
    entry: ReportEntry,
    filters: list[dict] | None = None,
    *,
    fetch_browser: Callable[[ReportEntry], Table] | None = None,
) -> tuple[str, Table]:
    """管理表の列に応じて取得経路を選び、``(経路, 取得した Table)`` を返す。

    **``fetch_browser`` が渡されているときは経路に関係なくブラウザで取る。**
    対象選定で選ばれた、当日まだ取れていない行を全部ブラウザで取るのが目的で、
    SOQL の行でも「2000件超」の行でも通常の行でも ``fetch_browser(entry)`` を
    1 回ずつ呼ぶ（SOQL の登録確認 ``soql_report_for()`` も通さない）。
    戻り値の経路は **常に** ``ROUTE_BROWSER`` になる。

    ``fetch_browser`` が ``None`` のとき（本体の通常の実行）の経路の優先順位は
    **SOQL → Report API**:

    1. ``entry.use_soql`` が真 → SOQL 経由（``fetch_via_soql()``）
    2. ``entry.exceeds_row_limit`` が真 → ``ValueError``（ブラウザ経由の行は
       ``fetch_browser`` を渡した実行だけが取る。通常の実行では
       ``_split_browser_targets()`` が先に外すので、ここに来るのは呼び出し方の
       誤りのときだけ）
    3. どちらも偽 → 通常の Report API（``fetch_via_api()``）

    **ブラウザは本体が操作しない。** ブラウザ経路の実際の取得は、外から
    渡された ``fetch_browser``（=別リポジトリ ``salesforce-report-browser`` が
    渡すもの）が 1 件分だけ行う。ブラウザの起動・ログイン・使い回しは
    そのリポジトリの責任で、本体には残さない。

    SOQL 経由・ブラウザ経由のどちらも ``filters`` は使えない（実行時フィルタの
    仕組みが無い）。

    **Report API の 2000 行打ち切りは取り直さない。** ``SalesforceReportTruncatedError``
    がそのまま失敗になる（管理表の「SOQL」か「2000件超」を ``○`` にして
    対処する運用）。取り直しの WARNING を 1 行出してから再送出する。

    Raises:
        ValueError: 実行時フィルタが、ブラウザ経由・SOQL 経由のレポートに
            指定されたとき、または ``fetch_browser`` を渡さない実行で
            ブラウザ経由の行が選ばれたとき。
    """
    if fetch_browser is not None:
        # ブラウザ経由の実行は経路が何であれブラウザで取る。SOQL の行も
        # ``soql_report_for()`` を通さない（ブラウザで取るので登録確認は不要）
        check_browser_filters(entry, filters)
        return ROUTE_BROWSER, fetch_browser(entry)
    if entry.use_soql:
        if filters is not None:
            raise ValueError(f"SOQL経由のレポートには実行時フィルタを指定できません: {entry.key}")
        return ROUTE_SOQL, fetch_via_soql(entry)
    if entry.exceeds_row_limit:
        # ブラウザ経由の行は ``fetch_browser`` を渡した実行だけが取る。
        # 通常の実行では ``_split_browser_targets()`` が先に外すので、
        # ここに来るのは呼び出し方の誤りのときだけ
        raise ValueError(
            f"ブラウザ経由の行は fetch_browser を渡した実行だけで取ります: {entry.key}"
        )
    try:
        return ROUTE_API, fetch_via_api(entry, filters)
    except SalesforceReportTruncatedError:
        # 取り直さない。管理表の「SOQL」か「2000件超」を ○ にするのが
        # 対処なので、どの行がそうなのかを 1 行で知らせる
        logger.warning(
            "Report API が 2000件超で打ち止められました: %s（%s）。"
            "管理表の「SOQL」か「2000件超」を○にしてください",
            entry.key,
            entry.summary,
        )
        raise

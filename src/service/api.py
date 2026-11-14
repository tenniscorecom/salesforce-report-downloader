r"""src/service/api.py — Report API 経由の取得。

``download_scheduled()`` の 1 件分で、管理表の「SOQL」「2000件超」列がどちらも
偽のときに呼ばれる。

    from src.service.api import fetch_via_api

`selenium` 依存に触らない（= 管理表で誰も「2000件超」を「○」にしていない
運用では `src.service` を import しても `selenium` は要らない）。
"""

from __future__ import annotations

from comken.core.table.model import Table
from comken.toolbox.salesforce.sites import site_for

from src.sheets.master import ReportEntry


def fetch_via_api(
    entry: ReportEntry,
    filters: list[dict] | None,
) -> Table:
    """通常の Report API で取得する。

    2000 件超で打ち切られたときの ``SalesforceReportTruncatedError`` は
    そのまま呼び出し側へ上がる（取り直しはしない。管理表の「SOQL」か「2000件超」を
    `○` にして対処する運用）。
    """
    site = site_for(entry.url)
    with site() as salesforce:
        if filters is None:
            return salesforce.report.get(entry.report_id)
        return salesforce.report.get(entry.report_id, filters=filters)

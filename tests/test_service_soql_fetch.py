"""_fetch() の SOQL経由フォールバックのテスト。

管理表の「SOQL」列（``ReportEntry.use_soql``）が真だと、_fetch() がレポートAPIでも
ブラウザ経由でもなく、comken の ``soql_report_for()`` で引いた ``SoqlReport`` 経由で
取得する（「2000件超」列より優先。tests/test_service_browser_fetch.py 側と対）。
"""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from comken.exceptions import SoqlReportNotRegisteredError
from comken.services.salesforce_downloader.sheets.master import ReportEntry
from comken.services.salesforce_downloader.soql_reports import _registry
from comken.services.salesforce_downloader.soql_reports.base import SoqlReport

from src.service import _fetch, _fetch_via_soql

URL = "https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"

ENTRY = ReportEntry(
    key="9001",
    summary="顧客一覧",
    url=URL,
    group="営業本部",
    assignee="山田",
    enabled=True,
    allow_empty=False,
)
SOQL_ENTRY = replace(ENTRY, use_soql=True)
# 「2000件超」も同時に○の行では「SOQL」列が優先される、を確かめる用
SOQL_AND_EXCEEDS_ENTRY = replace(ENTRY, use_soql=True, exceeds_row_limit=True)


class _StubReport(SoqlReport):
    """テスト用の ``SoqlReport`` 実装。"""

    KEY = "9001"
    SUMMARY = "顧客一覧（SOQL）"
    URL = URL
    FOLDER = "不使用"

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


@pytest.fixture(autouse=True)
def _registered_soql_reports():
    """``_registry.SOQL_REPORTS`` にテスト用の1件だけを登録する。"""
    original = _registry.SOQL_REPORTS
    _registry.SOQL_REPORTS = (_StubReport,)
    try:
        yield
    finally:
        _registry.SOQL_REPORTS = original


class TestFetchRoutesToSoql:
    """_fetch() — 「SOQL」列が真の管理番号はSOQL経由になる（「2000件超」より優先）。"""

    def test_uses_soql_when_use_soql_is_true(self):
        table = MagicMock()
        with patch(
            "src.service._fetch_via_soql",
            return_value=table,
        ) as fetch_via_soql:
            result = _fetch(SOQL_ENTRY)

        fetch_via_soql.assert_called_once_with(SOQL_ENTRY)
        assert result is table

    def test_soql_takes_priority_over_exceeds_row_limit(self):
        with (
            patch("src.service._fetch_via_soql") as fetch_via_soql,
            patch("src.service._fetch_via_browser") as fetch_via_browser,
        ):
            _fetch(SOQL_AND_EXCEEDS_ENTRY)

        fetch_via_soql.assert_called_once_with(SOQL_AND_EXCEEDS_ENTRY)
        fetch_via_browser.assert_not_called()

    def test_uses_api_when_use_soql_is_false(self):
        with (
            patch("src.service.site_for") as site_for,
            patch("src.service._fetch_via_soql") as fetch_via_soql,
        ):
            _fetch(ENTRY)

        site_for.assert_called_once_with(ENTRY.url)
        fetch_via_soql.assert_not_called()

    def test_raises_when_filters_given_for_soql_report(self):
        with pytest.raises(ValueError, match=SOQL_ENTRY.key):
            _fetch(SOQL_ENTRY, filters=[{"column": "x", "operator": "equals", "value": "1"}])


class TestFetchViaSoql:
    """_fetch_via_soql() — SOQL経由で取得し、_fetch() と同じ Table を返す。"""

    def test_returns_table_from_query(self):
        table = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.query.return_value = table
        site = MagicMock(return_value=client)
        with patch("src.service.site_for", return_value=site) as site_for:
            result = _fetch_via_soql(SOQL_ENTRY)

        site_for.assert_called_once_with(SOQL_ENTRY.url)
        client.__enter__.return_value.query.assert_called_once_with("SELECT Id, Name FROM Account")
        assert result is table

    def test_raises_when_no_matching_soql_report_registered(self):
        """管理表の「SOQL」列が○なのに、対応する SoqlReport が無ければ設定ミスとして止める。"""
        unregistered = replace(SOQL_ENTRY, key="9999")
        with pytest.raises(SoqlReportNotRegisteredError, match="9999"):
            _fetch_via_soql(unregistered)

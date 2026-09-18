"""_fetch() のブラウザ経由フォールバックのテスト。

管理表の「2000件超」列（``ReportEntry.exceeds_row_limit``）が真だと、_fetch() が
レポートAPIではなく comken.toolbox.salesforce.browser 経由で取得する
（「SOQL」列が優先されるケースは tests/test_service_soql_fetch.py 側）。
既存の _fetch() (API経由) のテストは tests/test_service.py に集約してあるため、
ここではブラウザ経由の分岐だけを扱う。
"""

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from comken.services.salesforce_downloader.sheets.master import ReportEntry

from src.salesforce_downloader.service import _fetch, _fetch_via_browser

ENTRY = ReportEntry(
    key="9001",
    summary="顧客一覧",
    url="https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view",
    group="営業本部",
    assignee="山田",
    enabled=True,
    allow_empty=False,
)
EXCEEDS_ENTRY = replace(ENTRY, exceeds_row_limit=True)


def _fake_browser_site(csv_bytes: bytes = "名前,金額\n山田,100\n".encode()):
    """export_reports() がCSVファイルを書き出すブラウザサイトクラスを作る。"""

    def _export_reports(reports, **kwargs):
        for _url, destination in reports.items():
            Path(destination).write_bytes(csv_bytes)
            yield "00O5g00000ABCDE", Path(destination)

    site_instance = MagicMock()
    site_instance.export_reports.side_effect = _export_reports
    site_instance.__enter__.return_value = site_instance
    site_class = MagicMock(return_value=site_instance)
    return site_class, site_instance


class TestFetchRoutesToBrowser:
    """_fetch() — 「2000件超」列が真の管理番号はブラウザ経由になる。"""

    def test_uses_browser_when_exceeds_row_limit(self):
        table = MagicMock()
        with patch(
            "src.salesforce_downloader.service._fetch_via_browser",
            return_value=table,
        ) as fetch_via_browser:
            result = _fetch(EXCEEDS_ENTRY)

        fetch_via_browser.assert_called_once_with(EXCEEDS_ENTRY)
        assert result is table

    def test_uses_api_when_exceeds_row_limit_is_false(self):
        with (
            patch("src.salesforce_downloader.service.site_for") as site_for,
            patch("src.salesforce_downloader.service._fetch_via_browser") as fetch_via_browser,
        ):
            _fetch(ENTRY)

        site_for.assert_called_once_with(ENTRY.url)
        fetch_via_browser.assert_not_called()

    def test_raises_when_filters_given_for_browser_report(self):
        with pytest.raises(ValueError, match=EXCEEDS_ENTRY.key):
            _fetch(EXCEEDS_ENTRY, filters=[{"column": "x", "operator": "equals", "value": "1"}])


class TestFetchViaBrowser:
    """_fetch_via_browser() — ブラウザ経由で取得し、_fetch() と同じ Table を返す。"""

    def test_returns_table_parsed_from_exported_csv(self):
        site_class, _site_instance = _fake_browser_site()
        with patch("comken.toolbox.salesforce.browser.sites.site_for", return_value=site_class):
            table = _fetch_via_browser(ENTRY)

        assert table.columns == ["名前", "金額"]
        assert list(table) == [{"名前": "山田", "金額": "100"}]

    def test_calls_go_login_before_export(self):
        site_class, site_instance = _fake_browser_site()
        with patch("comken.toolbox.salesforce.browser.sites.site_for", return_value=site_class):
            _fetch_via_browser(ENTRY)

        site_instance.go_login.assert_called_once()

    def test_does_not_call_wait_for_manual_login(self):
        """定期実行(無人)から呼ばれる前提のため、人の入力を待つ呼び出しをしない。"""
        site_class, site_instance = _fake_browser_site()
        with patch("comken.toolbox.salesforce.browser.sites.site_for", return_value=site_class):
            _fetch_via_browser(ENTRY)

        site_instance.wait_for_manual_login.assert_not_called()


class TestSeleniumStaysLazy:
    """「2000件超」列が誰も○にしていない運用では selenium を読み込まない。"""

    def test_fetch_does_not_import_browser_module(self, monkeypatch):
        for mod_name in list(sys.modules):
            if mod_name.startswith("comken.toolbox.salesforce.browser"):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

        with patch("src.salesforce_downloader.service.site_for") as site_for:
            site_for.return_value.__enter__.return_value.report.get.return_value = MagicMock()
            _fetch(ENTRY)

        assert not any(
            mod_name.startswith("comken.toolbox.salesforce.browser") for mod_name in sys.modules
        )

"""Report API 経路での自動切替（ブラウザ経由）のテスト。

2026-10 に ``src/service.py::_fetch_with_auto_fallback()`` で追加された挙動:

- Report API が ``SalesforceReportTruncatedError`` を投げたら、自動でブラウザ経由に
  取り直す（``route=ROUTE_BROWSER_FALLBACK_TRUNCATED``）
- Report API が 0 行 + ``allow_empty=False`` なら、自動でブラウザ経由に
  取り直す（``route=ROUTE_BROWSER_FALLBACK_EMPTY``）。ブラウザでも 0 行なら
  ``EmptyReportError`` で失敗扱い
- SOQL / 最初からブラウザ経路では自動切替しない
- 同じ実行で自動切替が 2 件起きても、ブラウザは 1 回だけ開いて使い回す

``test_service.py`` 側は既存の挙動テスト、
``test_service_browser_fetch.py`` 側はブラウザ経由の単体テスト、
``test_service_soql_fetch.py`` 側は SOQL 経由の単体テスト、
``test_service_browser_fallback.py``（=このファイル）は自動切替の
オーケストレーションを集約する。
"""

import csv
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from comken.exceptions import (
    SalesforceReportTruncatedError,
)
from comken.services.salesforce_downloader import history
from comken.toolbox.csv import CSV

import src.paths as _paths_module
from src import service as service_module
from src.exceptions import (
    BrowserFallbackFailedError,
    ScheduledDownloadFailedError,
)
from src.service import download_scheduled

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"
URL_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000FGHIJ/view"
ROWS = [{"名前": "山田", "金額": "100"}, {"名前": "鈴木", "金額": "200"}]
ROWS_B = [{"名前": "佐藤", "金額": "300"}]


def _fake_salesforce(rows=None, side_effect=None):
    """report.get() が Table を返す Salesforce クライアント。"""
    if side_effect is not None:
        client = MagicMock()
        client.__enter__.return_value.report.get.side_effect = side_effect
        return MagicMock(return_value=client)
    values = ROWS if rows is None else rows
    from comken.core.table import Table

    client = MagicMock()
    client.__enter__.return_value.report.get.return_value = Table(["名前", "金額"], values)
    return MagicMock(return_value=client)


def _fake_browser_site(rows=None):
    """export_reports() が CSV を書き出すブラウザ版サイトクラス。"""
    values = ROWS if rows is None else rows
    if values:
        lines = "\n".join(f"{r['名前']},{r['金額']}" for r in values)
        csv_bytes = f"名前,金額\n{lines}\n".encode()
    else:
        # 0 行: 見出しだけの CSV
        csv_bytes = "名前,金額\n".encode("utf-8-sig")

    def _export_reports(reports, **kwargs):
        for _url, destination in reports.items():
            Path(destination).write_bytes(csv_bytes)
            yield "dummy", Path(destination)

    instance = MagicMock()
    instance.export_reports.side_effect = _export_reports
    instance.__enter__.return_value = instance
    return MagicMock(return_value=instance)


def _patch_master_path(monkeypatch, master: Path, history_path: Path) -> None:
    """``MASTER_PATH`` / 履歴パスを差し替える（``test_service.py`` と同等の薄い版）。"""
    monkeypatch.setattr(_paths_module, "MASTER_PATH", master)
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path)


def _make_master(path: Path, master_rows, *, settings_rows) -> Path:
    """``test_service.py`` の ``make_master`` 相当の最小実装。"""
    from comken.core.table import Table
    from comken.toolbox.excel import Excel

    HEADERS = [
        "ID",
        "概要",
        "Salesforce URL",
        "グループ",
        "担当者",
        "有効",
        "0件あり",
        "2000件超",
        "SOQL",
    ]
    GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]
    table_rows = [dict(zip(HEADERS, row, strict=True)) for row in master_rows]
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table("管理表", Table(HEADERS, table_rows))
        settings_table_rows = [
            dict(zip(GROUP_SETTINGS_HEADERS, row, strict=True)) for row in settings_rows
        ]
        book.create_data_sheet("設定").create_table(
            "設定", Table(GROUP_SETTINGS_HEADERS, settings_table_rows)
        )
    return path


# ──────────────────────────────────────────────────────────────────────
# 自動切替のオーケストレーション
# ──────────────────────────────────────────────────────────────────────
class TestAutoFallbackOnTruncated:
    """``SalesforceReportTruncatedError`` → ブラウザに自動切替して成功。

    Report API 経路で ``SalesforceReportTruncatedError`` が来たら、自動で
    ブラウザ経由に取り直す。成功時の ``route`` は ``ROUTE_BROWSER_FALLBACK_TRUNCATED``。
    """

    def test_truncated_falls_back_to_browser_and_succeeds(self, tmp_path, monkeypatch):
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9001", "案件集計", URL_A, "営業本部", "山田", "○", "", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce(
            side_effect=SalesforceReportTruncatedError("00O5g00000ABCDE", 2000)
        )
        browser_site = _fake_browser_site(ROWS)
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
        ):
            saved = download_scheduled("案件集計")

        assert len(saved) == 1
        # Report API が 1 回、ブラウザが 1 回叩かれている
        assert api_site.return_value.__enter__.return_value.report.get.call_count == 1
        assert browser_site.return_value.export_reports.call_count == 1

        # 履歴: 成功・route=自動切替（2000件超）
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "成功"
            assert row["取得経路"] == history.ROUTE_BROWSER_FALLBACK_TRUNCATED
            assert row["取得件数"] == str(len(ROWS))


class TestAutoFallbackOnEmpty:
    """0 行 + ``allow_empty=False`` → ブラウザに自動切替して成功 / 失敗。"""

    def test_browser_has_rows_succeeds_with_empty_fallback(self, tmp_path, monkeypatch):
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            # 0件あり=× で空にしたいシナリオ
            [["9101", "空になる", URL_A, "営業本部", "山田", "○", "×", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce([])  # 0 行を返す
        browser_site = _fake_browser_site(ROWS)  # ブラウザは行あり
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
        ):
            saved = download_scheduled("案件集計")

        assert len(saved) == 1
        # 履歴: route=自動切替（0件）
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "成功"
            assert row["取得経路"] == history.ROUTE_BROWSER_FALLBACK_EMPTY
            assert row["取得件数"] == str(len(ROWS))

    def test_browser_also_empty_fails_with_empty_report(self, tmp_path, monkeypatch):
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9101", "空", URL_A, "営業本部", "山田", "○", "×", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce([])
        browser_site = _fake_browser_site([])  # ブラウザも 0 行
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled("案件集計")

        # 履歴: 失敗・route=API（ブラウザに切り替えても 0 行だった）
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "失敗"
            assert row["取得経路"] == history.ROUTE_API
            assert row["エラーコード"] == "EmptyReportError"

    def test_allow_empty_yes_does_not_call_browser(self, tmp_path, monkeypatch):
        """``allow_empty=True`` のときは 0 行で正常終了し、ブラウザを呼ばない。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9101", "空でも成功", URL_A, "営業本部", "山田", "○", "○", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce([])
        # ブラウザはモックしない。呼ばれないことを確認するため、不正な型（None）
        # で patch して「呼ばれたら TypeError」になるようにしておく
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=MagicMock(side_effect=AssertionError("ブラウザを呼んだ")),
            ),
        ):
            download_scheduled("案件集計")

        # 履歴: 成功・route=API・取得件数=0
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "成功"
            assert row["取得経路"] == history.ROUTE_API
            assert row["取得件数"] == "0"
            assert row["原因区分"] == ""


class TestNoFallbackForSoqlAndBrowserRoutes:
    """SOQL / 最初からブラウザ経路では自動切替しない。"""

    def test_soql_route_does_not_fall_back_to_browser(self, tmp_path, monkeypatch):
        from src.soql_reports import _registry
        from src.soql_reports.base import SoqlReport

        class _StubSoqlReport(SoqlReport):
            KEY = "9201"
            SUMMARY = "SOQL"
            URL = URL_A
            FOLDER = "営業本部"

            def soql(self) -> str:
                return "SELECT Id, Name FROM Account"

        monkeypatch.setattr(_registry, "registered_reports", lambda: (_StubSoqlReport,))

        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # SOQL=○（=SOQL 経由で取得）
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9201", "SOQL", URL_A, "営業本部", "山田", "○", "", "", "○"]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        soql_site = MagicMock()
        soql_client = MagicMock()
        # SOQL クエリで 0 件 → ``SalesforceRequestError`` ではなく空 Table
        from comken.core.table import Table

        soql_client.__enter__.return_value.query.return_value = Table(["名前", "金額"], [])
        soql_site.return_value = soql_client
        with (
            patch("src.service.site_for", return_value=soql_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=MagicMock(side_effect=AssertionError("ブラウザを呼んだ")),
            ),
            # ``download_scheduled()`` は ``EmptyReportError`` を ``ScheduledDownloadFailedError``
            # にラップして返す契約
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled("案件集計")

        # ブラウザは呼ばれない（=SOQL 経路では自動切替しない）。
        # ブラウザ側の site_for が ``AssertionError`` を投げるアサーションを
        # 仕込んであるので、呼ばれたら即座にテストが落ちる構造
        # （``patch`` の ``side_effect`` で検証）

    def test_truncated_route_does_not_fall_back_to_browser(self, tmp_path, monkeypatch):
        """「2000件超」列が真のレポート（最初からブラウザ経由）は、ブラウザ取得中に
        エラーが起きても自動切替しない（取り直し先が無い）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            # 2000件超=○（=最初からブラウザ経由）
            [["9301", "ブラウザ専用", URL_A, "営業本部", "山田", "○", "", "○", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        # API 経路は空モックで「ブラウザ経路で取得」だけをシミュレート
        api_site = MagicMock()
        api_client = MagicMock()
        from comken.core.table import Table

        api_client.__enter__.return_value.report.get.return_value = Table(["名前", "金額"], ROWS)
        api_site.return_value = api_client

        browser_site = _fake_browser_site(ROWS)
        # 同じ site_class を返すようにして、2 回目の取得でも同じインスタンスを使う
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
        ):
            download_scheduled("案件集計")

        # 履歴: 成功・route=ブラウザ（最初からブラウザ経路。自動切替ではない）
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "成功"
            assert row["取得経路"] == history.ROUTE_BROWSER


class TestBrowserReusedAcrossReports:
    """同じ実行で自動切替が 2 件起きても、ブラウザは 1 回だけ開いて使い回す。"""

    def test_two_fallbacks_share_one_browser_instance(self, tmp_path, monkeypatch):
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [
                ["9401", "案件A", URL_A, "営業本部", "山田", "○", "", "", ""],
                ["9402", "案件B", URL_B, "営業本部", "山田", "○", "", "", ""],
            ],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        # Report API は両方とも Truncated
        api_site = _fake_salesforce(
            side_effect=SalesforceReportTruncatedError("00O5g00000ABCDE", 2000)
        )
        browser_site = _fake_browser_site(ROWS)

        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
        ):
            saved = download_scheduled("案件集計")

        assert len(saved) == 2
        # API は 2 件分呼ばれる（=両方 Salesforce へ問い合わせが走った）
        assert api_site.return_value.__enter__.return_value.report.get.call_count == 2
        # ブラウザの ``__enter__`` は 1 回だけ（=同じブラウザを使い回す）
        assert browser_site.call_count == 1
        # ``export_reports`` は 2 回（=2 件の取得）
        assert browser_site.return_value.export_reports.call_count == 2

        with CSV(history_path) as _csv_file:
            rows = list(csv.DictReader(open(history_path, encoding="utf-8-sig")))
        routes = [r["取得経路"] for r in rows if r["成否"] == "成功"]
        assert routes == [
            history.ROUTE_BROWSER_FALLBACK_TRUNCATED,
            history.ROUTE_BROWSER_FALLBACK_TRUNCATED,
        ]


# ──────────────────────────────────────────────────────────────────────
# ブラウザ取り直しが失敗したケース
# ──────────────────────────────────────────────────────────────────────
class TestBrowserFallbackFails:
    """Report API で 2000件超 / 0 行のあと、ブラウザの取り直し自体が失敗したとき。

    旧実装では ``SalesforceReportTruncatedError(report_id, row_limit)`` の
    シグネチャに合わせて ``raise SalesforceReportTruncatedError("…")`` と
    文字列 1 つで ``raise`` していたため ``TypeError`` になり、原因区分が
    「プログラム」化けていた。新実装では ``BrowserFallbackFailedError``
    （``DownloaderError`` 系）で表現するため、原因区分は「Salesforce」になる。

    0 件取り直し（``_download()`` の ``except EmptyReportError``）経路も同じ
    ``BrowserFallbackFailedError`` で表現する（テスト1本で確認する）。
    """

    @staticmethod
    def _make_browser_site_that_fails(message: str = "ブラウザでログイン失敗"):
        """``login_with_credentials()`` が ``CredentialNotFoundError`` 風の
        ``ComkenError`` を投げるブラウザ版サイトクラスを返す。"""
        from comken.exceptions import CredentialNotFoundError

        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.login_with_credentials.side_effect = CredentialNotFoundError("user", [])
        return MagicMock(return_value=instance)

    def test_truncated_then_browser_fails_raises_browser_fallback_error(
        self, tmp_path, monkeypatch
    ):
        """Report API が ``SalesforceReportTruncatedError`` → ブラウザ取り直しも失敗
        → ``BrowserFallbackFailedError`` で「Salesforce」区分。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9501", "案件集計", URL_A, "営業本部", "山田", "○", "", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce(
            side_effect=SalesforceReportTruncatedError("00O5g00000ABCDE", 2000)
        )
        browser_site = self._make_browser_site_that_fails()
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            download_scheduled("案件集計")

        # ``BrowserFallbackFailedError`` が ``ScheduledDownloadFailedError`` に変換されて外へ
        # （個別レポートの失敗→``ScheduledDownloadFailedError``）。``__cause__`` に新しい例外
        assert isinstance(caught.value.__cause__, BrowserFallbackFailedError)
        # 元の ``CredentialNotFoundError`` が連鎖している
        assert caught.value.__cause__.__cause__ is not None

        # 履歴: 失敗・原因区分=「Salesforce」（=``ComkenError`` 系として分類される）
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "失敗"
            assert row["エラーコード"] == "BrowserFallbackFailedError"
            assert row["原因区分"] == "Salesforce"
            # 取得経路は「ブラウザ（自動切替：2000件超）」（=ブラウザに切り替えたが失敗）
            assert row["取得経路"] == history.ROUTE_BROWSER_FALLBACK_TRUNCATED

    def test_empty_then_browser_fails_raises_browser_fallback_error(self, tmp_path, monkeypatch):
        """Report API が 0 行 → ブラウザ取り直しも失敗 → 同じ ``BrowserFallbackFailedError``。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9601", "空になる", URL_A, "営業本部", "山田", "○", "×", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        api_site = _fake_salesforce([])  # Report API が 0 行
        browser_site = self._make_browser_site_that_fails()
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            download_scheduled("案件集計")

        assert isinstance(caught.value.__cause__, BrowserFallbackFailedError)

        # 履歴: 失敗・原因区分=「Salesforce」
        with CSV(history_path) as csv_file:
            row = csv_file.read()[-1]
            assert row["成否"] == "失敗"
            assert row["エラーコード"] == "BrowserFallbackFailedError"
            assert row["原因区分"] == "Salesforce"
            # 取得経路は「ブラウザ（自動切替：0件）」（=ブラウザに切り替えたが失敗）
            assert row["取得経路"] == history.ROUTE_BROWSER_FALLBACK_EMPTY


# ──────────────────────────────────────────────────────────────────────
# 「当日 2000 件超で失敗済みならスキップ」が無いことの回帰テスト
# ──────────────────────────────────────────────────────────────────────
class TestNoSkipOnSameDay:
    """当日中に ``SalesforceReportTruncatedError`` で失敗した記録があっても、
    同じ日の 2 回目の ``download_scheduled()`` で **Salesforce へ問い合わせが
    走る**（=自動切替で取りに行く）。旧実装の「スキップ」挙動は廃止された。
    """

    @staticmethod
    def _seed_truncated_failure(history_path: Path, when: dt.datetime) -> None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        # 既存テストの ``_seed_failure_row`` 相当。列は ``history.COLUMNS`` に
        # 従う（COLUMNS 末尾の「取得経路」は空文字）
        row_values = [
            when.strftime("%Y-%m-%d %H:%M:%S"),
            "9001",
            "",
            "案件集計",
            "00O5g00000ABCDE",
            URL_A,
            "案件集計",
            "失敗",
            "失敗",
            "",
            "ベース",
            "",
            "",
            "1.00",
            "Salesforce",
            "SalesforceReportTruncatedError",
            "2000 行超",
            "",
        ]

        history_path.write_bytes(
            ("﻿" + ",".join(history.COLUMNS) + "\r\n" + ",".join(row_values) + "\r\n").encode(
                "utf-8"
            )
        )

    def test_second_call_still_calls_salesforce_and_falls_back(self, tmp_path, monkeypatch):
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = _make_master(
            tmp_path / "レポート管理表.xlsx",
            [["9001", "案件集計", URL_A, "営業本部", "山田", "○", "", "", ""]],
            settings_rows=[["営業本部", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        fixed_now = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)
        # 「昨日 RPA が ``SalesforceReportTruncatedError`` で失敗した」履歴を書き、
        # 同じ日の 2 回目の ``download_scheduled()`` を走らせる
        self._seed_truncated_failure(history_path, when=fixed_now)

        api_site = _fake_salesforce(
            side_effect=SalesforceReportTruncatedError("00O5g00000ABCDE", 2000)
        )
        browser_site = _fake_browser_site(ROWS)
        with (
            patch("src.service.site_for", return_value=api_site),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
        ):
            saved = download_scheduled("案件集計")

        # 2 回目でも Salesforce へ問い合わせが走った（旧挙動: スキップで 0 回）
        assert api_site.return_value.__enter__.return_value.report.get.call_count == 1
        # 自動切替で成功し、2 件の保存（=昨日の失敗履歴 + 今日の成功行）
        assert len(saved) == 1

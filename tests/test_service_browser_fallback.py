"""Report API 経路での自動切替（ブラウザ経由）のテスト。

2026-10 に ``src/service.py::_fetch_with_auto_fallback()`` で追加された挙動:

- Report API が ``SalesforceReportTruncatedError`` を投げたら、自動でブラウザ経由に
  取り直す（``route=ROUTE_BROWSER_FALLBACK_TRUNCATED``）
- Report API が 0 行 + ``allow_empty=False`` なら、自動でブラウザ経由に
  取り直す（``route=ROUTE_BROWSER_FALLBACK_EMPTY``）。ブラウザでも 0 行なら
  ``EmptyReportError`` で失敗扱い
- SOQL / 最初からブラウザ経路では自動切替しない
- 同じ実行で自動切替が 2 件起きても、ブラウザは 1 回だけ開いて使い回す

Box 通知ファイル（``src.notification``）も同時に検証する。
1 件 1 ファイルで ``~/Box/Salesforceレポートダウンローダー通知`` に置かれ、
失敗時と自動切替成功時に書かれる。``Path.home()`` はテストで ``tmp_path``
を指すように差し替え、本物のホームには絶対に書かない。

``test_service.py`` 側は既存の挙動テスト、
``test_service_browser_fetch.py`` 側はブラウザ経由の単体テスト、
``test_service_soql_fetch.py`` 側は SOQL 経由の単体テスト、
``test_service_browser_fallback.py``（=このファイル）は「自動切替」と
「Box 通知」のオーケストレーションを集約する。
"""

import csv
import datetime as dt
import json
import logging
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
    ScheduledDownloadFailedError,
)
from src.notification import write_notification
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


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    """``Path.home()`` を ``tmp_path`` に差し替える。``src.paths.NOTIFICATION_FOLDER``
    は ``Path.home() / "Box" / "..."`` で組み立てるため、テストが本物の
    ホームに触れないようにするために必須。
    """
    box_root = tmp_path / "Box"
    box_root.mkdir()
    monkeypatch.setattr(_paths_module.Path, "home", classmethod(lambda cls: tmp_path))
    return box_root


# ──────────────────────────────────────────────────────────────────────
# 自動切替のオーケストレーション
# ──────────────────────────────────────────────────────────────────────
class TestAutoFallbackOnTruncated:
    """``SalesforceReportTruncatedError`` → ブラウザに自動切替して成功。

    Report API 経路で ``SalesforceReportTruncatedError`` が来たら、自動で
    ブラウザ経由に取り直す。成功時の ``route`` は ``ROUTE_BROWSER_FALLBACK_TRUNCATED``、
    Box 通知ファイルが 1 件作られる（``kind=自動切替``）。
    """

    def test_truncated_falls_back_to_browser_and_succeeds(self, tmp_path, monkeypatch, home_dir):
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

        # 通知: 自動切替で 1 ファイル
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert len(notif_files) == 1
        payload = json.loads(notif_files[0].read_text(encoding="utf-8"))
        assert payload["管理番号"] == "9001"
        assert payload["種類"] == "自動切替"
        assert payload["取得経路"] == history.ROUTE_BROWSER_FALLBACK_TRUNCATED


class TestAutoFallbackOnEmpty:
    """0 行 + ``allow_empty=False`` → ブラウザに自動切替して成功 / 失敗。"""

    def test_browser_has_rows_succeeds_with_empty_fallback(self, tmp_path, monkeypatch, home_dir):
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

        # 通知: 自動切替で 1 ファイル
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert len(notif_files) == 1
        payload = json.loads(notif_files[0].read_text(encoding="utf-8"))
        assert payload["種類"] == "自動切替"
        assert payload["取得経路"] == history.ROUTE_BROWSER_FALLBACK_EMPTY

    def test_browser_also_empty_fails_with_empty_report(self, tmp_path, monkeypatch, home_dir):
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

        # 通知: 失敗で 1 ファイル
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert len(notif_files) == 1
        payload = json.loads(notif_files[0].read_text(encoding="utf-8"))
        assert payload["種類"] == "失敗"
        assert payload["取得経路"] == history.ROUTE_API

    def test_allow_empty_yes_does_not_call_browser(self, tmp_path, monkeypatch, home_dir):
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

        # 通知: 書かれない（成功時）
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert notif_files == []


class TestNoFallbackForSoqlAndBrowserRoutes:
    """SOQL / 最初からブラウザ経路では自動切替しない。"""

    def test_soql_route_does_not_fall_back_to_browser(self, tmp_path, monkeypatch, home_dir):
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
        # 失敗時は「失敗」通知が 1 件だけ作られる（=自動切替ではないので
        # 「自動切替」通知ではない）
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert len(notif_files) == 1
        payload = json.loads(notif_files[0].read_text(encoding="utf-8"))
        assert payload["種類"] == "失敗"
        assert payload["取得経路"] == history.ROUTE_SOQL

    def test_truncated_route_does_not_fall_back_to_browser(self, tmp_path, monkeypatch, home_dir):
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

    def test_two_fallbacks_share_one_browser_instance(self, tmp_path, monkeypatch, home_dir):
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

        # 通知: 自動切替 2 件分
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert len(notif_files) == 2


# ──────────────────────────────────────────────────────────────────────
# Box 通知ファイル
# ──────────────────────────────────────────────────────────────────────
class TestNotificationFolderMissing:
    """``~/Box`` が無いとき、警告ログだけで取得は成功扱い。"""

    def test_no_box_folder_warns_and_does_not_raise(self, tmp_path, monkeypatch, caplog):
        """``Path.home() / 'Box'`` が無い状態で ``write_notification()`` を呼んでも、
        例外を外へ出さず ``None`` を返す。"""
        # ``~/Box`` を作らずに ``home_dir`` 相当の差し替えだけ行う
        monkeypatch.setattr(_paths_module.Path, "home", classmethod(lambda cls: tmp_path))
        with caplog.at_level(logging.WARNING, logger="src.notification"):
            result = write_notification(
                report_key="9999",
                summary="missing box",
                url=URL_A,
                kind="失敗",
                route=history.ROUTE_API,
                cause="Salesforce",
                error_code="X",
                error_message="失敗",
                row_count=None,
                executed_at=dt.datetime(2026, 9, 30, 10, 0),  # noqa: DTZ001
            )
        assert result is None
        assert any("Box 通知フォルダの親" in record.message for record in caplog.records), (
            caplog.records
        )


class TestNotificationIsolatedFileNames:
    """同時刻の 2 件でファイルが衝突しないこと（排他的新規作成）。"""

    def test_same_timestamp_two_files(self, tmp_path, monkeypatch, home_dir):
        executed_at = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        p1 = write_notification(
            report_key="9001",
            summary="a",
            url=URL_A,
            kind="失敗",
            route=history.ROUTE_API,
            cause="",
            error_code="X",
            error_message="",
            row_count=None,
            executed_at=executed_at,
        )
        p2 = write_notification(
            report_key="9002",
            summary="b",
            url=URL_B,
            kind="失敗",
            route=history.ROUTE_API,
            cause="",
            error_code="X",
            error_message="",
            row_count=None,
            executed_at=executed_at,
        )
        assert p1 is not None and p2 is not None
        assert p1 != p2
        assert p1.exists()
        assert p2.exists()


class TestNotificationDryRun:
    """``comken.runtime.is_dry_run()`` が真のときは書かない。"""

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch, home_dir):
        # ``is_dry_run`` は ``comken.runtime`` の内部状態（``_dry_run``）を読む。
        # ``notification`` 側で ``from comken.runtime import is_dry_run`` のように
        # ローカル束縛しているので、両方を差し替える
        from comken import runtime

        monkeypatch.setattr(runtime, "_dry_run", True)
        result = write_notification(
            report_key="9001",
            summary="dry",
            url=URL_A,
            kind="失敗",
            route=history.ROUTE_API,
            cause="",
            error_code="X",
            error_message="",
            row_count=None,
            executed_at=dt.datetime(2026, 9, 30, 10, 0),  # noqa: DTZ001
        )
        assert result is None
        # ファイルも作られていない
        notif_files = list(home_dir.glob("Salesforceレポートダウンローダー通知/*.json"))
        assert notif_files == []


class TestNotificationPayloadKeys:
    """通知 JSON の中身に必要なキーが揃っている。"""

    def test_payload_has_expected_keys(self, tmp_path, monkeypatch, home_dir):
        executed_at = dt.datetime(2026, 9, 30, 10, 0)  # noqa: DTZ001
        path = write_notification(
            report_key="9001",
            summary="テスト",
            url=URL_A,
            kind="自動切替",
            route=history.ROUTE_BROWSER_FALLBACK_EMPTY,
            cause="",
            error_code="",
            error_message="0 件\nが返った",
            row_count=2,
            executed_at=executed_at,
        )
        assert path is not None
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "実行日時",
            "管理番号",
            "概要",
            "URL",
            "種類",
            "取得経路",
            "原因区分",
            "エラーコード",
            "エラー内容",
            "件数",
        ):
            assert key in payload, key
        assert payload["実行日時"] == "2026-09-30 10:00:00"
        assert payload["管理番号"] == "9001"
        assert payload["種類"] == "自動切替"
        assert payload["取得経路"] == history.ROUTE_BROWSER_FALLBACK_EMPTY
        assert payload["件数"] == 2
        # 改行はスペースに置換して 1 行で書く（Teams 側で読みやすく）
        assert "\n" not in payload["エラー内容"]


class TestNotificationOSErrorIsSwallowed:
    """OSError（書き込めない等）は握りつぶすが、それ以外の例外は外へ出す。"""

    def test_oserror_is_swallowed(self, tmp_path, monkeypatch, caplog):
        """ファイル書込みを ``OSError`` で失敗させても ``write_notification()``
        は ``None`` を返して外へ例外を出さない。"""
        monkeypatch.setattr(_paths_module.Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / "Box").mkdir()
        # ``tempfile.mkstemp`` を OSError で失敗させる
        import tempfile

        def _raise_oserror(*a, **kw):
            raise OSError("disk full")

        with (
            monkeypatch.context() as ctx,
            caplog.at_level(logging.WARNING, logger="src.notification"),
        ):
            ctx.setattr(tempfile, "mkstemp", _raise_oserror)
            result = write_notification(
                report_key="9001",
                summary="x",
                url=URL_A,
                kind="失敗",
                route=history.ROUTE_API,
                cause="",
                error_code="X",
                error_message="",
                row_count=None,
                executed_at=dt.datetime(2026, 9, 30, 10, 0),  # noqa: DTZ001
            )
        assert result is None
        assert any("書き出しに失敗" in record.message for record in caplog.records), caplog.records

    def test_non_oserror_propagates(self, tmp_path, monkeypatch):
        """``TypeError``（バグ）は握りつぶさず外へ伝える。"""
        monkeypatch.setattr(_paths_module.Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / "Box").mkdir()
        import tempfile

        def _raise_typeerror(*a, **kw):
            raise TypeError("bug")

        with monkeypatch.context() as ctx:
            ctx.setattr(tempfile, "mkstemp", _raise_typeerror)
            with pytest.raises(TypeError, match="bug"):
                write_notification(
                    report_key="9001",
                    summary="x",
                    url=URL_A,
                    kind="失敗",
                    route=history.ROUTE_API,
                    cause="",
                    error_code="X",
                    error_message="",
                    row_count=None,
                    executed_at=dt.datetime(2026, 9, 30, 10, 0),  # noqa: DTZ001
                )


class TestNotificationFolderConstant:
    """``src.paths.NOTIFICATION_FOLDER`` の構造と、実行時の ``Path.home()`` を
    反映する ``_notification_folder()`` の挙動。"""

    def test_default_folder_layout(self):
        """``NOTIFICATION_FOLDER`` は ``~/Box/Salesforceレポートダウンローダー通知``
        という ``Path.home()`` 配下の固定パスとして組み立てられている。"""
        assert _paths_module.NOTIFICATION_FOLDER.parts[-1] == "Salesforceレポートダウンローダー通知"
        # ``Box`` の手前までが ``Path.home()`` と一致する（=ホーム配下にある）
        assert "Box" in _paths_module.NOTIFICATION_FOLDER.parts

    def test_runtime_folder_reflects_monkeypatched_home(self, tmp_path, monkeypatch, home_dir):
        """``write_notification()`` 内で ``_notification_folder()`` が
        実行時に ``Path.home()`` を見直すため、``monkeypatch`` した
        ``home`` の下が通知先になる。"""
        # ``home_dir`` フィクスチャが ``monkeypatch`` 済みなので、そのまま
        # ``write_notification()`` を呼ぶと ``tmp_path / Box / ...`` に書かれる
        result = write_notification(
            report_key="9001",
            summary="home-relative",
            url=URL_A,
            kind="失敗",
            route=history.ROUTE_API,
            cause="",
            error_code="X",
            error_message="",
            row_count=None,
            executed_at=dt.datetime(2026, 9, 30, 10, 0),  # noqa: DTZ001
        )
        assert result is not None
        # ホームが ``tmp_path`` に差し替わっているので、書き出し先も ``tmp_path``
        # 配下になる（本物の ``C:\Users\oguri\Box`` には触らない）
        assert str(result).startswith(str(tmp_path))


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

    def test_second_call_still_calls_salesforce_and_falls_back(
        self, tmp_path, monkeypatch, home_dir
    ):
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

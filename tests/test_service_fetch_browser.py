"""``fetch_browser`` を外から渡してブラウザで取る仕組みのテスト。

ブラウザ経由の取得は別リポジトリ ``salesforce-report-browser`` の担当で、
本体は ``download_scheduled(fetch_browser=...)`` の引数で「1 件取りに行く関数」を
受け取るだけ。ここでは次の 4 点を確かめる。

1. ``fetch_browser`` を渡さない実行では、管理表の「2000件超」が ○ の行は取らない
   （Report API も呼ばない / 履歴にも書かない / 他の行は通常どおり取る）
2. ``fetch_browser`` を渡した実行では、**対象選定で選ばれた行が全部** ブラウザで
   取られる（経路が SOQL でも通常でも関係なく、履歴の「取得経路」は全部
   ``ROUTE_BROWSER``）
3. 当日すでに成功履歴がある行は ``fetch_browser`` を渡しても取らない
   （ブラウザは本体の取り残しだけを取る）
4. ``fetch_browser`` と実行時フィルタを同時に渡すと、接続前に ``ValueError``
"""

import datetime as dt
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.exceptions import LoginFailedError, SalesforceReportTruncatedError
from comken.services.salesforce_downloader.history import (
    COLUMNS,
    ROUTE_API,
    ROUTE_BROWSER,
    SUCCESS,
    append_history,
)
from comken.toolbox.csv import CSV
from comken.toolbox.excel import Excel

import src.paths as paths_module
from src.exceptions import ScheduledDownloadFailedError
from src.service import attempt as attempt_module
from src.service import download_scheduled

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"
URL_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000FGHIJ/view"
URL_C = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000KLMNO/view"
ROWS = [{"名前": "山田", "金額": "100"}, {"名前": "鈴木", "金額": "200"}]

MASTER_HEADERS = [
    "ID",
    "グループ",
    "担当者",
    "概要",
    "Salesforce URL",
    "有効",
    "0件あり",
    "2000件超",
    "SOQL",
    "上書き",
]
SCHEDULE_HEADERS = [
    "スケジュールキー",
    "レポートキー",
    "取得頻度",
    "取得開始時刻",
    "取得時刻",
    "曜日",
    "日付",
    "祝日対応",
    "有効",
]
GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]

# 管理表の 1 行。列の並びは ``MASTER_HEADERS`` と同じ。
# 「有効」が ○ の行には「毎日・時刻なし」のスケジュール行が 1 件付く
# （``download_scheduled()`` は「スケジュール」シートに行が無いレポートを
# 取らないため）。スケジュールキーは ``S{管理番号}`` になる
BROWSER_ROW = [
    "1001",
    "営業事務グループ",
    "山田",
    "大きなレポート",
    URL_A,
    "○",  # 有効
    "",  # 0件あり
    "○",  # 2000件超（ブラウザ経由の行）
    "",  # SOQL
    "",  # 上書き
]
API_ROW = [
    "1002",
    "営業事務グループ",
    "佐藤",
    "普通のレポート",
    URL_B,
    "○",
    "",
    "",  # 2000件超
    "",  # SOQL
    "",
]
SOQL_ROW = [
    "1003",
    "営業事務グループ",
    "鈴木",
    "SOQLのレポート",
    URL_C,
    "○",
    "",
    "",  # 2000件超
    "○",  # SOQL
    "",
]


def _make_master(path: Path, master_rows: list[list[str]], *, settings_rows) -> Path:
    """管理表（Excel）を作る。「管理表」「スケジュール」「設定」の 3 シート。

    ``master_rows`` の各行は ``MASTER_HEADERS`` と同じ長さのリスト。
    「有効」が ○ の行には「毎日・時刻なし」のスケジュール行を 1 件ずつ当てる
    （``download_scheduled()`` は「スケジュール」シートに行が無いレポートを
    取らないため）。
    """
    table_rows = [dict(zip(MASTER_HEADERS, row, strict=True)) for row in master_rows]
    schedule_rows = [
        dict(
            zip(
                SCHEDULE_HEADERS,
                [
                    f"S{row[0]}",
                    row[0],
                    "毎日",
                    "",
                    "",
                    "",
                    "",
                    "取得しない",
                    "○",
                ],
                strict=True,
            )
        )
        for row in master_rows
        if row[5] == "○"
    ]
    settings_table_rows = [
        dict(zip(GROUP_SETTINGS_HEADERS, row, strict=True)) for row in settings_rows
    ]
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table("管理表", Table(MASTER_HEADERS, table_rows))
        book.create_data_sheet("スケジュール").create_table(
            "スケジュール", Table(SCHEDULE_HEADERS, schedule_rows)
        )
        book.create_data_sheet("設定").create_table(
            "設定", Table(GROUP_SETTINGS_HEADERS, settings_table_rows)
        )
    return path


def _make_env(tmp_path, monkeypatch, master_rows: list[list[str]]) -> dict:
    """管理表・履歴・保存先を用意して、返り値に各パスを入れる。"""
    base_path = tmp_path / "ベース"
    base_path.mkdir()
    master = _make_master(
        tmp_path / "レポート管理表.xlsx",
        master_rows,
        settings_rows=[["営業事務グループ", str(base_path)]],
    )
    history_path = tmp_path / "ダウンロード履歴.csv"
    monkeypatch.setattr(paths_module, "MASTER_PATH", master)
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path)
    fixed_now = dt.datetime(2026, 11, 12, 10, 0)  # noqa: DTZ001
    monkeypatch.setattr(attempt_module, "clock_now", lambda: fixed_now)
    monkeypatch.setattr(paths_module, "clock_now", lambda: fixed_now)
    return {
        "master_path": master,
        "history_path": history_path,
        "base_path": base_path,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """管理表・履歴・保存先を用意して、ブラウザ行（1001）と API 行（1002）を用意する。

    - ``1001`` …「2000件超」= ○（ブラウザ経由の行）
    - ``1002`` …「2000件超」= ×（通常の Report API の行）
    """
    return _make_env(tmp_path, monkeypatch, [BROWSER_ROW, API_ROW])


@pytest.fixture
def env_with_soql(tmp_path, monkeypatch):
    """SOQL の行（1003）を足した管理表。

    - ``1001`` …「2000件超」= ○（ブラウザ経由の行）
    - ``1002`` …「2000件超」= ×（通常の Report API の行）
    - ``1003`` …「SOQL」= ○（SOQL 経由の行）
    """
    return _make_env(tmp_path, monkeypatch, [BROWSER_ROW, API_ROW, SOQL_ROW])


def _fake_salesforce(rows=None) -> MagicMock:
    """report.get() が Table を返す Salesforce クライアント。"""
    values = ROWS if rows is None else rows
    client = MagicMock()
    client.__enter__.return_value.report.get.return_value = Table(["名前", "金額"], values)
    return MagicMock(return_value=client)


def _history_rows(env: dict) -> list[dict]:
    """履歴 CSV を読み返す（無ければ空リスト）。"""
    if not env["history_path"].exists():
        return []
    with CSV(env["history_path"], read_only=True) as csv_file:
        return csv_file.read().to_rows()


def _write_success_history(history_path: Path, *, key: str, schedule_key: str) -> None:
    """履歴 CSV に「そのスケジュールキーで当日成功済み」の 1 行を足す。

    当日取得済の判定（``schedule_succeeded_today()``）が効くように、
    「実行日時」が当日（2026-11-12）で「成否」「保存結果」が成功の行を書く。
    """
    values: dict[str, object] = dict.fromkeys(COLUMNS, "")
    values.update(
        {
            "管理番号": key,
            "スケジュールキー": schedule_key,
            "成否": SUCCESS,
            "Salesforce取得結果": SUCCESS,
            "保存結果": SUCCESS,
            "取得経路": ROUTE_API,
        }
    )
    append_history(
        history_path,
        values,
        executed_at=dt.datetime(2026, 11, 12, 9, 0),  # noqa: DTZ001
    )


class TestWithoutFetchBrowser:
    """``fetch_browser`` を渡さない実行（本体の通常の実行）。"""

    def test_browser_row_is_not_fetched_at_all(self, env):
        """「2000件超」= ○ の行は取らない。Report API も呼ばず、履歴にも書かない。

        他の行（API 行）は通常どおり取れる。
        """
        site = _fake_salesforce()
        with patch("src.service.api.site_for", return_value=site):
            saved = download_scheduled()

        report_get = site.return_value.__enter__.return_value.report.get
        # API 行（1002）だけ問い合わせられている（1001 は 1 回も呼ばない）
        assert report_get.call_count == 1
        report_get.assert_called_once_with("00O5g00000FGHIJ")
        # 保存も API 行だけ
        assert [path.name.split("_")[0] for path in saved] == ["1002"]
        # 履歴にも API 行の 1 行だけ（ブラウザ行は書かない）
        rows = _history_rows(env)
        assert [row["管理番号"] for row in rows] == ["1002"]

    def test_browser_row_is_skipped_with_one_info_log(self, env, caplog):
        """飛ばしたブラウザ行は 1 実行 1 行の INFO ログで知らせる。"""
        with (
            caplog.at_level(logging.INFO, logger="src.service"),
            patch("src.service.api.site_for", return_value=_fake_salesforce()),
        ):
            download_scheduled()

        messages = [
            record.getMessage()
            for record in caplog.records
            if "salesforce-report-browser" in record.getMessage()
        ]
        assert len(messages) == 1
        assert "1001" in messages[0]


class TestWithFetchBrowser:
    """``fetch_browser`` を渡した実行（別リポジトリからの実行）。"""

    def test_all_selected_rows_are_fetched_by_browser(self, env_with_soql):
        """対象選定で選ばれた行は **全部** ``fetch_browser`` で取る。

        通常行（1002）・SOQL の行（1003）・「2000件超」○ の行（1001）のすべてが
        ブラウザで取られ、Report API（``report.get``）も SOQL（``bulk_query`` /
        ``query``）も呼ばれない。履歴の「取得経路」は全部 ``ROUTE_BROWSER``。
        """
        called: list = []

        def _fetch_browser(entry):
            called.append(entry)
            return Table(["名前", "金額"], ROWS)

        with (
            patch("src.service.api.site_for") as api_site,
            patch("src.service.soql.site_for") as soql_site,
            patch("src.service.soql.soql_report_for") as soql_report_for,
        ):
            saved = download_scheduled(fetch_browser=_fetch_browser)

        # 選ばれた 3 行すべてが fetch_browser で取られている
        assert [entry.key for entry in called] == ["1001", "1002", "1003"]
        # Report API も SOQL も呼ばない（SOQL の登録確認も通さない）
        api_site.assert_not_called()
        soql_site.assert_not_called()
        soql_report_for.assert_not_called()
        # 保存も 3 件
        assert sorted(path.name.split("_")[0] for path in saved) == ["1001", "1002", "1003"]
        assert all(path.is_file() for path in saved)
        # 履歴の取得経路はすべてブラウザ
        rows = _history_rows(env_with_soql)
        assert [row["管理番号"] for row in rows] == ["1001", "1002", "1003"]
        assert [row["取得経路"] for row in rows] == [ROUTE_BROWSER] * 3
        assert [row["成否"] for row in rows] == [SUCCESS] * 3

    def test_already_succeeded_today_is_not_fetched_again(self, env):
        """当日すでに成功履歴がある行は、``fetch_browser`` を渡しても取らない。

        対象選定（スケジュール・当日成功済みの判定）で外れた行は
        取り残しではないので、ブラウザでも取り直さない。
        """
        _write_success_history(env["history_path"], key="1002", schedule_key="S1002")

        called: list = []

        def _fetch_browser(entry):
            called.append(entry)
            return Table(["名前", "金額"], ROWS)

        with patch("src.service.api.site_for") as api_site:
            saved = download_scheduled(fetch_browser=_fetch_browser)

        # 1002 は当日成功済みなので対象外（取り残しだけを取る）
        assert [entry.key for entry in called] == ["1001"]
        api_site.assert_not_called()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]
        # 履歴は「前回分の 1002」＋「今回取った 1001」の 2 行
        rows = _history_rows(env)
        assert [row["管理番号"] for row in rows] == ["1002", "1001"]
        assert rows[1]["取得経路"] == ROUTE_BROWSER

    def test_filters_with_fetch_browser_raise_before_connecting(self, env):
        """``fetch_browser`` と実行時フィルタを同時に渡すと ``ValueError``。

        フィルタはブラウザでは使えない。**接続前に**落とすので、
        ``fetch_browser`` も Salesforce も呼ばれない。
        """
        called: list = []

        def _fetch_browser(entry):
            called.append(entry)
            return Table(["名前", "金額"], ROWS)

        with (
            patch("src.service.api.site_for") as api_site,
            pytest.raises(ValueError, match="実行時フィルタ"),
        ):
            download_scheduled(
                filters_by_report={"1002": [{"column": "x", "operator": "equals", "value": "1"}]},
                fetch_browser=_fetch_browser,
            )

        assert called == []
        api_site.assert_not_called()
        # 履歴にも何も残さない（対象選定に入る前）
        assert _history_rows(env) == []

    def test_fetch_browser_failure_is_recorded(self, env):
        """``fetch_browser`` が例外を出すと、そのレポートだけ失敗として残る。"""

        def _fetch_browser(entry):
            if entry.key == "1001":
                raise LoginFailedError("MFA が承認されませんでした")
            return Table(["名前", "金額"], ROWS)

        with (
            patch("src.service.api.site_for", return_value=_fake_salesforce()),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled(fetch_browser=_fetch_browser)

        rows = _history_rows(env)
        assert [row["管理番号"] for row in rows] == ["1001", "1002"]
        assert rows[0]["成否"] == "失敗"
        assert rows[0]["エラーコード"] == "LoginFailedError"
        assert rows[0]["原因区分"] == "Salesforce"
        assert rows[0]["取得経路"] == ROUTE_BROWSER
        # 失敗したレポートのファイルは作られない（成功した 1002 のものは作られる）
        csvs = sorted(path.name.split("_")[0] for path in env["base_path"].glob("**/*.csv"))
        assert csvs == ["1002"]


class TestTruncatedIsNotRetried:
    """Report API の 2000 件超の打ち切りは取り直さずに失敗になる。"""

    def test_truncated_fails_without_browser_retry(self, env, caplog):
        """打ち切られたらそのまま失敗。管理表の「SOQL」か「2000件超」を
        `○` にするよう、ログで知らせる。
        """
        site = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.report.get.side_effect = SalesforceReportTruncatedError(
            "00O5g00000FGHIJ", 2000
        )
        site.return_value = client

        with (
            caplog.at_level(logging.INFO, logger="src.service"),
            patch("src.service.api.site_for", return_value=site),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()

        # Report API は 1 回だけ（取り直しはしない）
        assert client.__enter__.return_value.report.get.call_count == 1
        # 履歴に失敗が残る
        rows = _history_rows(env)
        assert [row["管理番号"] for row in rows] == ["1002"]
        assert rows[0]["成否"] == "失敗"
        assert rows[0]["エラーコード"] == "SalesforceReportTruncatedError"
        # 対処が分かる警告が出る
        assert "「SOQL」か「2000件超」を○にしてください" in caplog.text
        # ファイルは作られない
        assert list(env["base_path"].glob("**/*.csv")) == []

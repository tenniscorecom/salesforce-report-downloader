"""失敗時の履歴1行（``_failure_row()``）の「原因区分」と、履歴の3状態の対応を確かめる。

原因区分（``設定`` / ``Salesforce`` / ``データなし`` / ``ファイル`` / ``プログラム``）は
例外の型から機械的に決める。対応表の1行ずつを ``_failure_row()`` に渡して、
「Salesforce取得結果」「保存結果」「原因区分」の3つを確かめる。

グループ未登録（設定シートに無いグループ名）のレポートを定期取得で動かしたときの
履歴も、原因区分が ``設定`` になることを確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import ComkenError
from comken.toolbox.csv import CSV
from comken.toolbox.excel import Excel

from src.exceptions import (
    ColumnNamesCSVFormatError,
    EmptyReportError,
    GroupNotRegisteredError,
    OverwriteTimeMissingError,
    ReportFolderNotFoundError,
    SavedFileEmptyError,
    ScheduledDownloadFailedError,
    SoqlColumnNameConflictError,
    SoqlCountMismatchError,
    SoqlIncompleteError,
    SoqlReportNotRegisteredError,
    SoqlTransformError,
)
from src.service import download_scheduled
from src.service.attempt import _failure_row

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"

MASTER_HEADERS = ["ID", "グループ", "担当者", "概要", "Salesforce URL", "有効"]
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
SETTINGS_HEADERS = ["グループ", "ベースURL"]

_MASTER_PATH = Path("C:/共有/report-management.xlsx")


def _cause_cases() -> list[tuple[BaseException, bool | None, bool | None, str]]:
    """原因区分の対応表。``(例外, Salesforce取得結果, 保存結果, 原因区分)``。"""
    return [
        # 取得段階に入らない設定の誤り
        (ReportFolderNotFoundError("1001", Path("C:/out")), None, None, "設定"),
        (GroupNotRegisteredError("営業本部", [], _MASTER_PATH), None, None, "設定"),
        (SoqlReportNotRegisteredError("1001", []), None, None, "設定"),
        (SoqlIncompleteError("1001", "顧客一覧"), None, None, "設定"),
        (OverwriteTimeMissingError("1001", "S001"), None, None, "設定"),
        # 取得は終わったが、列名（見出し）に問題が起きた
        (
            SoqlColumnNameConflictError("1001", {"金額": ["Amount"]}),
            True,
            None,
            "設定",
        ),
        (ColumnNamesCSVFormatError("列名の対応.csv が読めない"), True, None, "設定"),
        # 取得は終わったが、SOQL の加工で問題が起きた
        (
            SoqlCountMismatchError("1001", "顧客一覧", 10, 20),
            True,
            None,
            "プログラム",
        ),
        (
            SoqlTransformError("1001", "顧客一覧", "HogeReport", "KeyError"),
            True,
            None,
            "プログラム",
        ),
        # 取得は成功したが 0 件
        (
            EmptyReportError("1001", "顧客一覧", URL_A),
            True,
            None,
            "データなし",
        ),
        # 保存は終わったが、ファイルが 0 バイトだった
        (
            SavedFileEmptyError("1001", "顧客一覧", Path("C:/out/1001.csv"), 10),
            True,
            False,
            "ファイル",
        ),
        # 保存段階の失敗（書き込み・権限・共有サーバー断）
        (OSError("共有サーバーへ書き込めません"), True, False, "ファイル"),
        # 表に無い ComkenError
        (ComkenError("認証に失敗しました"), False, None, "Salesforce"),
        # 想定外の例外
        (TypeError("想定外"), False, None, "プログラム"),
    ]


@pytest.mark.parametrize(("exc", "fetched", "saved", "cause"), _cause_cases())
def test_failure_row_matches_cause_table(
    exc: BaseException,
    fetched: bool | None,
    saved: bool | None,
    cause: str,
) -> None:
    """対応表の1行ずつで、取得結果・保存結果・原因区分が一致する。"""
    row = _failure_row(exc, 1.0, "S001")
    assert row.fetched_from_salesforce is fetched
    assert row.saved_to_file is saved
    assert row.cause == cause


def _make_master(
    path: Path,
    *,
    master_rows: list[list[str]],
    schedule_rows: list[list[str]],
    settings_rows: list[list[str]],
) -> Path:
    """管理表・スケジュール・設定の3シートを持つブックを作る。"""
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table(
            "管理表",
            Table(
                MASTER_HEADERS,
                [dict(zip(MASTER_HEADERS, row, strict=True)) for row in master_rows],
            ),
        )
        book.create_data_sheet("スケジュール").create_table(
            "スケジュール",
            Table(
                SCHEDULE_HEADERS,
                [dict(zip(SCHEDULE_HEADERS, row, strict=True)) for row in schedule_rows],
            ),
        )
        book.create_data_sheet("設定").create_table(
            "設定",
            Table(
                SETTINGS_HEADERS,
                [dict(zip(SETTINGS_HEADERS, row, strict=True)) for row in settings_rows],
            ),
        )
    return path


@pytest.fixture(autouse=True)
def _reset_group_settings_cache():
    """``src.paths`` のグループ設定キャッシュをテストごとに破棄する。"""
    import src.paths as paths_module

    paths_module.reset_group_settings_cache()
    try:
        yield
    finally:
        paths_module.reset_group_settings_cache()


def test_unregistered_group_is_recorded_as_config(tmp_path, monkeypatch) -> None:
    """グループ未登録のレポートは、履歴の「原因区分」が ``設定`` になる。

    出力先を決められないので取得段階の前で止まる（取得結果・保存結果は空）。
    """
    master = _make_master(
        tmp_path / "管理表.xlsx",
        master_rows=[["1001", "営業本部", "山田", "顧客一覧", URL_A, "○"]],
        schedule_rows=[["S001", "1001", "毎日", "09:00", "", "", "", "取得しない", "○"]],
        # 「営業本部」は設定シートに無い（別部署だけ登録している）
        settings_rows=[["経理グループ", str(tmp_path / "out")]],
    )
    history_path = tmp_path / "履歴.csv"
    monkeypatch.setattr("src.paths.MASTER_PATH", master)
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path)

    with pytest.raises(ScheduledDownloadFailedError):
        download_scheduled()

    with CSV(history_path) as csv_file:
        rows = csv_file.read()
    assert len(rows) == 1
    assert rows[0]["成否"] == "失敗"
    assert rows[0]["原因区分"] == "設定"
    assert rows[0]["エラーコード"] == "GroupNotRegisteredError"
    # 取得も保存も始まっていないので、両方とも空（未到達）
    assert rows[0]["Salesforce取得結果"] == ""
    assert rows[0]["保存結果"] == ""

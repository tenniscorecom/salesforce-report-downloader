"""src.sheets.master の ReportEntry 固有の列を検証する。

汎用の MasterRow / column() 機構は tests/test_master_table.py 側でカバーする。
ここでは ReportEntry に追加した列（`exceeds_row_limit` / `use_soql`）の読み取りと、
列が無い既存管理表でも既定値で読めること（後方互換）だけを見る。
"""

from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import SalesforceReportIDNotFoundError
from comken.toolbox.excel import Excel

from src.sheets.master import ReportEntry, load_master

URL = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"

HEADERS = [
    "ID",
    "グループ",
    "担当者",
    "概要",
    "Salesforce URL",
    "有効",
    "0件あり",
    "2000件超",
    "SOQL",
    "備考",
]

# 「2000件超」「SOQL」列を追加する前の古い管理表（後方互換の確認用）
LEGACY_HEADERS = [
    "ID",
    "グループ",
    "担当者",
    "概要",
    "Salesforce URL",
    "有効",
    "0件あり",
    "備考",
]


def make_master(path: Path, headers: list[str], rows: list[list]) -> Path:
    """管理表（Excel）を作る。"""
    table_rows = [dict(zip(headers, row, strict=True)) for row in rows]
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table("管理表", Table(headers, table_rows))
    return path


def _row(*, exceeds: str, soql: str) -> list:
    return [
        "1001",
        "営業本部",
        "山田太郎",
        "顧客一覧",
        URL,
        "○",
        "×",
        exceeds,
        soql,
        "",
    ]


class TestExceedsRowLimitAndUseSoql:
    """「2000件超」「SOQL」列の読み取り。"""

    def test_reads_maru_as_true(self, tmp_path):
        master = make_master(tmp_path / "管理表.xlsx", HEADERS, [_row(exceeds="○", soql="○")])
        entry = load_master(master)["1001"]
        assert entry.exceeds_row_limit is True
        assert entry.use_soql is True

    def test_reads_batsu_as_false(self, tmp_path):
        master = make_master(tmp_path / "管理表.xlsx", HEADERS, [_row(exceeds="×", soql="×")])
        entry = load_master(master)["1001"]
        assert entry.exceeds_row_limit is False
        assert entry.use_soql is False

    def test_missing_columns_default_to_false(self, tmp_path):
        """列が無い既存の管理表でも読める（後方互換）。既定は両方「×」相当。"""
        rows = [
            [
                "1001",
                "営業本部",
                "山田太郎",
                "顧客一覧",
                URL,
                "○",
                "×",
                "",
            ]
        ]
        master = make_master(tmp_path / "管理表.xlsx", LEGACY_HEADERS, rows)
        entry = load_master(master)["1001"]
        assert entry.exceeds_row_limit is False
        assert entry.use_soql is False


class TestInvalidReportUrl:
    """管理表の URL が壊れていると、行番号ではなく管理番号で示して止まる。"""

    def test_error_names_the_management_number(self, tmp_path):
        row = _row(exceeds="×", soql="×")
        row[4] = "https://example.com/"  # URL 列
        rows = [row]
        master = make_master(tmp_path / "管理表.xlsx", HEADERS, rows)
        with pytest.raises(SalesforceReportIDNotFoundError, match="管理番号 1001"):
            load_master(master)


class TestDirectConstruction:
    """ReportEntry を直接組み立てるとき（テストの他の場所で使う形）も既定値が効く。"""

    def test_defaults_when_omitted(self):
        entry = ReportEntry(
            key="1001",
            summary="顧客一覧",
            url=URL,
            group="営業本部",
            assignee="山田太郎",
            enabled=True,
            allow_empty=False,
        )
        assert entry.exceeds_row_limit is False
        assert entry.use_soql is False

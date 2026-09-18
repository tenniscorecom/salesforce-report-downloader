"""``src/salesforce_downloader.template_writer`` の雛形生成を検証する。

雛形と読み込みで列がズレないこと、`choices` 列にドロップダウンが付くこと、
フォントが Noto Sans JP に揃うこと、記入例に背景色が付き「Sheet」が残らないこと、
``create_combined_workbook`` で3シート＋記入方法シートが1ブックにまとまること
を確認する。
"""

from pathlib import Path

import comken.services.salesforce_downloader.paths as _paths_module
import pytest
from comken.core.table import Table
from comken.exceptions import SheetNotFoundError
from comken.services.salesforce_downloader.sheets.group_settings import GroupSetting
from comken.services.salesforce_downloader.sheets.master import EXAMPLES, ReportEntry, load_master
from comken.services.salesforce_downloader.sheets.schedule import (
    SCHEDULE_SHEET_NAME,
    ScheduleRule,
    load_schedule,
)
from comken.toolbox.excel import Excel
from openpyxl import load_workbook

from src.salesforce_downloader.template_writer import (
    GROUP_SETTING_EXAMPLES,
    REPORT_ENTRY_GUIDE_INTRO,
    SCHEDULE_EXAMPLES,
    apply_schedule_dropdowns,
    create_combined_workbook,
    create_template,
)

SCHEDULE_HEADERS = [
    "スケジュールキー",
    "レポートキー",
    "取得頻度",
    "取得時刻",
    "曜日",
    "日付",
    "祝日対応",
    "有効",
]
SETTINGS_HEADERS = ["グループ", "ベースURL"]


# ── 1クラス分の雛形 ─────────────────────────────────────────────────────


class TestCreateTemplateReportEntry:
    """`ReportEntry` の雛形が読み込みと往復できるか。"""

    def test_generated_template_can_be_loaded(self, tmp_path):
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        entries = load_master(path)
        assert list(entries) == ["1001", "1002"]

    def test_headers_are_written_in_declaration_order(self, tmp_path):
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry)
        sheet = load_workbook(path)["PY_管理表"]
        headers = [cell.value for cell in sheet[1]]
        # ReportEntry の宣言順（9291 向け「出力ファイル名」「保存方式」を含む）
        assert headers[:8] == [
            "ID",
            "概要",
            "Salesforce URL",
            "グループ",
            "担当者",
            "出力ファイル名",
            "保存方式",
            "有効",
        ]

    def test_examples_point_at_different_reports(self, tmp_path):
        """記入例が同じレポートを指していると、check が重複として報告してしまう。"""
        from comken.services.salesforce_downloader.sheets.master import shared_report_ids

        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        entries = load_master(path)
        assert shared_report_ids(entries) == {}

    def test_guide_sheet_is_included_with_intro(self, tmp_path):
        """非エンジニアが1枚で分かるよう、記入方法のシートを付け、先頭に案内を置く。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        wb = load_workbook(path)
        assert "記入方法" in wb.sheetnames
        guide_text = "\n".join(str(c.value) for row in wb["記入方法"].iter_rows() for c in row)
        assert REPORT_ENTRY_GUIDE_INTRO in guide_text

    def test_choice_columns_get_dropdown(self, tmp_path):
        """`choices` のある列には Excel のドロップダウン（入力規則）が付く。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        ws = load_workbook(path)["PY_管理表"]
        ranges = sorted(str(v.sqref) for v in ws.data_validations.dataValidation)
        # ReportEntry の `choices` 列は「保存方式」「有効」「0件あり」「2000件超」「SOQL」
        # の 5 つ。新2列追加で列位置が ``+2`` ずれている（保存方式=7 列目、有効=8 列目）
        # 最下行は _FIRST_DATA_ROW(2) + 例(2) - 1 + _DATA_VALIDATION_ROWS(1000) = 1003
        assert "G2:G1003" in ranges  # 保存方式
        assert "H2:H1003" in ranges  # 有効
        assert "I2:I1003" in ranges  # 0件あり
        assert "J2:J1003" in ranges  # 2000件超
        assert "K2:K1003" in ranges  # SOQL

    def test_template_font_is_noto_sans_jp(self, tmp_path):
        """雛形（表シート・記入方法シートとも）のフォントが Noto Sans JP。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        wb = load_workbook(path)
        assert wb["PY_管理表"]["A1"].font.name == "Noto Sans JP"
        for row in wb["記入方法"].iter_rows(min_row=1, max_row=10):
            for cell in row:
                assert cell.font.name == "Noto Sans JP"

    def test_example_rows_are_marked_with_fill(self, tmp_path):
        """記入例全体に薄い背景色が付く。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        ws = load_workbook(path)["PY_管理表"]
        fg = ws["A2"].fill.fgColor.value or ws["A2"].fill.fgColor.rgb
        assert fg is not None
        assert fg.endswith("D9D9D9") or fg == "00D9D9D9" or fg == "FFD9D9D9"

    def test_create_template_does_not_leave_default_sheet(self, tmp_path):
        """openpyxl が自動で作る「Sheet」が雛形に残らないこと。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry)
        assert "Sheet" not in load_workbook(path).sheetnames

    def test_table_is_created_in_template(self, tmp_path):
        """Excel のテーブルにしておくと、行を足すのが楽になる。"""
        path = create_template(tmp_path / "管理表.xlsx", ReportEntry, EXAMPLES)
        assert "PY_T_ReportEntry" in load_workbook(path)["PY_管理表"].tables


class TestCreateTemplateGroupSetting:
    """`GroupSetting` の雛形がそのまま読み込めるか。"""

    def test_generated_template_can_be_loaded(self, tmp_path):
        path = create_template(tmp_path / "設定.xlsx", GroupSetting, GROUP_SETTING_EXAMPLES)
        rows = GroupSetting.load(path)
        assert len(rows) == 1
        assert rows[0].group == "営業本部"
        assert rows[0].base_path == Path(r"\\server\share\営業本部")

    def test_unique_group_constraint_is_preserved(self, tmp_path):
        """`unique=True` の列が読み込み側の検証で生きている（同じ値の重複を弾ける）。"""
        from comken.exceptions import MasterDuplicateValueError

        path = create_template(
            tmp_path / "設定.xlsx",
            GroupSetting,
            [
                {"group": "営業本部", "base_path": Path(r"\\server\share\営業本部")},
                {"group": "営業本部", "base_path": Path(r"\\server\share\営業本部B")},
            ],
        )
        with pytest.raises(MasterDuplicateValueError):
            GroupSetting.load(path)


class TestCreateTemplateScheduleRule:
    """`ScheduleRule` の雛形がそのまま読み込めるか。"""

    def test_generated_template_can_be_loaded(self, tmp_path):
        path = create_template(
            tmp_path / "スケジュール.xlsx", ScheduleRule, SCHEDULE_EXAMPLES
        )
        rules = ScheduleRule.load(path)
        assert [r.schedule_key for r in rules] == ["S001"]
        assert rules[0].report_key == "1001"
        assert rules[0].enabled is True

    def test_choice_columns_get_dropdown(self, tmp_path):
        """`choices` 列（「取得頻度」「有効」）だけにドロップダウンが付く。"""
        path = create_template(
            tmp_path / "スケジュール.xlsx", ScheduleRule, SCHEDULE_EXAMPLES
        )
        ws = load_workbook(path)[f"PY_{SCHEDULE_SHEET_NAME}"]
        ranges = sorted(str(v.sqref) for v in ws.data_validations.dataValidation)
        # 取得頻度=C列, 有効=H列
        assert "C2:C1002" in ranges
        assert "H2:H1002" in ranges
        # 「曜日」「日付」「祝日対応」は自由記述のため対象外
        assert "D2:D1002" not in ranges
        assert "E2:E1002" not in ranges
        assert "G2:G1002" not in ranges


# ── スケジュール シートへのドロップダウン後付け ────────────────────────────────


def _make_schedule_book(path: Path, rows: list[list]) -> Path:
    """「スケジュール」シートだけ持つ既存ブックを作る。"""
    table_rows = [dict(zip(SCHEDULE_HEADERS, row, strict=True)) for row in rows]
    with Excel(path) as book:
        book.create_data_sheet(SCHEDULE_SHEET_NAME).create_table(
            SCHEDULE_SHEET_NAME, Table(SCHEDULE_HEADERS, table_rows)
        )
    return path


class TestApplyScheduleDropdowns:
    # `create_data_sheet()` が `PY_` プレフィックスを付けるので、
    # ヘルパーで作ったシートも `PY_` 付きで load する
    _PY_SCHEDULE = f"PY_{SCHEDULE_SHEET_NAME}"

    def test_adds_dropdown_to_choice_columns(self, tmp_path):
        path = _make_schedule_book(
            tmp_path / "スケジュール.xlsx",
            [["S001", "1001", "毎日", "10:00", "", "", "取得しない", "○"]],
        )
        apply_schedule_dropdowns(path)
        ws = load_workbook(path)[self._PY_SCHEDULE]
        ranges = {str(v.sqref): v.formula1 for v in ws.data_validations.dataValidation}
        # choices は `ScheduleRule.column_specs()` の宣言順（=FREQUENCY_* の順）
        assert ranges["C2:C1001"] == '"1時間ごと,毎日,毎週,毎月"'
        assert ranges["H2:H1001"] == '"○,×"'

    def test_does_not_overwrite_other_data(self, tmp_path):
        """ドロップダウンだけ後付けで、見出し・既存行はそのまま。"""
        path = _make_schedule_book(
            tmp_path / "スケジュール.xlsx",
            [
                ["S001", "1001", "毎日", "10:00", "", "", "取得しない", "○"],
                ["S002", "1002", "毎週", "09:00", "月", "", "取得しない", "○"],
            ],
        )
        apply_schedule_dropdowns(path)
        rules = ScheduleRule.load(path)
        assert [r.schedule_key for r in rules] == ["S001", "S002"]
        assert rules[1].weekday == 0

    def test_missing_schedule_sheet_raises(self, tmp_path):
        """「スケジュール」シートが無いブックでは SheetNotFoundError。"""
        with Excel(tmp_path / "no_sched.xlsx") as book:
            book.create_data_sheet("別のシート").create_table(
                "別のシート", Table(["列"], [])
            )
        with pytest.raises(SheetNotFoundError):
            apply_schedule_dropdowns(tmp_path / "no_sched.xlsx")

    def test_missing_file_raises(self, tmp_path):
        from comken.exceptions import ExcelFileNotFoundError

        with pytest.raises(ExcelFileNotFoundError):
            apply_schedule_dropdowns(tmp_path / "無い.xlsx")


# ── 3シートまとめて 1 ブック ──────────────────────────────────────────────────


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """管理表・履歴の共有定数を tmp_path に差し替える（load_* 系を使うため）。"""
    master = tmp_path / "レポート管理表.xlsx"
    history = tmp_path / "ダウンロード履歴.csv"
    monkeypatch.setattr(_paths_module, "MASTER_PATH", master)
    monkeypatch.setattr(_paths_module, "HISTORY_PATH", history)
    return master, history


class TestCreateCombinedWorkbook:
    def test_creates_three_data_sheets(self, tmp_path):
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        wb = load_workbook(path)
        assert f"PY_{ReportEntry.SHEET_NAME}" in wb.sheetnames
        assert f"PY_{ScheduleRule.SHEET_NAME}" in wb.sheetnames
        assert f"PY_{GroupSetting.SHEET_NAME}" in wb.sheetnames

    def test_load_master_reads_report_entries(self, paths):
        master_path, _ = paths
        create_combined_workbook(master_path)
        entries = load_master(master_path)
        assert list(entries) == ["1001", "1002"]

    def test_load_schedule_reads_schedule_rules(self, paths):
        master_path, _ = paths
        create_combined_workbook(master_path)
        rules = load_schedule(master_path)
        assert [r.schedule_key for r in rules] == ["S001"]

    def test_load_group_settings_reads_settings(self, paths):
        from comken.services.salesforce_downloader.sheets.group_settings import (
            load_group_settings,
        )

        master_path, _ = paths
        create_combined_workbook(master_path)
        # 「設定」シートは GroupSetting.load() 経由でも読める
        settings = {s.group: s.base_path for s in GroupSetting.load(master_path)}
        assert settings == {"営業本部": Path(r"\\server\share\営業本部")}
        # load_group_settings() は MASTER_PATH 経由でも同じ結果を返す
        assert load_group_settings(master_path) == settings

    def test_combined_guide_sheet_includes_all_classes(self, tmp_path):
        """3クラスぶんの列説明が 1 つの「記入方法」シートに並ぶ。"""
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        wb = load_workbook(path)
        assert "記入方法" in wb.sheetnames
        text = "\n".join(str(c.value) for row in wb["記入方法"].iter_rows() for c in row)
        # 先頭の案内（旧 ReportEntry.GUIDE_INTRO 相当）
        assert REPORT_ENTRY_GUIDE_INTRO in text
        # 3クラス全部の列名が出る
        for header in ReportEntry.headers() + ScheduleRule.headers() + GroupSetting.headers():
            assert header in text, f"「{header}」が記入方法シートに出ていない"

    def test_each_data_sheet_has_choices_dropdown(self, tmp_path):
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        wb = load_workbook(path)
        report_ws = wb[f"PY_{ReportEntry.SHEET_NAME}"]
        report_ranges = sorted(str(v.sqref) for v in report_ws.data_validations.dataValidation)
        # 9291 向けの「保存方式」列と「有効」列のドロップダウンがある
        # （新2列追加で列位置が ``+2`` ずれている）
        assert "G2:G1003" in report_ranges  # 保存方式
        assert "H2:H1003" in report_ranges  # 有効
        schedule_ws = wb[f"PY_{ScheduleRule.SHEET_NAME}"]
        schedule_ranges = sorted(str(v.sqref) for v in schedule_ws.data_validations.dataValidation)
        assert "C2:C1002" in schedule_ranges
        # GroupSetting には choices 列が無い
        settings_ws = wb[f"PY_{GroupSetting.SHEET_NAME}"]
        assert list(settings_ws.data_validations.dataValidation) == []

    def test_does_not_leave_default_sheet(self, tmp_path):
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        assert "Sheet" not in load_workbook(path).sheetnames

    def test_font_is_noto_sans_jp_on_all_sheets(self, tmp_path):
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        wb = load_workbook(path)
        for sheet_name in (
            f"PY_{ReportEntry.SHEET_NAME}",
            f"PY_{ScheduleRule.SHEET_NAME}",
            f"PY_{GroupSetting.SHEET_NAME}",
            "記入方法",
        ):
            sheet = wb[sheet_name]
            for row in sheet.iter_rows(min_row=1, max_row=5):
                for cell in row:
                    assert cell.font.name == "Noto Sans JP", (
                        f"{sheet_name} の {cell.coordinate} のフォントが Noto Sans JP ではない"
                    )

    def test_example_rows_have_fill_in_all_sheets(self, tmp_path):
        """3シートそれぞれの記入例に薄い背景色が付く。"""
        path = create_combined_workbook(tmp_path / "レポート管理表.xlsx")
        wb = load_workbook(path)
        for sheet_name in (
            f"PY_{ReportEntry.SHEET_NAME}",
            f"PY_{ScheduleRule.SHEET_NAME}",
            f"PY_{GroupSetting.SHEET_NAME}",
        ):
            sheet = wb[sheet_name]
            cell = sheet["A2"]
            fg = cell.fill.fgColor.value or cell.fill.fgColor.rgb
            assert fg is not None
            assert fg.endswith("D9D9D9") or fg == "00D9D9D9" or fg == "FFD9D9D9"

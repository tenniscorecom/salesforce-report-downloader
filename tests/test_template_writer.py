"""``src/salesforce_downloader.template_writer`` の雛形生成を検証する。

雛形と読み込みで列がズレないこと、`choices` 列にドロップダウンが付くこと、
フォントが Noto Sans JP に揃うこと、記入例に背景色が付き「Sheet」が残らないこと、
``create_combined_workbook`` で3シート＋記入方法シートが1ブックにまとまること、
``create_template`` / ``create_combined_workbook`` が既存シートを新列構成に
マイグレーションできることを確認する。
"""

from dataclasses import dataclass
from pathlib import Path

import comken.services.salesforce_downloader.paths as _paths_module
import pytest
from comken.core.table import Table
from comken.exceptions import SheetNotFoundError
from comken.services.salesforce_downloader.report_master import MasterRow, column
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
    "取得開始時刻",
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
        # ReportEntry の宣言順。「グループ」「担当者」は ID の直後に置く
        # （参照時にすぐ辿れるよう）。「担当者」「概要」は記録用で出力パスに
        # 使わないので宣言順はこのまま
        assert headers == [
            "ID",
            "グループ",
            "担当者",
            "概要",
            "Salesforce URL",
            "有効",
            "0件あり",
            "2000件超",
            "SOQL",
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
        # ReportEntry の `choices` 列は「有効」「0件あり」「2000件超」「SOQL」の 4 つ。
        # 宣言順は ID/概要/Salesforce URL/グループ/担当者/有効/0件あり/2000件超/SOQL の
        # 9 列で、`choices` 付きは 6 列目以降なのでドロップダウンは F〜I 列に付く。
        # 最下行は _FIRST_DATA_ROW(2) + 例(2) - 1 + _DATA_VALIDATION_ROWS(1000) = 1003
        assert "F2:F1003" in ranges  # 有効
        assert "G2:G1003" in ranges  # 0件あり
        assert "H2:H1003" in ranges  # 2000件超
        assert "I2:I1003" in ranges  # SOQL

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
        """`choices` 列（「取得頻度」「曜日」「祝日対応」「有効」）だけにドロップダウンが付く。"""
        path = create_template(
            tmp_path / "スケジュール.xlsx", ScheduleRule, SCHEDULE_EXAMPLES
        )
        ws = load_workbook(path)[f"PY_{SCHEDULE_SHEET_NAME}"]
        ranges = sorted(str(v.sqref) for v in ws.data_validations.dataValidation)
        # 取得頻度=C列, 曜日=F列, 祝日対応=H列, 有効=I列
        assert "C2:C1002" in ranges
        assert "F2:F1002" in ranges  # 曜日（choices 宣言で自動付与）
        assert "H2:H1002" in ranges
        assert "I2:I1002" in ranges
        # 「取得開始時刻」「取得時刻」「日付」は自由記述のため対象外
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
            [["S001", "1001", "毎日", "10:00", "", "", "", "取得しない", "○"]],
        )
        apply_schedule_dropdowns(path)
        ws = load_workbook(path)[self._PY_SCHEDULE]
        ranges = {str(v.sqref): v.formula1 for v in ws.data_validations.dataValidation}
        # choices は `ScheduleRule.column_specs()` の宣言順
        # （取得頻度 / 曜日 / 祝日対応 / 有効）
        assert ranges["C2:C1001"] == '"毎日,毎週,毎月,毎営業日"'
        assert ranges["F2:F1001"] == '"月,火,水,木,金,土,日"'
        assert ranges["H2:H1001"] == '"取得しない,取得する,1営業日前,1営業日後"'
        assert ranges["I2:I1001"] == '"○,×"'

    def test_does_not_overwrite_other_data(self, tmp_path):
        """ドロップダウンだけ後付けで、見出し・既存行はそのまま。"""
        path = _make_schedule_book(
            tmp_path / "スケジュール.xlsx",
            [
                ["S001", "1001", "毎日", "10:00", "", "", "", "取得しない", "○"],
                ["S002", "1002", "毎週", "09:00", "", "月", "", "取得しない", "○"],
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
        # `choices` 列は「有効」「0件あり」「2000件超」「SOQL」の 4 つ。
        # 列宣言順は ID/グループ/担当者/概要/Salesforce URL/有効/0件あり/2000件超/SOQL なので
        # ドロップダウンは F〜I 列に付く
        assert "F2:F1003" in report_ranges  # 有効
        assert "G2:G1003" in report_ranges  # 0件あり
        assert "H2:H1003" in report_ranges  # 2000件超
        assert "I2:I1003" in report_ranges  # SOQL
        schedule_ws = wb[f"PY_{ScheduleRule.SHEET_NAME}"]
        schedule_ranges = sorted(str(v.sqref) for v in schedule_ws.data_validations.dataValidation)
        # スケジュール: 列宣言順は スケジュールキー/レポートキー/取得頻度/取得開始時刻/
        # 取得時刻/曜日/日付/祝日対応/有効。`choices` 列は 取得頻度(C) /
        # 曜日(F) / 祝日対応(H) / 有効(I) の 4 つ
        assert "C2:C1002" in schedule_ranges  # 取得頻度
        assert "F2:F1002" in schedule_ranges  # 曜日
        assert "H2:H1002" in schedule_ranges  # 祝日対応
        assert "I2:I1002" in schedule_ranges  # 有効
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


# ── 既存シートの自動マイグレーション ────────────────────────────────────────


def _is_example_fill(cell) -> bool:
    """薄い背景色（記入例マーク）が付いているか。"""
    fg = cell.fill.fgColor.value or cell.fill.fgColor.rgb
    return bool(fg and (fg.endswith("D9D9D9") or fg == "00D9D9D9" or fg == "FFD9D9D9"))


@dataclass(frozen=True, kw_only=True)
class _LegacyReportEntry(MasterRow):
    """列が6つしかない旧 ReportEntry。マイグレーションを誘発するテスト用。

    ``SHEET_NAME = "管理表"`` で `ReportEntry` と同じシート名を共有させ、
    先にこのクラスで書いたブックを `ReportEntry` の `create_template()` に
    渡すことで「列が追加された」シナリオを再現する。
    """

    SHEET_NAME = "管理表"

    key: str = column("ID", unique=True, help="ID")
    group: str = column("グループ", help="group")
    assignee: str = column("担当者", help="assignee")
    summary: str = column("概要", help="summary")
    url: str = column("Salesforce URL", help="url")
    enabled: bool = column("有効", choices=("○", "×"), help="enabled")


class TestMigrateTemplate:
    """`create_template()` が既存シートを新列構成にマイグレーションできることを確認する。"""

    def test_creates_from_examples_when_no_existing_file(self, tmp_path):
        """ファイル・シートが存在しない場合は従来通り記入例で新規作成される。"""
        path = tmp_path / "管理表.xlsx"
        create_template(path, ReportEntry, EXAMPLES)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        assert sheet.cell(row=2, column=1).value == "1001"
        # 記入例には背景色が付く（回帰確認）
        assert _is_example_fill(sheet.cell(row=2, column=1))

    def test_migrates_existing_data_without_examples(self, tmp_path):
        """既存シートにデータがある状態で create_template() を呼ぶと、
        SheetAlreadyExistsError にならず、データが保持された新列構成のシートになる。"""
        path = tmp_path / "管理表.xlsx"
        # 旧バージョン（6列）で先にブックを作る
        create_template(
            path,
            _LegacyReportEntry,
            [
                {
                    "key": "1001",
                    "group": "営業本部",
                    "assignee": "山田",
                    "summary": "顧客一覧",
                    "url": "https://example/1001",
                    "enabled": True,
                },
                {
                    "key": "1002",
                    "group": "営業本部",
                    "assignee": "佐藤",
                    "summary": "売上実績",
                    "url": "https://example/1002",
                    "enabled": True,
                },
            ],
        )
        # 新バージョン（9列）で同じパスに雛形生成。マイグレーションが走る
        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        # ヘッダーは新しい宣言順
        headers = [cell.value for cell in sheet[1]]
        assert headers == ReportEntry.headers()
        # 既存データの実体は保持されている
        assert sheet.cell(row=2, column=1).value == "1001"
        assert sheet.cell(row=3, column=1).value == "1002"
        assert sheet.cell(row=2, column=2).value == "営業本部"
        assert sheet.cell(row=2, column=3).value == "山田"

    def test_added_columns_are_blank(self, tmp_path):
        """新列は空欄で追加される。"""
        path = tmp_path / "管理表.xlsx"
        create_template(
            path,
            _LegacyReportEntry,
            [
                {
                    "key": "1001",
                    "group": "営業本部",
                    "assignee": "山田",
                    "summary": "顧客一覧",
                    "url": "https://example/1001",
                    "enabled": True,
                },
            ],
        )
        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        # 新列 (allow_empty, exceeds_row_limit, use_soql) はヘッダーはあるが行は空欄
        headers = ReportEntry.headers()
        for header in ("0件あり", "2000件超", "SOQL"):
            assert header in headers
        for header in ("0件あり", "2000件超", "SOQL"):
            col_idx = headers.index(header) + 1
            assert sheet.cell(row=2, column=col_idx).value in (None, ""), (
                f"新列 {header} が空欄になっていない: {sheet.cell(row=2, column=col_idx).value!r}"
            )

    def test_removed_columns_are_dropped(self, tmp_path):
        """旧シートに余分な列がある場合は捨てられて、新シートには現れない。"""
        path = tmp_path / "管理表.xlsx"
        create_template(path, ReportEntry, EXAMPLES)
        # 余分な列を追加して保存（手で列を足した想定）
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        # 10列目・11列目にヘッダーと値を入れる
        sheet.cell(row=1, column=10, value="旧カラムA")
        sheet.cell(row=2, column=10, value="捨てられる値")
        sheet.cell(row=1, column=11, value="旧カラムB")
        sheet.cell(row=2, column=11, value="これも捨てる")
        wb.save(path)
        wb.close()

        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        headers = [cell.value for cell in sheet[1]]
        # 余分な列は消えている
        assert "旧カラムA" not in headers
        assert "旧カラムB" not in headers
        assert len(headers) == len(ReportEntry.headers())
        # 既存行のキー列はそのまま
        assert sheet.cell(row=2, column=1).value == "1001"
        # 捨てられた列のセル位置は空欄
        assert sheet.cell(row=2, column=10).value in (None, "")
        assert sheet.cell(row=2, column=11).value in (None, "")

    def test_migrated_rows_have_no_example_fill(self, tmp_path):
        """マイグレーションされた既存データの行には、記入例特有の薄い背景色が**付かない**。"""
        path = tmp_path / "管理表.xlsx"
        create_template(
            path,
            _LegacyReportEntry,
            [
                {
                    "key": "1001",
                    "group": "営業本部",
                    "assignee": "山田",
                    "summary": "顧客一覧",
                    "url": "https://example/1001",
                    "enabled": True,
                },
            ],
        )
        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        # マイグレーションされた行は白背景のまま
        for row_number in (2,):
            for col in range(1, len(ReportEntry.headers()) + 1):
                assert not _is_example_fill(sheet.cell(row=row_number, column=col)), (
                    f"マイグレーション行 {row_number} 列 {col} に背景色が残っている"
                )

    def test_columns_in_declaration_order_after_migration(self, tmp_path):
        """マイグレーション後の列順は新しい宣言順になる（既存ファイルでの列順は問わない）。"""
        path = tmp_path / "管理表.xlsx"
        # わざと逆順で列を書いた古いシートを作る
        legacy_headers = ["有効", "Salesforce URL", "概要", "担当者", "グループ", "ID"]
        with Excel(path) as book:
            book.create_data_sheet("管理表").create_table(
                "管理表",
                Table(
                    legacy_headers,
                    [
                        dict(
                            zip(
                                legacy_headers,
                                ["○", "https://example/1001", "顧客", "山田", "営業", "1001"],
                                strict=True,
                            )
                        ),
                    ],
                ),
            )
        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        headers = [cell.value for cell in sheet[1]]
        assert headers == ReportEntry.headers()
        # 値が列名と対応付け直されている（最初の列=ID に "1001"）
        assert sheet.cell(row=2, column=1).value == "1001"
        assert sheet.cell(row=2, column=2).value == "営業"
        assert sheet.cell(row=2, column=6).value == "○"

    def test_dropdowns_and_guide_sheet_applied_after_migration(self, tmp_path):
        """マイグレーション後もドロップダウン・「記入方法」シートが正しく適用される。"""
        path = tmp_path / "管理表.xlsx"
        create_template(
            path,
            _LegacyReportEntry,
            [
                {
                    "key": "1001",
                    "group": "営業本部",
                    "assignee": "山田",
                    "summary": "顧客一覧",
                    "url": "https://example/1001",
                    "enabled": True,
                },
                {
                    "key": "1002",
                    "group": "営業本部",
                    "assignee": "佐藤",
                    "summary": "売上",
                    "url": "https://example/1002",
                    "enabled": True,
                },
            ],
        )
        create_template(path, ReportEntry)
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        ranges = sorted(str(v.sqref) for v in sheet.data_validations.dataValidation)
        # 新しい宣言順（F列=有効, G列=0件あり, H列=2000件超, I列=SOQL）にドロップダウン
        # マイグレーション行=2 なので最終行は 2 + 2 - 1 + 1000 = 1003
        assert "F2:F1003" in ranges
        assert "G2:G1003" in ranges
        assert "H2:H1003" in ranges
        assert "I2:I1003" in ranges
        # 「記入方法」シートは新列構成で作り直されている
        assert "記入方法" in wb.sheetnames
        guide_text = "\n".join(
            str(c.value) for row in wb["記入方法"].iter_rows() for c in row
        )
        # 新列（allow_empty, exceeds_row_limit, use_soql の見出し）も記入方法シートに載る
        assert "0件あり" in guide_text
        assert "2000件超" in guide_text
        assert "SOQL" in guide_text

    def test_idempotent_when_columns_unchanged(self, tmp_path):
        """列構成が変わらない再呼び出しでは、データは保持されて背景色も付かない。"""
        path = tmp_path / "管理表.xlsx"
        create_template(path, _LegacyReportEntry, EXAMPLES)
        # 同じクラスで 2 回目の create_template()。examples に上書きされないこと
        create_template(path, _LegacyReportEntry, [{"key": "9999"}])
        wb = load_workbook(path)
        sheet = wb["PY_管理表"]
        # examples ではなく既存の "1001" が残っている
        assert sheet.cell(row=2, column=1).value == "1001"
        # マイグレーション扱いで背景色は付かない
        assert not _is_example_fill(sheet.cell(row=2, column=1))


class TestMigrateCombinedWorkbook:
    """`create_combined_workbook()` でも 3シート単位でマイグレーションが効くことを確認する。"""

    def test_migrates_only_existing_sheets(self, tmp_path):
        """3シートのうち一部だけ既存データがあるケースでも、混在して扱える。"""
        path = tmp_path / "レポート管理表.xlsx"
        # 管理表シートだけ先に作っておく（実データ入り）
        create_template(path, ReportEntry, EXAMPLES)
        # 結合ブックを実行 → スケジュールと設定は記入例から新規作成、管理表はマイグレーション
        create_combined_workbook(path)
        wb = load_workbook(path)
        assert f"PY_{ReportEntry.SHEET_NAME}" in wb.sheetnames
        assert f"PY_{ScheduleRule.SHEET_NAME}" in wb.sheetnames
        assert f"PY_{GroupSetting.SHEET_NAME}" in wb.sheetnames
        # 管理表の実データは保持
        report_sheet = wb[f"PY_{ReportEntry.SHEET_NAME}"]
        assert report_sheet.cell(row=2, column=1).value == "1001"
        # スケジュール・設定は記入例から作られた
        schedule_sheet = wb[f"PY_{ScheduleRule.SHEET_NAME}"]
        assert schedule_sheet.cell(row=2, column=1).value == "S001"
        settings_sheet = wb[f"PY_{GroupSetting.SHEET_NAME}"]
        assert settings_sheet.cell(row=2, column=1).value == "営業本部"
        # 管理表（マイグレーション）は白背景、スケジュール・設定（記入例）は背景色あり
        assert not _is_example_fill(report_sheet.cell(row=2, column=1))
        assert _is_example_fill(schedule_sheet.cell(row=2, column=1))
        assert _is_example_fill(settings_sheet.cell(row=2, column=1))

    def test_migrates_all_three_sheets_when_present(self, tmp_path):
        """3シート全てが既存の場合、すべてマイグレーションされる（背景色は付かない）。"""
        path = tmp_path / "レポート管理表.xlsx"
        # 3シート分の既存ブックを作る
        report_headers = ReportEntry.headers()
        schedule_headers = ScheduleRule.headers()
        settings_headers = GroupSetting.headers()
        with Excel(path) as book:
            book.create_data_sheet(ReportEntry.SHEET_NAME).create_table(
                ReportEntry.SHEET_NAME,
                Table(
                    report_headers,
                    [dict.fromkeys(report_headers, "") | {"ID": "1001"}],
                ),
            )
            book.create_data_sheet(ScheduleRule.SHEET_NAME).create_table(
                ScheduleRule.SHEET_NAME,
                Table(
                    schedule_headers,
                    [dict.fromkeys(schedule_headers, "") | {"スケジュールキー": "S001"}],
                ),
            )
            book.create_data_sheet(GroupSetting.SHEET_NAME).create_table(
                GroupSetting.SHEET_NAME,
                Table(
                    settings_headers,
                    [dict.fromkeys(settings_headers, "") | {"グループ": "営業本部"}],
                ),
            )
        create_combined_workbook(path)
        wb = load_workbook(path)
        # 3シートとも実データが残る
        assert wb[f"PY_{ReportEntry.SHEET_NAME}"].cell(row=2, column=1).value == "1001"
        assert wb[f"PY_{ScheduleRule.SHEET_NAME}"].cell(row=2, column=1).value == "S001"
        assert wb[f"PY_{GroupSetting.SHEET_NAME}"].cell(row=2, column=1).value == "営業本部"
        # 全てマイグレーション扱いで背景色は付かない
        for sheet_name in (
            f"PY_{ReportEntry.SHEET_NAME}",
            f"PY_{ScheduleRule.SHEET_NAME}",
            f"PY_{GroupSetting.SHEET_NAME}",
        ):
            assert not _is_example_fill(wb[sheet_name].cell(row=2, column=1)), (
                f"{sheet_name} のデータ行に背景色が残っている"
            )
        # 3クラス分の「記入方法」シートは1つにまとめ直されている
        assert wb["記入方法"]["A1"].value == REPORT_ENTRY_GUIDE_INTRO

    def test_creates_combined_when_no_existing_file(self, tmp_path):
        """既存ファイル無しの場合は従来通り3シートとも記入例で新規作成される（回帰確認）。"""
        path = tmp_path / "レポート管理表.xlsx"
        create_combined_workbook(path)
        wb = load_workbook(path)
        # 全データシートの記入例に背景色が付く
        for sheet_name in (
            f"PY_{ReportEntry.SHEET_NAME}",
            f"PY_{ScheduleRule.SHEET_NAME}",
            f"PY_{GroupSetting.SHEET_NAME}",
        ):
            assert _is_example_fill(wb[sheet_name].cell(row=2, column=1)), (
                f"{sheet_name} の記入例に背景色が付いていない"
            )

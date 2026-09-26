"""``python -m src.cli check`` の 3 シート相互参照を検証する。

管理表 / スケジュール / 設定の 3 シートを読み、相互参照エラー
（レポートキー未登録・グループ未登録）が複数あってもすべて列挙してから
終了コード 1 を返すこと、正常なブックは 0 で終わることを確認する。
"""

from pathlib import Path

from comken.core.table import Table
from comken.toolbox.excel import Excel

from src.cli import main as sfdl_main

# `src.cli` 自身が直接 ``from src.paths import MASTER_PATH``
# しているので、ここの ``make_workbook`` は ``MASTER_PATH`` の差し替えには頼らない。
# 代わりに CLI にパスを引数として渡し、シート単位で組み立てたブックを参照させる。

GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]
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
MASTER_HEADERS = [
    "ID",
    "グループ",
    "担当者",
    "概要",
    "Salesforce URL",
    "有効",
]


URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"


def make_workbook(
    path: Path,
    *,
    master_rows: list[list[str]] | None,
    schedule_rows: list[list[str]] | None,
    settings_rows: list[list[str]] | None,
) -> Path:
    """3 シート（管理表 / スケジュール / 設定）のうち、指定されたシートだけを作る。

    ``None`` を渡したシートはブックに含めない（``None`` でないシートは1行以上の
    データ行が必要）。``schedule_rows=None`` はスケジュールシート自体を作らない
    （CLI 側では ``SheetNotFoundError`` を ``load_schedule`` が捕捉して空リストに
    するため、読み込みエラーにはならない）。
    """
    with Excel(path) as book:
        if master_rows is not None:
            book.create_data_sheet("管理表").create_table(
                "管理表",
                Table(
                    MASTER_HEADERS,
                    [dict(zip(MASTER_HEADERS, row, strict=True)) for row in master_rows],
                ),
            )
        if schedule_rows is not None:
            book.create_data_sheet("スケジュール").create_table(
                "スケジュール",
                Table(
                    SCHEDULE_HEADERS,
                    [dict(zip(SCHEDULE_HEADERS, row, strict=True)) for row in schedule_rows],
                ),
            )
        if settings_rows is not None:
            book.create_data_sheet("設定").create_table(
                "設定",
                Table(
                    GROUP_SETTINGS_HEADERS,
                    [dict(zip(GROUP_SETTINGS_HEADERS, row, strict=True)) for row in settings_rows],
                ),
            )
    return path


class TestSfdlCheckExitCode:
    """``sfdl check`` の終了コード。"""

    def test_returns_zero_for_well_formed_book(self, tmp_path):
        """3 シートが正しく相互参照できるブックは、終了コード 0。"""
        path = make_workbook(
            tmp_path / "管理表.xlsx",
            master_rows=[
                ["1001", "営業本部", "山田", "顧客一覧", URL_A, "○"],
                ["1002", "営業本部", "佐藤", "売上実績", URL_A, "×"],
            ],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
            ],
            settings_rows=[
                ["営業本部", str(tmp_path / "out")],
            ],
        )
        code = sfdl_main(["check", str(path)])
        assert code == 0

    def test_collects_multiple_cross_reference_errors_then_exits_one(self, tmp_path, capsys):
        """相互参照エラーが複数あっても全部出して、終了コード 1。"""
        path = make_workbook(
            tmp_path / "管理表.xlsx",
            master_rows=[
                # 「1001」はグループ「営業本部」、「1003」は未登録の「新規部署」
                ["1001", "営業本部", "山田", "顧客一覧", URL_A, "○"],
                ["1003", "新規部署", "鈴木", "在庫", URL_A, "○"],
            ],
            schedule_rows=[
                # S001: レポートキー「9999」が管理表に存在しない
                ["S001", "9999", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                # S002: レポートキー「0000」も管理表に存在しない（複数エラーの確認）
                ["S002", "0000", "毎週", "09:00", "", "火", "", "取得しない", "○"],
            ],
            # 「営業本部」だけ登録。「新規部署」が無いのでここでもエラー
            settings_rows=[
                ["営業本部", str(tmp_path / "out")],
            ],
        )
        code = sfdl_main(["check", str(path)])
        captured = capsys.readouterr()
        assert code == 1
        # エラーが3件出ているはず（9999, 0000, 新規部署）
        assert "9999" in captured.err
        assert "0000" in captured.err
        assert "新規部署" in captured.err
        # 「1件目で止まらず全部出す」ことが目的なので、件数表示が出る
        assert "3 件" in captured.err

    def test_succeeds_when_schedule_sheet_is_absent(self, tmp_path):
        """「スケジュール」シートが無いブックは正常終了する。

        ``load_schedule()`` が空リストを返すので、相互参照エラーにはならない。
        """
        path = make_workbook(
            tmp_path / "管理表.xlsx",
            master_rows=[
                ["1001", "営業本部", "山田", "顧客一覧", URL_A, "○"],
            ],
            schedule_rows=None,
            settings_rows=[
                ["営業本部", str(tmp_path / "out")],
            ],
        )
        code = sfdl_main(["check", str(path)])
        assert code == 0


class TestSfdlCheckReferenceNotes:
    """参考情報の表示。エラーではないが気づけるように出す行。"""

    def test_lists_enabled_reports_without_schedule(self, tmp_path, capsys):
        """有効な管理番号でスケジュールが1つもないものは参考情報として出す。"""
        path = make_workbook(
            tmp_path / "管理表.xlsx",
            master_rows=[
                ["1001", "営業本部", "山田", "顧客一覧", URL_A, "○"],
                ["1002", "営業本部", "佐藤", "売上実績", URL_A, "○"],
            ],
            # スケジュールは1行も無い
            schedule_rows=[],
            settings_rows=[
                ["営業本部", str(tmp_path / "out")],
            ],
        )
        code = sfdl_main(["check", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "1001" in out
        assert "1002" in out
        # 参考情報の見出し
        assert "スケジュール行が登録されていません" in out

    def test_lists_schedule_pointing_to_disabled_report(self, tmp_path, capsys):
        """有効なスケジュール行が、無効化されたレポートを指している場合の参考表示。"""
        path = make_workbook(
            tmp_path / "管理表.xlsx",
            master_rows=[
                ["1001", "営業本部", "山田", "顧客一覧", URL_A, "×"],  # 無効
            ],
            schedule_rows=[
                # スケジュールは「有効」で 1001 を指している
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
            ],
            settings_rows=[
                ["営業本部", str(tmp_path / "out")],
            ],
        )
        code = sfdl_main(["check", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "無効化された" in out or "無効" in out

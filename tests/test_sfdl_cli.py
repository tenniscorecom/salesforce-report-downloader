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


class TestSfdlInitExitCode:
    """``sfdl init`` の終了コードと雛形生成。

    ``init`` は ``create_combined_workbook()`` を呼ぶ薄いラッパーで、

    - 存在しないパスなら雛形を作って 0 で終わる
    - 既存ファイルがあるなら上書きせず 1 で終わる

    を確認する。雛形がそのまま ``check`` を通るかも見る（雛形側の整合性が
    崩れていないことを CI で早期発見する）。
    """

    def test_creates_template_and_check_passes(self, tmp_path):
        """存在しないパスで init → 0、ファイルができ、そのファイルへの check も 0。

        雛形の記入例は「スケジュール S001 → レポート 1001」「グループ営業本部 →
        設定シートに登録」のように整合しているので、雛形自体がそのまま
        相互参照エラー無しで読めることを確かめる。
        """
        path = tmp_path / "new_master.xlsx"
        assert not path.exists()

        code = sfdl_main(["init", str(path)])
        assert code == 0
        assert path.exists()

        # 同じパスに対して check を走らせ、相互参照エラーで落ちないことを確認
        check_code = sfdl_main(["check", str(path)])
        assert check_code == 0

    def test_refuses_to_overwrite_existing_file(self, tmp_path):
        """既存ファイルに init → 1、ファイルの中身（バイト列）が変わっていない。

        ``--force`` を持たせない仕様なので、既存ファイルは何があっても
        上書きされない。中身が壊れないことをバイト列で確認する。

        本物の雛形 xlsx を既存ファイルにするのは、ガードが無いと
        ``create_combined_workbook()`` がマイグレーションとして上書き保存してしまい、
        それをバイト列の変化で検出できるようにするため。
        """
        path = tmp_path / "existing_master.xlsx"
        # 先に init を走らせ、本物の雛形 xlsx を「既存ファイル」として用意する
        code = sfdl_main(["init", str(path)])
        assert code == 0
        original_bytes = path.read_bytes()

        # 同じパスにもう一度 init。ガードが効いていれば 1 で弾かれ中身も不変
        code = sfdl_main(["init", str(path)])
        assert code == 1
        assert path.read_bytes() == original_bytes

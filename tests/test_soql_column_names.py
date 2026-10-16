"""``src.soql_reports.column_names`` の単体テスト。

``soql-collector`` の出力 ``列名の対応.csv`` を読み、主オブジェクトに対応する
``{SOQL列名: 表示名}`` を返すヘルパーの挙動を確認する。

**2026-10 改定:** ``列名の対応.csv`` は管理番号ごとのファイルではなく、
**全体で 1 ファイル** にまとめた形式になった。 列は ``主オブジェクト, SOQL列名,
表示名, 備考, 更新日`` の並びで、 ``(主オブジェクト, SOQL列名)`` が同じなら
どのレポートでも同じ ``表示名`` を返す。 テストはこの新形式に合わせて書く。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pytest
from comken.core.table import Table
from comken.services.salesforce_downloader.contract import COLUMN_NAMES_CSV_COLUMNS
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import ColumnNamesCSVFormatError
from src.soql_reports import column_names as column_names_module
from src.soql_reports.base import SoqlReport
from src.soql_reports.column_names import extract_main_object, load_column_names_for
from src.soql_reports.runner import _apply_column_names

# テストで使う ``列名の対応.csv`` のヘッダ順。 ``COLUMN_NAMES_CSV_COLUMNS``
# から取る（契約は comken の ``comken.services.salesforce_downloader.contract``
# が正）。
_HEADER: list[str] = list(COLUMN_NAMES_CSV_COLUMNS)


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    """``列名の対応.csv`` を ``comken.toolbox.csv.CSV`` で書く（soql-collector と同じ形）。

    soql-collector は UTF-8 BOM 付き CRLF の CSV を出力する（ ``CSV`` クラスの既定）。
    ヘッダは ``_HEADER`` の順で固定し、 ``rows`` はデータ行の ``list[list[str]]`` 。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    dict_rows = [dict(zip(_HEADER, row, strict=True)) for row in rows]
    with CSV(path, columns=_HEADER) as csv_file:
        csv_file.replace(dict_rows)


class TestExtractMainObject:
    """``extract_main_object()`` の挙動確認。"""

    def test_returns_first_from_keyword(self):
        """``FROM Account`` から主オブジェクト ``Account`` を返す。"""
        assert extract_main_object("SELECT Id, Name FROM Account") == "Account"

    def test_handles_lowercase_from(self):
        """小文字 ``from`` でも取り出せる（大文字小文字を区別しない）。"""
        assert extract_main_object("SELECT Id, Name from account") == "account"

    def test_handles_mixed_case(self):
        """``From`` / ``FROM`` のような大小混在でも取り出せる。"""
        assert extract_main_object("select id, name From Account") == "Account"

    def test_handles_multiline_soql(self):
        """改行を含む SOQL でも最初の ``FROM`` を取り出せる。"""
        soql = (
            "SELECT Id, Name,\n       Account.Name\nFROM Opportunity\nWHERE CloseDate >= 2024-01-01"
        )
        assert extract_main_object(soql) == "Opportunity"

    def test_handles_where_clause(self):
        """``FROM Account WHERE ...`` のように ``WHERE`` が後ろでも問題ない。"""
        assert extract_main_object("SELECT Id FROM Account WHERE CreatedDate = TODAY") == "Account"

    def test_handles_underscore_in_name(self):
        """主オブジェクト名に ``_`` を含むパターンもそのまま取り出す。"""
        assert extract_main_object("SELECT Id FROM Custom_Object__c") == "Custom_Object__c"

    def test_returns_none_when_no_from(self):
        """``FROM`` がない SOQL では ``None`` を返す。"""
        assert extract_main_object("SELECT Id, Name") is None
        assert extract_main_object("") is None

    def test_first_from_wins(self):
        """``FROM`` が複数あっても最初のものを返す。 通常は無い形だが、防御的に。"""
        soql = "SELECT Id FROM Account WHERE Id IN (SELECT Id FROM Contact)"
        assert extract_main_object(soql) == "Account"


class TestLoadColumnNamesFor:
    """``load_column_names_for()`` の挙動確認。"""

    def test_returns_matching_main_object_only(self, csv_path: Path):
        """複数主オブジェクトが混在するファイルから ``main_object`` に該当する行だけを返す。"""
        _write_csv(
            csv_path,
            [
                ["Account", "Name", "名前", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
                ["Account", "Industry", "業種", "", "2026-10-01"],
            ],
        )
        result = load_column_names_for("Account")

        assert result == {"Name": "名前", "Industry": "業種"}

    def test_other_main_object_rows_are_ignored(self, csv_path: Path):
        """他主オブジェクトの行があっても ``main_object`` の行だけ返る（混入防止）。"""
        _write_csv(
            csv_path,
            [
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
                ["Contact", "LastName", "姓", "", "2026-10-01"],
            ],
        )
        result = load_column_names_for("Account")
        # Account の行は無いので空 dict
        assert result == {}

    def test_main_object_comparison_is_case_insensitive(self, csv_path: Path):
        """主オブジェクト名は大文字小文字を区別しない（Salesforce の API 名仕様）。"""
        # ファイル側は小文字
        _write_csv(
            csv_path,
            [
                ["account", "Name", "名前", "", "2026-10-01"],
                ["account", "Industry", "業種", "", "2026-10-01"],
            ],
        )
        # 引数側は大文字でも一致する
        assert load_column_names_for("Account") == {"Name": "名前", "Industry": "業種"}
        assert load_column_names_for("ACCOUNT") == {"Name": "名前", "Industry": "業種"}
        assert load_column_names_for("account") == {"Name": "名前", "Industry": "業種"}

    def test_missing_file_returns_empty_with_warning(
        self,
        csv_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """ファイルが無いと空 dict + WARNING を返す（取得は止めない）。"""
        # csv_path は指すが実ファイルは作らない
        assert not csv_path.exists()
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("Account")

        assert result == {}
        # WARNING に「列名の対応ファイルがありません」「API 名のまま保存します」
        # が出ている（取得を止めないことが伝わる文言）
        assert "列名の対応ファイルがありません" in caplog.text
        assert "API 名のまま保存します" in caplog.text
        assert str(csv_path) in caplog.text

    def test_no_matching_main_object_returns_empty_with_warning(
        self,
        csv_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """ファイルは有るが該当主オブジェクトの行が無い → 空 dict + WARNING。"""
        _write_csv(
            csv_path,
            [
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("Account")

        assert result == {}
        # WARNING に「主オブジェクト=Account」「行がありません」「soql-collector で取ってください」
        assert "Account" in caplog.text
        assert "行がありません" in caplog.text
        assert "soql-collector" in caplog.text

    def test_header_only_file_returns_empty_with_warning(
        self,
        csv_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """見出しだけでデータ行が無いファイル（soql-collector 走り立て直後の状態）は
        空 dict + WARNING を返す。 フォーマット不正ではない。"""
        # データ行を書かない = ヘッダのみのファイル
        _write_csv(csv_path, [])

        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("Account")

        # 空 dict + 「行が無い」 WARNING。 取得は止めない
        assert result == {}
        assert "Account" in caplog.text
        assert "行がありません" in caplog.text
        assert "soql-collector" in caplog.text

    def test_empty_soql_or_display_rows_are_skipped(self, csv_path: Path):
        """SOQL列名または表示名が空の行は飛ばす（例外にはしない）。"""
        _write_csv(
            csv_path,
            [
                ["Account", "Name", "名前", "", "2026-10-01"],
                # SOQL列名が空
                ["Account", "", "業種", "", "2026-10-01"],
                # 表示名が空
                ["Account", "Industry", "", "", "2026-10-01"],
            ],
        )
        result = load_column_names_for("Account")
        assert result == {"Name": "名前"}

    def test_notes_and_updated_at_are_ignored(self, csv_path: Path):
        """``備考`` と ``更新日`` 列は人が読むメタ列で、読み込みでは使わない。"""
        _write_csv(
            csv_path,
            [
                [
                    "Account",
                    "Name",
                    "名前",
                    "別の表示名あり: 取引先名",
                    "2026-10-13",
                ],
            ],
        )
        result = load_column_names_for("Account")
        assert result == {"Name": "名前"}

    def test_format_error_raises_on_missing_required_column(self, csv_path: Path):
        """必要な列が無いファイルは ``ColumnNamesCSVFormatError`` で失敗させる。"""
        # 「主オブジェクト」列を抜いたヘッダー（BOM は CSV クラスの既定で付く）
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with CSV(path, columns=["SOQL列名", "表示名", "備考", "更新日"]) as csv_file:
            csv_file.replace([{"SOQL列名": "Name", "表示名": "名前", "備考": "", "更新日": ""}])

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("Account")
        message = str(caught.value)
        assert "主オブジェクト" in message
        assert "SOQL列名" in message
        assert "表示名" in message
        assert "対処" in message
        # パスも入っている（業務担当者が共有サーバーの障害に気づけるように）
        assert str(csv_path) in message

    def test_old_format_header_raises_format_error(self, csv_path: Path):
        """旧形式の見出し（ ``管理番号, 概要, SOQL列名, 表示名, 備考`` ）のファイルは
        ``ColumnNamesCSVFormatError`` で止める（ ``主オブジェクト`` 列が無い）。

        新規の ``soql-collector`` で取り直すまで、 CSV 形式の不一致を運用側に
        気づかせるためのテスト。 旧形式のファイルは無視せず明示的に失敗させる。"""
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with CSV(
            path,
            columns=["管理番号", "概要", "SOQL列名", "表示名", "備考"],
        ) as csv_file:
            csv_file.replace(
                [
                    {
                        "管理番号": "9001",
                        "概要": "顧客一覧",
                        "SOQL列名": "Name",
                        "表示名": "名前",
                        "備考": "",
                    },
                ],
            )

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("Account")
        message = str(caught.value)
        # 「対処」メッセージに新形式への切替を促す文言が出る
        assert "対処" in message
        assert "主オブジェクト" in message
        # 新しい ``soql-collector`` を実行し直す手順が書かれている
        assert "soql-collector" in message

    def test_format_error_raises_on_empty_file(self, csv_path: Path):
        """**本当に空**のファイル（BOM だけ or 0 バイト）もフォーマット不正として扱う。

        見出しすら無いケースは、 CSV 形式が壊れている運用上の問題として
        ``ColumnNamesCSVFormatError`` で止める。 見出しはあるが行が無い
        （``読み込み結果が空`` ）ケースは、 ``WARNING`` で「該当主オブジェクトの
        行が無い」を出して続行する。 ``test_header_only_file_returns_empty_with_warning``
        参照。
        """
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # BOM だけ。 comken の ``CSV`` クラスは見出し無しと判定して ``CSVError``
        # を上げる
        path.write_bytes(b"\xef\xbb\xbf")

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("Account")
        message = str(caught.value)
        assert "対処" in message

    def test_format_error_on_unreadable_bytes(self, csv_path: Path):
        """ファイルが壊れている（ ``UnicodeDecodeError`` ）ときもフォーマット不正で止まる。"""
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe" + b"not utf-8 at all " * 4)

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("Account")
        message = str(caught.value)
        assert "対処" in message

    def test_explicit_csv_path_overrides_default(
        self,
        csv_path: Path,
        tmp_path: Path,
    ):
        """``csv_path`` 引数を明示で渡すとデフォルトより優先される（テスト用差し替え口）。"""
        _write_csv(
            csv_path,
            [
                ["Account", "Name", "デフォルト名", "", "2026-10-01"],
            ],
        )
        override = tmp_path / "override.csv"
        _write_csv(
            override,
            [
                ["Account", "Name", "上書き名", "", "2026-10-01"],
            ],
        )

        result = load_column_names_for("Account", csv_path=override)
        assert result == {"Name": "上書き名"}


class _ColumnNamesManualReport(SoqlReport):
    """``COLUMN_NAMES = {"Name": "名前"}`` のダミー。 ``extra`` との優先順位確認用。"""

    KEY = "9101"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {"Name": "名前"}

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _ColumnNamesEmptyReport(SoqlReport):
    """``COLUMN_NAMES = {}`` のダミー。 ``extra`` 単独の確認用。"""

    KEY = "9102"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _ColumnNamesPartialReport(SoqlReport):
    """``COLUMN_NAMES`` が ``Name`` のみのダミー（マージ確認用）。"""

    KEY = "9103"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {"Name": "名前"}

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Account"


class TestApplyColumnNamesExtra:
    """``runner._apply_column_names()`` の ``extra`` 引数の挙動確認。"""

    def test_extra_overrides_column_names(self):
        """``extra`` と ``COLUMN_NAMES`` で同じキーが衝突すると、 ``COLUMN_NAMES``
        （手書き）が勝つ。 ``_apply_column_names`` のマージ順 ``{**extra,
        **COLUMN_NAMES}`` に依存している。
        """
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        result = _apply_column_names(_ColumnNamesManualReport, table, extra={"Name": "氏名"})
        assert result.columns == ["Id", "名前"]  # 手書きが優先
        assert result.to_rows() == [{"Id": "1", "名前": "x"}]

    def test_extra_alone_works_when_column_names_empty(self):
        """``extra`` 単独（ ``COLUMN_NAMES`` が空）でも動く。"""
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        result = _apply_column_names(_ColumnNamesEmptyReport, table, extra={"Name": "名前"})
        assert result.columns == ["Id", "名前"]
        assert result.to_rows() == [{"Id": "1", "名前": "x"}]

    def test_extra_and_column_names_merge(self):
        """``extra`` と ``COLUMN_NAMES`` に重複しないキーがあれば両方適用。"""
        # ``_ColumnNamesPartialReport`` の ``COLUMN_NAMES`` は ``Name`` のみ、
        # ``extra`` で ``Amount`` を渡す → 両方が反映される
        table = Table(["Id", "Name", "Amount"], [{"Id": "1", "Name": "x", "Amount": "100"}])
        result = _apply_column_names(
            _ColumnNamesPartialReport,
            table,
            extra={"Amount": "金額"},
        )
        assert result.columns == ["Id", "名前", "金額"]
        assert result.to_rows() == [{"Id": "1", "名前": "x", "金額": "100"}]

    def test_extra_none_means_no_extra(self):
        """``extra=None`` は ``{}`` と同じく動く（後方互換）。"""
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        # ``_ColumnNamesManualReport`` の ``COLUMN_NAMES = {"Name": "名前"}`` が効く
        result = _apply_column_names(_ColumnNamesManualReport, table, extra=None)
        assert result.columns == ["Id", "名前"]


@pytest.fixture
def csv_path(tmp_path, monkeypatch) -> Path:
    """``paths.COLUMN_NAMES_CSV_PATH`` を ``tmp_path`` 配下に差し替える。

    ``column_names.py`` 側が ``from src.paths import COLUMN_NAMES_CSV_PATH`` で
    モジュール変数を取り込んでいる（ ``paths.COLUMN_NAMES_CSV_PATH`` を直接
    参照しているわけではない）。 両方を patch しないと ``load_column_names_for``
    が既定の UNC パスを読みに行くので注意。
    戻り値はテストが CSV を書き込むパス（実ファイルはまだ無い）。
    ファイルの中身は各テストで ``_write_csv`` する。
    """
    path = tmp_path / "soql-collector" / "列名の対応.csv"
    monkeypatch.setattr(paths_module, "COLUMN_NAMES_CSV_PATH", path)
    # ``column_names.py`` 側で import 済みのモジュール変数を上書きする
    monkeypatch.setattr(column_names_module, "COLUMN_NAMES_CSV_PATH", path)
    return path

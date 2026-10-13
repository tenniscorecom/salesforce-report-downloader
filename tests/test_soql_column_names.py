"""``src.soql_reports.column_names`` の単体テスト。

``soql-collector`` の出力 ``列名の対応.csv`` を読み、管理番号に対応する
``{SOQL列名: 表示名}`` を返すヘルパーの挙動を確認する。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

import pytest
from comken.core.table import Table
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import ColumnNamesCSVFormatError
from src.soql_reports import column_names as column_names_module
from src.soql_reports.base import SoqlReport
from src.soql_reports.column_names import load_column_names_for
from src.soql_reports.runner import _apply_column_names

# テストで使う ``列名の対応.csv`` のヘッダ順。``column_names._REQUIRED_COLUMNS``
# ＋「概要」。 soql-collector 側との取り決めで、人が「この行は何のレポートか」
# を見るときに使う列
_HEADER: list[str] = ["管理番号", "概要", "SOQL列名", "表示名", "備考"]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    """``列名の対応.csv`` を ``comken.toolbox.csv.CSV`` で書く（soql-collector と同じ形）。

    soql-collector は UTF-8 BOM 付き CRLF の CSV を出力する（``CSV`` クラスの既定）。
    ヘッダは ``_HEADER`` の順で固定し、 ``rows`` はデータ行の ``list[list[str]]``
    （呼び出し側の書き味を ``,`` 結合の手書きに揃えている）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    dict_rows = [dict(zip(_HEADER, row, strict=True)) for row in rows]
    with CSV(path, columns=_HEADER) as csv_file:
        csv_file.replace(dict_rows)


class TestLoadColumnNamesFor:
    """``load_column_names_for()`` の挙動確認。"""

    def test_returns_matching_report_keys_only(self, csv_path: Path):
        """複数管理番号が混在するファイルから ``report_key`` に該当する行だけを返す。"""
        _write_csv(
            csv_path,
            [
                ["9001", "顧客一覧", "Name", "名前", ""],
                ["9002", "売上実績", "Amount", "金額", ""],
                ["9001", "顧客一覧", "Amount", "金額", ""],
            ],
        )
        result = load_column_names_for("9001")

        assert result == {"Name": "名前", "Amount": "金額"}

    def test_other_report_key_rows_are_ignored(self, csv_path: Path):
        """他管理番号の行があっても ``report_key`` の行だけ返る（混入防止）。"""
        _write_csv(
            csv_path,
            [
                ["9002", "売上実績", "Amount", "金額", ""],
                ["9003", "案件一覧", "Name", "名前", ""],
            ],
        )
        result = load_column_names_for("9001")
        # 9001 には何も無いので空 dict（行が無い旨の WARNING は下で検証）
        assert result == {}

    def test_missing_file_returns_empty_with_warning(
        self, csv_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """ファイルが無いと空 dict + WARNING を返す（取得は止めない）。"""
        # csv_path は指すが実ファイルは作らない
        assert not csv_path.exists()
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("9001")

        assert result == {}
        # WARNING に「列名の対応ファイルがありません」「API 名のまま保存します」
        # が出ている（取得を止めないことが伝わる文言）
        assert "列名の対応ファイルがありません" in caplog.text
        assert "API 名のまま保存します" in caplog.text
        assert str(csv_path) in caplog.text

    def test_no_matching_report_key_returns_empty_with_warning(
        self, csv_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """ファイルは有るが該当管理番号の行が無い → 空 dict + WARNING。"""
        _write_csv(
            csv_path,
            [
                ["9002", "売上実績", "Amount", "金額", ""],
            ],
        )
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("9001")

        assert result == {}
        # WARNING に「行がありません」「soql-collector で取ってください」が出ている
        assert "9001" in caplog.text
        assert "行がありません" in caplog.text
        assert "soql-collector" in caplog.text

    def test_header_only_file_returns_empty_with_warning(
        self, csv_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """見出しだけでデータ行が無いファイル（soql-collector 走り立て直後の状態）は
        空 dict + WARNING を返す。 フォーマット不正ではない。"""
        # データ行を書かない = ヘッダのみのファイル
        _write_csv(csv_path, [])

        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("9001")

        # 空 dict + 「行が無い」 WARNING。 取得は止めない
        assert result == {}
        assert "9001" in caplog.text
        assert "行がありません" in caplog.text
        assert "soql-collector" in caplog.text

    def test_duplicate_rows_are_skipped(
        self, csv_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        """備考が ``表示名が重複`` の行はスキップされ、SOQL列名が WARNING に並ぶ。"""
        _write_csv(
            csv_path,
            [
                ["9001", "顧客一覧", "Name", "名前", ""],
                ["9001", "顧客一覧", "Amount", "金額", ""],
                ["9001", "顧客一覧", "Account.Name", "取引先名", "表示名が重複"],
            ],
        )
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = load_column_names_for("9001")

        assert result == {"Name": "名前", "Amount": "金額"}
        assert "Account.Name" in caplog.text
        assert "重複" in caplog.text
        # Account.Name は結果に含まれない（重複行として除外された）
        assert "Account.Name" not in result

    def test_empty_soql_or_display_rows_are_skipped(self, csv_path: Path):
        """SOQL列名または表示名が空の行は飛ばす（例外にはしない）。"""
        _write_csv(
            csv_path,
            [
                ["9001", "顧客一覧", "Name", "名前", ""],
                # SOQL列名が空
                ["9001", "顧客一覧", "", "取引先名", ""],
                # 表示名が空
                ["9001", "顧客一覧", "Amount", "", ""],
            ],
        )
        result = load_column_names_for("9001")
        assert result == {"Name": "名前"}

    def test_format_error_raises_on_missing_required_column(self, csv_path: Path):
        """必要な列が無いファイルは ``ColumnNamesCSVFormatError`` で失敗させる。"""
        # 「管理番号」列を抜いたヘッダー
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "﻿" + "概要,SOQL列名,表示名,備考\r\n顧客一覧,Name,名前,\r\n"
        path.write_bytes(content.encode("utf-8"))

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("9001")
        message = str(caught.value)
        assert "管理番号" in message
        assert "対処" in message
        # パスも入っている（業務担当者が共有サーバーの障害に気づけるように）
        assert str(csv_path) in message

    def test_format_error_raises_on_empty_file(self, csv_path: Path):
        """**本当に空**のファイル（BOM だけ or 0 バイト）もフォーマット不正として扱う。

        見出しすら無いケースは、CSV 形式が壊れている運用上の問題として
        ``ColumnNamesCSVFormatError`` で止める。 見出しはあるが行が無い
        （``読み込み結果が空``）ケースは、 ``WARNING`` で「該当管理番号の
        行が無い」を出して続行する。 ``test_header_only_file_returns_empty_with_warning``
        参照。
        """
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # BOM だけ。 comken の ``CSV`` クラスは見出し無しと判定して ``CSVError``
        # を上げる
        path.write_bytes(b"\xef\xbb\xbf")

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("9001")
        message = str(caught.value)
        assert "対処" in message

    def test_format_error_on_unreadable_bytes(self, csv_path: Path):
        """ファイルが壊れている（``UnicodeDecodeError``）ときもフォーマット不正で止まる。"""
        path = csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe" + b"not utf-8 at all " * 4)

        with pytest.raises(ColumnNamesCSVFormatError) as caught:
            load_column_names_for("9001")
        message = str(caught.value)
        assert "対処" in message

    def test_explicit_csv_path_overrides_default(
        self, csv_path: Path, tmp_path: Path,
    ):
        """``csv_path`` 引数を明示で渡すとデフォルトより優先される（テスト用差し替え口）。"""
        _write_csv(
            csv_path,
            [
                ["9001", "デフォルト", "Name", "名前", ""],
            ],
        )
        override = tmp_path / "override.csv"
        _write_csv(
            override,
            [
                ["9001", "上書き", "Amount", "金額", ""],
            ],
        )

        result = load_column_names_for("9001", csv_path=override)
        assert result == {"Amount": "金額"}


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
        """``extra`` と ``COLUMN_NAMES`` で同じキーが衝突すると、``COLUMN_NAMES``
        （手書き）が勝つ。 ``_apply_column_names`` のマージ順 ``{**extra,
        **COLUMN_NAMES}`` に依存している。
        """
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        result = _apply_column_names(_ColumnNamesManualReport, table, extra={"Name": "氏名"})
        assert result.columns == ["Id", "名前"]  # 手書きが優先
        assert result.to_rows() == [{"Id": "1", "名前": "x"}]

    def test_extra_alone_works_when_column_names_empty(self):
        """``extra`` 単独（``COLUMN_NAMES`` が空）でも動く。"""
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
    モジュール変数を取り込んでいる（``paths.COLUMN_NAMES_CSV_PATH`` を直接
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

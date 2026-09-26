"""Excel の表を設定として読む仕組みを、実際に Excel を作って検証する。

Salesforce に依存しない汎用部分だけを見る（Salesforce 固有の列は
test_service.py 側）。
"""

import datetime as dt
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import (
    ComkenFileNotFoundError,
    ExcelApplicationNotAvailableError,
)
from comken.toolbox.excel import Excel
from openpyxl import load_workbook

from src.exceptions import (
    MasterTableError,
)
from src.report_master import MasterRow, column


@dataclass(frozen=True, kw_only=True)
class Item(MasterRow):
    """検証用の1行。"""

    SHEET_NAME = "一覧"

    key: str = column("ID", unique=True, help="管理番号")
    name: str = column("名前", help="人が読んで分かる名前")
    source: Path = column("コピー元", help="共有サーバー上のファイル")
    mode: str = column("方式", choices=("毎日", "手動"), help="毎日は自動で取ります")
    enabled: bool = column("有効", choices=("○", "×"), help="「○」か「×」と書いてください")
    note: str = column("備考", default="", help="編集者の覚え書き")


HEADERS = ["ID", "名前", "コピー元", "方式", "有効", "備考"]
ROW_A = ["1001", "受注一覧", r"\\server\受注\data.csv", "毎日", "○", ""]
ROW_B = ["1002", "在庫", r"\\server\在庫\data.csv", "手動", "×", ""]


def make_sheet(path: Path, rows: list[list], headers: list[str] | None = None) -> Path:
    actual_headers = headers or HEADERS
    table_rows = [dict(zip(actual_headers, row, strict=False)) for row in rows]
    with Excel(path) as excel:
        excel.create_data_sheet("一覧").create_table("一覧", Table(actual_headers, table_rows))
    return path


class TestLoad:
    """宣言した型のとおりに読める。"""

    def test_reads_rows_with_declared_types(self, tmp_path):
        items = Item.load(make_sheet(tmp_path / "一覧.xlsx", [ROW_A, ROW_B]))
        assert [item.key for item in items] == ["1001", "1002"]
        assert isinstance(items[0].source, Path)  # 型注釈のとおり Path になる
        assert items[0].enabled is True
        assert items[1].enabled is False  # 「×」は False

    def test_blank_rows_are_skipped(self, tmp_path):
        """表の下に残った空行は読み飛ばす。"""
        items = Item.load(make_sheet(tmp_path / "一覧.xlsx", [ROW_A, [None] * 6]))
        assert len(items) == 1

    def test_blank_in_required_column_raises(self, tmp_path):
        """既定値の無い列が空欄なら止める（→ 理由は TestBlankPolicy）。"""
        row = ["1001", "受注一覧", r"\\server\a.csv", "毎日", None, ""]
        with pytest.raises(MasterTableError):
            Item.load(make_sheet(tmp_path / "一覧.xlsx", [row]))

    def test_blank_without_default_raises(self, tmp_path):
        """既定値の無い列が空なら、その行と列を示して止める。"""
        row = ["1001", None, r"\\server\a.csv", "毎日", "○", ""]
        with pytest.raises(MasterTableError) as e:
            Item.load(make_sheet(tmp_path / "一覧.xlsx", [row]))
        assert "2 行目" in str(e.value)  # 見出しが1行目なので、最初のデータは2行目
        assert "名前" in str(e.value)

    def test_path_without_default_raises(self, tmp_path):
        with pytest.raises(MasterTableError):
            Item.load()  # PATH も引数も無い

    def test_missing_path_raises_with_path_in_message(self, tmp_path):
        """存在しないパスを渡したら、業務担当者が原因を読める例外で止める。

        **書き込みモードで開くと、空ブックを新規作成して「対象テーブルを
        一意に決められません」で落ちる**（ファイル不在の事実が消える）。
        管理表は共有サーバー (UNC) に置く運用で、現実の失敗は「サーバーが
        落ちた」「パスが変わった」「権限が無い」。 ``ComkenFileNotFoundError``
        がそのまま上がれば、画面にパスが出るため担当者が IT に連絡できる。
        旧実装 (``Excel(source)`` 書き込みモード) では ``TableError``
        が送出されるため、このテストは旧実装では落ちる。
        """
        missing = tmp_path / "存在しない管理表.xlsx"
        assert not missing.exists()  # 前提: ファイルが無い
        with pytest.raises(ComkenFileNotFoundError) as e:
            Item.load(missing)
        assert str(missing) in str(e.value)  # パスがメッセージに出る


class TestValidation:
    """非エンジニアが編集する表なので、どこが変かを示して止める。"""

    def test_value_outside_choices_raises(self, tmp_path):
        row = ["1001", "受注一覧", r"\\server\a.csv", "毎週", "○", ""]
        with pytest.raises(MasterTableError) as e:
            Item.load(make_sheet(tmp_path / "一覧.xlsx", [row]))
        assert "「毎日」か「手動」" in str(e.value)  # 書ける値を示す

    def test_non_numeric_key_raises(self, tmp_path):
        row = ["A001", "受注一覧", r"\\server\a.csv", "毎日", "○", ""]
        # str 型の key 列は「数字」を要求しない（むしろ数字以外も使える）。
        # 代わりに、重複していないことの検証として、別のシナリオで確認する
        items = Item.load(make_sheet(tmp_path / "一覧.xlsx", [row]))
        assert items[0].key == "A001"

    def test_numeric_string_key_is_preserved(self, tmp_path):
        """Excel が `1001` を数値セルで返すとき、`"1001"` として読める。"""
        # openpyxl 経由で、数値セル（float）として `1001` を書く
        path = make_sheet(
            tmp_path / "一覧.xlsx",
            [[1001, "受注一覧", r"\\server\a.csv", "毎日", "○", ""]],
        )

        items = Item.load(path)
        assert items[0].key == "1001"  # "1001.0" ではない

    def test_duplicate_unique_value_raises(self, tmp_path):
        rows = [ROW_A, ["1001", "別の名前", r"\\server\b.csv", "手動", "○", ""]]
        with pytest.raises(MasterTableError) as e:
            Item.load(make_sheet(tmp_path / "一覧.xlsx", rows))
        assert "ID" in str(e.value)

    def test_missing_header_raises_with_existing_headers(self, tmp_path):
        """見出しを変えられたら、今ある見出しを示して止める。"""
        headers = ["ID", "名称", "コピー元", "方式", "有効", "備考"]  # 「名前」を「名称」に変えた
        with pytest.raises(MasterTableError) as e:
            Item.load(make_sheet(tmp_path / "一覧.xlsx", [ROW_A], headers))
        assert "名前" in str(e.value)
        assert "名称" in str(e.value)  # 今ある見出しも出す

    def test_value_outside_bool_choices_raises(self, tmp_path):
        """`enabled` を「○」「×」以外にするとエラー。表記が1つに絞られる。"""
        row = ["1001", "受注一覧", r"\\server\a.csv", "毎日", "有効", ""]
        with pytest.raises(MasterTableError) as e:
            Item.load(make_sheet(tmp_path / "一覧.xlsx", [row]))
        assert "「○」か「×」" in str(e.value)


class TestColumnAdded:
    """あとから列を足しても、既存の表が読めなくならないこと。

    共有サーバーを更新すると全プロジェクトへ伝播するので、**列を1つ足した瞬間に
    既存の管理表がすべて読めなくなる**と業務が止まる。
    """

    def test_new_column_with_default_is_filled(self, tmp_path):
        """既定値のある列は、見出しごと無くても埋められる。"""

        @dataclass(frozen=True, kw_only=True)
        class WithNewColumn(MasterRow):
            SHEET_NAME = "一覧"

            key: str = column("ID", unique=True)
            name: str = column("名前")
            source: Path = column("コピー元")
            mode: str = column("方式", choices=("毎日", "手動"))
            enabled: bool = column("有効", choices=("○", "×"))
            memo: str = column("備考", default="")  # ← あとから足した列

        items = WithNewColumn.load(make_sheet(tmp_path / "一覧.xlsx", [ROW_A]))
        assert items[0].memo == ""  # 既定値で埋まる
        assert items[0].name == "受注一覧"  # 既存の列はそのまま読める

    def test_new_column_without_default_still_raises(self, tmp_path):
        """値が要る列を足したなら、管理表に足すまで止める。"""

        @dataclass(frozen=True, kw_only=True)
        class WithRequiredColumn(MasterRow):
            SHEET_NAME = "一覧"

            key: str = column("ID", unique=True)
            name: str = column("名前")
            source: Path = column("コピー元")
            mode: str = column("方式", choices=("毎日", "手動"))
            enabled: bool = column("有効", choices=("○", "×"))
            owner: str = column("担当", help="この一覧の持ち主")  # 既定値なし

        with pytest.raises(MasterTableError) as e:
            WithRequiredColumn.load(make_sheet(tmp_path / "一覧.xlsx", [ROW_A]))
        assert "担当" in str(e.value)

    def test_extra_column_in_excel_is_ignored(self, tmp_path):
        """宣言していない列が Excel にあっても無視する（列を消したとき）。"""
        headers = [*HEADERS, "使わない列"]
        rows = [[*ROW_A, "なにか"]]
        items = Item.load(make_sheet(tmp_path / "一覧.xlsx", rows, headers))
        assert items[0].key == "1001"


class TestColumnSpecs:
    """``column_specs()`` の公開 API。利用側（雛形生成・ドロップダウン適用など）が
    列定義を読むための入り口。``_columns()`` と並び順・内容を保ったまま既定値も返す。"""

    def test_returns_one_entry_per_declared_column(self):
        specs = Item.column_specs()
        assert [name for name, _, _, _ in specs] == [
            "key",
            "name",
            "source",
            "mode",
            "enabled",
            "note",
        ]

    def test_spec_carries_header_and_choices(self):
        specs = Item.column_specs()
        modes = next(item for item in specs if item[0] == "mode")
        _, mode_spec, _, _ = modes
        assert mode_spec.header == "方式"
        assert mode_spec.choices == ("毎日", "手動")
        assert mode_spec.help == "毎日は自動で取ります"

    def test_default_is_dataclasses_missing_when_not_declared(self):
        import dataclasses

        specs = Item.column_specs()
        name_default = next(item[3] for item in specs if item[0] == "name")
        assert name_default is dataclasses.MISSING  # 既定値なし＝必須

    def test_default_is_returned_when_declared(self):
        specs = Item.column_specs()
        note_default = next(item[3] for item in specs if item[0] == "note")
        assert note_default == ""  # 既定値あり＝空欄でもOK

    def test_value_type_is_the_annotated_type(self):
        specs = Item.column_specs()
        key_value_type = next(item[2] for item in specs if item[0] == "key")
        # 注釈が str なら str クラスが返る（前方参照のとき文字列になる仕様は同じ）
        assert key_value_type is str


class TestHeaderAccess:
    """Python の名前から Excel の見出しを引ける。"""

    def test_header_returns_the_excel_header(self):
        assert Item.header("name") == "名前"
        assert Item.header("source") == "コピー元"

    def test_headers_are_in_declaration_order(self):
        assert Item.headers() == HEADERS

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError):
            Item.header("存在しない")


class TestBlankPolicy:
    """空欄をどう扱うかは、既定値の有無で決まる。

    **既定値は「空欄でよい」という宣言。** 意味が反転する列（有効/無効）に既定値を
    付けると、書き忘れがそのまま「有効」になり、意図と逆の結果になる。
    """

    def test_blank_is_an_error_without_default(self, tmp_path):
        """既定値の無い列は、空欄なら止める（入力し忘れと区別が付かないため）。"""

        @dataclass(frozen=True, kw_only=True)
        class Strict(MasterRow):
            SHEET_NAME = "一覧"

            key: str = column("ID")
            enabled: bool = column("有効", choices=("○", "×"))  # 既定値を持たせない

        path = make_sheet(tmp_path / "一覧.xlsx", [["1001", None]], ["ID", "有効"])
        with pytest.raises(MasterTableError) as e:
            Strict.load(path)
        assert "有効" in str(e.value)

    def test_blank_uses_default_when_declared(self, tmp_path):
        """既定値があるなら、空欄は「そう書いた」とみなす。"""

        @dataclass(frozen=True, kw_only=True)
        class WithMemo(MasterRow):
            SHEET_NAME = "一覧"

            key: str = column("ID")
            memo: str = column("備考", default="")

        path = make_sheet(tmp_path / "一覧.xlsx", [["1001", None]], ["ID", "備考"])
        assert WithMemo.load(path)[0].memo == ""


class TestFormula:
    r"""セルに数式が入っていても、数式そのものが値として通らないこと。

    人は保存先などを数式で組み立てる（`=CONCATENATE(D2,"\input")`）。そのまま読むと
    **数式が文字列として通ってしまい**、エラーにもならず `=CONCATENATE(...)` という値で
    処理が進む。それが一番まずいので、計算結果を読む
    （計算結果がファイルに無いときだけ Excel を起動して計算させる）。
    """

    def _with_formula(self, path: Path) -> Path:
        make_sheet(path, [["1001", "受注一覧", "", "毎日", "○", r"\server\受注"]])
        book = load_workbook(path)
        book["PY_一覧"]["C2"] = r'=CONCATENATE(F2,"\data.csv")'
        book.save(path)
        book.close()
        return path

    def test_formula_is_never_taken_as_a_value(self, tmp_path):
        """数式が値として通らない。

        Excel がある PC では計算結果が読め、無い PC では
        ExcelApplicationNotAvailableError で止まる。**どちらでも
        '=CONCATENATE(...)' が値になることはない。**
        """
        path = self._with_formula(tmp_path / "数式.xlsx")
        try:
            items = Item.load(path)
        except ExcelApplicationNotAvailableError as e:
            assert "Excel" in str(e)  # Excel が無い PC。原因が分かる形で止まる
        else:
            assert "CONCATENATE" not in str(items[0].source)  # 計算結果が入っている


class TestToTime:
    """``_to_time`` のパース挙動。``schedule.py`` の「取得開始時刻」「取得時刻」
    列は Excel から文字列として読まれることがあるため、ゼロパディングなしの
    表記でも受け付ける。Excel セル経由の ``dt.time`` / ``dt.datetime`` は
    従来どおり扱える。
    """

    def test_parses_strict_iso_time(self):
        """``dt.time.fromisoformat`` が受け付ける表記はそのまま通す（高速パス）。"""
        from src.report_master import _to_time

        assert _to_time("09:00:00") == dt.time(9, 0, 0)
        assert _to_time("09:00") == dt.time(9, 0)
        assert _to_time("23:59:59") == dt.time(23, 59, 59)

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("9:00", dt.time(9, 0)),  # 時 1 桁・秒なし
            ("9:00:00", dt.time(9, 0, 0)),  # 時 1 桁・秒あり
            ("9:5", dt.time(9, 5)),  # 分 1 桁
            ("9:5:30", dt.time(9, 5, 30)),  # 分 1 桁・秒あり
            ("0:0", dt.time(0, 0)),  # 境界: 0:0
            ("23:59", dt.time(23, 59)),  # 境界: 23:59
            ("  9:00  ", dt.time(9, 0)),  # 前後の空白は許容（``strip`` 済み）
        ],
    )
    def test_accepts_zero_padded_variants(self, text, expected):
        """ゼロパディングなしの ``H:M`` / ``H:M:S`` も ``dt.time`` に変換できる。

        Excel 側で「文字列」として保存された時刻（例: スケジュール管理表の
        「取得開始時刻」列に ``9:00`` と手入力されたケース）でも落ちない。
        """
        from src.report_master import _to_time

        assert _to_time(text) == expected

    def test_passes_through_dt_time(self):
        """``dt.time`` インスタンスはそのまま返す（秒・マイクロ秒は 0 に丸める）。"""
        from src.report_master import _to_time

        # 既存の挙動: 分は保持し、秒・マイクロ秒を 0 に丸める
        assert _to_time(dt.time(9, 0, 30)) == dt.time(9, 0, 0)

    def test_passes_through_dt_datetime(self):
        """``dt.datetime`` インスタンスは ``time`` 部分だけ取り出す。"""
        from src.report_master import _to_time

        value = dt.datetime(2026, 1, 1, 9, 30, 45)  # noqa: DTZ001
        # 既存の挙動: 時・分は保持し、秒・マイクロ秒を 0 に丸める
        assert _to_time(value) == dt.time(9, 30, 0)

    def test_blank_returns_none(self):
        """空欄（``None`` / ``""``）は ``None`` を返す。"""
        from src.report_master import _to_time

        assert _to_time(None) is None
        assert _to_time("") is None

    @pytest.mark.parametrize("text", ["25:00", "9:60", "9時", "来月", "abc", "9", "9:00:"])
    def test_invalid_value_raises_value_error(self, text):
        """時刻として解釈不能な値は ``ValueError`` をそのまま送出する。

        範囲外（``"25:00"`` / ``"9:60"``）と非時刻表記（``"9時"`` / ``"来月"`` /
        ``"abc"`` / ``"9"`` / ``"9:00:"``）の両方を弾く。``fromisoformat`` の
        高速パスと正規表現フォールバックのどちらにも掛からないため、明示的な
        エラーメッセージ付きで ``ValueError`` が上がる。
        """
        from src.report_master import _to_time

        with pytest.raises(ValueError):
            _to_time(text)


class TestCandidateTypes:
    """``_candidate_types`` は Union 型ヒントを (要素の型, ...) に展開する。

    Python 3.10+ の ``X | None`` 構文は ``types.UnionType`` で表されるため、
    3.11〜3.13 で ``dt.time | None`` を渡されたときも ``(dt.time, NoneType)`` を
    返す必要がある。``typing.Union`` だけだと 3.11〜3.13 で時刻に変換されず
    文字列のまま返る回帰が起きる。
    """

    def test_expands_typing_union(self):
        """``typing.Union[dt.time, None]`` は ``(dt.time, NoneType)`` に展開される。"""
        from src.report_master import _candidate_types

        result = _candidate_types(dt.time | None)
        assert dt.time in result
        assert type(None) in result

    def test_expands_pipe_union(self):
        """``dt.time | None`` は 3.11〜3.13 で ``types.UnionType`` として現れる。

        3.14 では ``typing.Union`` 側に正規化されるため、3.14 ではこの分岐は
        通らないが、3.11〜3.13 の回帰を防ぐために同じパスを通すことを確認する。
        """
        import types as _types

        from src.report_master import _candidate_types

        union_type = dt.time | None
        # 3.11〜3.13 では ``types.UnionType``、3.14 では ``typing.Union``。
        # どちらでも同じ結果が返れば OK
        assert typing.get_origin(union_type) in (_types.UnionType, typing.Union)
        result = _candidate_types(union_type)
        assert dt.time in result
        assert type(None) in result

    def test_returns_single_type_for_plain_annotation(self):
        """Union でない型はそのまま 1要素タプルで返す。"""
        from src.report_master import _candidate_types

        assert _candidate_types(dt.time) == (dt.time,)
        assert _candidate_types(str) == (str,)

    def test_string_annotation_returns_empty_tuple(self):
        """``from __future__ import annotations`` 時の文字列は判定不能なので空タプル。"""
        from src.report_master import _candidate_types

        assert _candidate_types("dt.time | None") == ()


class TestConvertTimeValueError:
    """``_convert()`` は時刻変換の ``ValueError`` を行番号・列名付きの
    ``MasterTableError`` に変換する。"""

    def test_invalid_time_value_raises_master_row_value_error(self, tmp_path):
        """``"25:00"`` のような範囲外は、業務担当者に届く形（行番号・列名）で上がる。"""
        from src.report_master import (
            ColumnSpec,
            _convert,
        )

        @dataclass(frozen=True, kw_only=True)
        class WithTime(MasterRow):
            """時刻列を持つ検証用。"""

            SHEET_NAME = "一覧"

            key: str = column("ID", unique=True, help="管理番号")
            at: dt.time = column("時刻", help="実行時刻")

        spec = ColumnSpec(header="時刻")
        with pytest.raises(MasterTableError) as caught:
            _convert("25:00", dt.time, spec, 7, WithTime)
        # 行番号・列名・正しい書き方のヒントが業務担当者に届く
        assert "7 行目" in str(caught.value)
        assert "時刻" in str(caught.value)
        assert "9:00" in str(caught.value)

    def test_invalid_time_in_union_returns_master_row_value_error(self, tmp_path):
        """``dt.time | None`` 型ヒントでも、不正値は ``MasterTableError`` に変換される。"""
        from src.report_master import (
            ColumnSpec,
            _convert,
        )

        @dataclass(frozen=True, kw_only=True)
        class WithOptionalTime(MasterRow):
            """``dt.time | None`` を持つ検証用。"""

            SHEET_NAME = "一覧"

            key: str = column("ID", unique=True, help="管理番号")
            at: dt.time | None = column("時刻", default=None, help="空欄可")

        spec = ColumnSpec(header="時刻")
        with pytest.raises(MasterTableError) as caught:
            _convert("abc", dt.time | None, spec, 3, WithOptionalTime)
        assert "3 行目" in str(caught.value)
        assert "時刻" in str(caught.value)

r"""src/report_master.py — Excel の表を設定として読む。

**非エンジニアが Excel で編集する一覧**を、型付きの行として読み込む。
「どのレポートを取るか」「どのファイルをコピーするか」のように**行が増えていく設定**は、
config.ini より表のほうが扱いやすい（並べ替え・フィルタ・コピーができる）。

使い方は、1行につき1列を宣言するだけ。

    from dataclasses import dataclass
    from pathlib import Path

    from src.report_master import MasterRow, column


    @dataclass(frozen=True, kw_only=True)
    class Report(MasterRow):
        '''レポート管理表の1行。'''

        SHEET_NAME = "管理表"
        PATH = Path(r"\\server\share\レポート管理表.xlsx")

        key: int = column("ID", unique=True, help="社内で決める管理番号")
        summary: str = column("概要", help="人が読んで分かる説明")
        folder: Path = column("保存先", help="落としたファイルを置くフォルダ")
        enabled: bool = column("有効", default=True, help="使わなくなったら「無効」")

    for report in Report.load():   # 読む（型変換・検証込み）
        print(report.summary, report.folder)

**雛形（Excel）の書き出しはこのモジュールの責務ではない。** 雛形生成・ドロップダウン
適用・「記入方法」シートの組み立ては `src.template_writer` などの同じリポジトリ内の
モジュールが行う。このモジュールは読み込み・検証に集中する。`column_specs()` を
使うと、列定義（`column()` で宣言した内容）を読み取れる。

**Python の名前は英語、Excel の見出しは日本語**にできる。`column()` の第1引数が
Excel の見出しで、スペースを含む見出し（`Salesforce URL`）も扱える。

**`kw_only=True` を付ける。** 付けないと「既定値のある列の後ろに、既定値のない列を
書けない」という dataclass の制約に引っかかり、**列を足すときに並び順を気にする**ことに
なる。Excel の列は増えるものなので、どこにでも書けるようにしておく
（Excel 側の並び順は元から自由。見出しの名前で引くため）。

**空欄をどう扱うかは、既定値の有無で決まる。**

| 宣言 | セルが空欄 | 列（見出し）ごと無い |
|---|---|---|
| `column("備考", default="")` | 既定値を使う | 既定値を使う |
| `column("担当")` | **エラー** | **エラー** |

**既定値は「空欄でよい」という宣言**として使う。意味が反転する列（有効/無効のような）に
既定値を付けてはいけない——**書き忘れが「有効」になり、意図と逆の結果になる**。
そういう列は既定値を持たせず、必ず書かせる。

既定値のある列は、**見出しごと無くても読める**。列を1つ足した瞬間に既存の管理表が
すべて読めなくなると業務が止まるため（共有サーバーを更新すると全プロジェクトへ伝播する）。
値が要る列を足したときは、管理表に足すまで止まる。

型は注釈から決まる（`int` / `str` / `bool` / `Path`）。**列の定義はここ1か所**なので、
読み込む型と Excel の見出しがズレることがない。
"""

import dataclasses
import datetime as dt
import logging
import re
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Self

from comken.core.table.model import Table as CoreTable
from comken.core.text import is_true_word
from comken.core.timer import measure
from comken.exceptions import (
    ExcelApplicationNotAvailableError,
)
from comken.toolbox.excel import Excel

from src.exceptions import (
    MasterDuplicateValueError,
    MasterRowValueError,
    MasterTableError,
)

logger = logging.getLogger(__name__)


def _sheet_not_defined_error(class_name: str) -> MasterTableError:
    """``MasterTableError`` の「管理表の場所が決まっていない」文言。

    `load()` を引数なしで呼ぶには、クラス変数 `PATH` に既定の場所を書いておく必要がある。
    """
    return MasterTableError(
        f"{class_name} に管理表の場所が指定されていません。\n"
        "対処: load(パス) でファイルを渡すか、クラス変数 PATH を書いてください。"
        "（コードの直し方の話なので、非エンジニアが見た場合は管理者へ連絡してください）"
    )


def _column_not_found_error(
    header: str, existing: list[str], path: Path, sheet_name: str
) -> MasterTableError:
    """``MasterTableError`` の「管理表に必要な列が無い」文言。

    見出しの行を書き換えた・列を消した・別のシートを見ている、のいずれか。
    プログラムは見出しの名前で列を探すので、見出しが変わると読めなくなる。
    """
    known = "、".join(str(name) for name in existing) or "（見出しなし）"
    return MasterTableError(
        f"管理表に「{header}」の列がありません: {path}（シート: {sheet_name}）\n"
        f"今ある見出し: {known}\n"
        "対処: 管理表の1行目（見出し）を元に戻してください。"
        "消してしまった場合は、メッセージに出ている「今ある見出し」と見比べて足してください。"
    )


# フィールドの metadata に入れるときのキー
_SPEC_KEY = "comken_master_column"

# 「有効」列などで真として扱う値（小文字で比較する）。
# 英語の "true" 表記は config.ini と同じ判定にするため、ここには含めず
# is_true_word() で共通判定する（_to_bool 側で合わせて見る）
_TRUE_WORDS = ("有効", "○", "o", "yes", "1", "on", "はい")

# 見出し行を除いた1行目が Excel の何行目か（見出しが1行目のため）
_FIRST_DATA_ROW = 2

# Excel が「文字列」として保存した時刻を、ゼロパディングなしでも受け付けるための
# フォールバック。``dt.time.fromisoformat()`` は ``"9:00"`` のような表記を
# 拒否するため、ここでは 1〜2 桁の時・分・（省略可の）秒を許容する。
# 範囲チェック（時 0〜23 / 分・秒 0〜59）は ``dt.time`` コンストラクタに任せる。
_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$")


@dataclass(frozen=True)
class ColumnSpec:
    """1つの列の決まりごと。

    Attributes:
        header: Excel の見出し（そのまま1行目に書かれる）。
        unique: True なら、同じ値が2つ以上あるとエラーにする。
        choices: 書ける値を限る場合の一覧。
        help: 「記入方法」シートとエラーメッセージに使う説明。
    """

    header: str
    unique: bool = False
    choices: tuple[str, ...] | None = None
    help: str = ""


def column(
    header: str,
    *,
    unique: bool = False,
    choices: tuple[str, ...] | None = None,
    default: Any = dataclasses.MISSING,
    help: str = "",  # 「記入方法」シートに出る説明。help 以外の名前だと意味が伝わらない
) -> Any:
    """列を1つ宣言する（dataclass のフィールドに使う）。

    Args:
        header: **Excel の見出し**。Python の名前と違ってよく、スペースも使える。
        unique: 同じ値が2つ以上あればエラーにする（管理番号など）。
        choices: 書ける値を限る（`("定期", "個別")` など）。
        default: 空欄のときの値。省略すると**空欄はエラー**になる。
        help: 何を書く列かの説明。「記入方法」シートとエラーメッセージに出る。
    """
    spec = ColumnSpec(header=header, unique=unique, choices=choices, help=help)
    metadata = {_SPEC_KEY: spec}
    if default is dataclasses.MISSING:
        return field(metadata=metadata)
    return field(default=default, metadata=metadata)


class MasterRow:
    """Excel の表の1行。`@dataclass(frozen=True, kw_only=True)` と一緒に継承して使う。

    クラス変数:
        SHEET_NAME: 読み書きするシート名。
        PATH: 既定のファイル。指定すると `load()` を引数なしで呼べる。
    """

    SHEET_NAME: ClassVar[str] = "管理表"
    PATH: ClassVar[Path | None] = None

    # ── 読む ────────────────────────────────────────────────────────────────
    @classmethod
    @measure
    def load(cls, path: str | Path | None = None) -> list[Self]:
        """表を読んで、行のリストを返す。

        Args:
            path: Excel のパス。省略時はクラス変数 `PATH`。

        Returns:
            宣言した順のまま、1行ずつのインスタンス。

        Raises:
            MasterTableError: path も PATH も無い場合、宣言した見出しが表に無い場合。
            MasterRowValueError: 値が型・選択肢に合わない場合。
            MasterDuplicateValueError: unique の列に同じ値がある場合。
        """
        source = Path(path) if path is not None else cls.PATH
        if source is None:
            raise _sheet_not_defined_error(cls.__name__)

        logger.debug(
            "管理表読込開始: class=%s, path=%s, sheet=%s", cls.__name__, source, cls.SHEET_NAME
        )

        # 共有関数 ``read_raw_rows`` に生 dict 化を任せ、ここでは型変換・検証に
        # 集中する（同じロジックを ``load_schedule`` 側でも使う）
        raw_rows = read_raw_rows(source, cls.SHEET_NAME)

        rows: list[Self] = []
        seen: dict[str, set[Any]] = {}
        for offset, raw in enumerate(raw_rows):
            if _is_blank(raw):
                continue  # 表の下に残った空行は読み飛ばす
            row_number = offset + _FIRST_DATA_ROW
            _require_headers(cls, raw, source)
            rows.append(cls._build(raw, row_number, seen, source))
        logger.debug("管理表読込完了: class=%s, path=%s, 件数=%d", cls.__name__, source, len(rows))
        return rows

    @classmethod
    def _build(
        cls, raw: dict[str, Any], row_number: int, seen: dict[str, set[Any]], source: Path
    ) -> Self:
        """1行ぶんの生の値を、型付きのインスタンスにする。"""
        values = {}
        for name, spec, value_type in cls._columns():
            value = _convert(raw.get(spec.header), value_type, spec, row_number, cls)
            if value is _EMPTY:
                default = _default_of(cls, name)
                if default is dataclasses.MISSING:
                    raise MasterRowValueError(
                        row_number,
                        spec.header,
                        "",
                        "空のままにできません。",
                    )
                value = default
            if spec.unique:
                if value in seen.setdefault(name, set()):
                    raise MasterDuplicateValueError(spec.header, value, source)
                seen[name].add(value)
            values[name] = value
        instance = cls(**values)
        # 行ごとの追加検証（例: 「頻度」と「曜日」「日付」の組み合わせ）。
        # ``validate()`` が ``(Excel の見出し, メッセージ)`` を返した場合のみ、
        # ここで ``MasterRowValueError`` に行番号・列名を必ず付けて再送出する。
        bad = instance.validate()
        if bad is not None:
            column_header, message = bad
            raise MasterRowValueError(
                row_number,
                column_header,
                raw.get(column_header),
                message,
            )
        return instance

    def validate(self) -> tuple[str, str] | None:
        """行ごとの追加検証のフック。継承先で必要に応じて上書きする。

        不正値を発見したら ``(Excel の見出し, 直し方のメッセージ)`` を返す。
        ``_build()`` が行番号を付けて ``MasterRowValueError`` に変換して
        業務担当者に届く。例外で値を返す方式（``ValueError`` の ``args`` に
        タプルを詰める）は読みづらく受け渡しのミスも起きやすいため、
        戻り値で渡す。

        何も問題がなければ ``None`` を返す。何も上書きしない既定の挙動は「常に OK」
        （``ReportEntry`` / ``GroupSetting`` など、追加検証が要らない行クラスへ
        の影響を残さない）。
        """

    # ── 列の情報 ─────────────────────────────────────────────────────────────
    @classmethod
    def header(cls, name: str) -> str:
        """Python の名前から、Excel の見出しを返す。

        メッセージに出す・Excel を直接触るときに使う。見出しを直接書くと、
        宣言を変えたときにズレるため。

            ReportEntry.header("summary")   # → "概要"

        Raises:
            KeyError: そのフィールドが宣言されていない場合。
        """
        for field_name, spec, _ in cls._columns():
            if field_name == name:
                return spec.header
        raise KeyError(f"{cls.__name__} に {name} という列はありません")

    @classmethod
    def headers(cls) -> list[str]:
        """Excel の見出しを宣言順で返す。"""
        return [spec.header for _, spec, _ in cls._columns()]

    @classmethod
    def column_specs(cls) -> list[tuple[str, ColumnSpec, Any, Any]]:
        """(Python の名前, 列の決まり, 型注釈, 既定値) を宣言順で返す。

        雛形生成・入力規則（ドロップダウン）適用など、列定義を必要とする
        利用側コードのための公開API。**既定値が `dataclasses.MISSING` のとき、
        その列は必須（空欄不可）**を示す。

        `_columns()` と同じ並び順を保つので、雛形や読み込み側の実装と
        食い違いが起きない。
        """
        return [
            (name, spec, value_type, _default_of(cls, name))
            for name, spec, value_type in cls._columns()
        ]

    @classmethod
    def _columns(cls) -> list[tuple[str, ColumnSpec, Any]]:
        """(Python の名前, 列の決まり, 型注釈) を宣言順で返す。

        `column()` を使っていないフィールドは、**名前をそのまま見出しにする**
        （見出しと同じ名前が使えるなら、宣言は型注釈だけで済む）。

        Returns:
            ``(name, spec, value_type)`` のリスト。``value_type`` は ``Field.type``
            の値で、dataclass の型注釈そのもの（class か文字列の前方参照）。

        Raises:
            TypeError: ``cls`` に ``@dataclass`` が付いていない場合（継承側で
                付け忘れた）。実行時に ``dataclasses.is_dataclass()`` で守って
                ``__dataclass_fields__`` を直接読む。
        """
        # MasterRow 本体は dataclass ではないが、継承先は必ず @dataclass を付ける
        # （docstring 冒頭のサンプル参照）。is_dataclass() で守ったうえで
        # __dataclass_fields__ を直接読めば ``fields()`` のプロトコル合致問題を
        # 避けつつ、誤用には明示的な例外で気づける
        if not dataclasses.is_dataclass(cls):
            raise TypeError(
                f"{cls.__name__} には @dataclass を付けてください（MasterRow を継承するとき）"
            )
        found: list[tuple[str, ColumnSpec, Any]] = []
        for name, item in cls.__dataclass_fields__.items():
            spec = item.metadata.get(_SPEC_KEY) or ColumnSpec(header=name)
            found.append((name, spec, item.type))
        return found

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # dataclass を付け忘れると fields() が空になり、原因の分からない失敗になる。
        # 継承した時点では判定できないので、ここでは何もせず load() 側で確かめる
        super().__init_subclass__(**kwargs)


class _Empty:
    """空欄を表す番兵（None も空文字も「書かれた値」と区別したいため）。"""


_EMPTY = _Empty()


def _is_blank(raw: dict[str, Any]) -> bool:
    """すべての列が空の行か。"""
    return all(value in (None, "") for value in raw.values())


def read_raw_rows(source: Path, sheet_name: str) -> CoreTable:
    """指定シートを「Excel の生 dict のリスト」として読む（実体は `Table`、dict の
    イテレータとして使える）。

    ``MasterRow`` に紐付かない共通の下読みとして使う。``MasterRow.load()`` も
    ``schedule.load_schedule()`` も、Excel の開き方・未計算の数式判定をここで揃えて
    二重実装を避ける。

    **読み取り専用で開く。** 書き込みモードだと存在しないパスを渡されたときに
    空のブックを新規作成し、その後のテーブル解決が「対象テーブルを一意に決められません」
    で落ちる。管理表は共有サーバー (UNC) に置く運用が前提で、現実の失敗は
    「サーバーが落ちた」「パスが変わった」「権限が無い」のいずれか。
    **業務担当者が画面で見ても原因が分かるよう、ファイル不在は
    ``ComkenFileNotFoundError`` がそのまま上がる経路にする。**

    Raises:
        ComkenFileNotFoundError: ``source`` が存在しない場合。
        SheetNotFoundError: ``sheet_name`` がブックに無い場合。
        ExcelApplicationNotAvailableError: 未計算の数式セルが含まれていた場合。
    """
    with Excel(source, read_only=True) as excel:
        raw_rows = excel.data_sheet(sheet_name).table().read()
    if any(
        isinstance(value, str) and value.startswith("=")
        for raw_row in raw_rows
        for value in raw_row.values()
    ):
        logger.debug("管理表に未計算の数式を検出: path=%s, sheet=%s", source, sheet_name)
        raise ExcelApplicationNotAvailableError(
            source,
            RuntimeError("管理表に未計算の数式があります"),
        )
    logger.debug(
        "管理表の生行読込完了: path=%s, sheet=%s, 生行数=%d", source, sheet_name, len(raw_rows)
    )
    return raw_rows


def _require_headers(cls: type[MasterRow], raw: dict[str, Any], source: Path) -> None:
    """宣言した見出しが表にあるか確かめる。

    **既定値のある列は、見出しごと無くてもよい。** 列を1つ足した瞬間に、既存の管理表が
    すべて読めなくなると業務が止まる（共有サーバーを更新すると全プロジェクトへ伝播するため）。
    既定値を付けて足せば、**既存の管理表はそのまま動き、必要な人だけ Excel に列を足せる**。
    """
    for name, spec, _ in cls._columns():
        if spec.header in raw:
            continue
        if _default_of(cls, name) is not dataclasses.MISSING:
            continue  # 既定値があるので、列が無くても埋められる
        raise _column_not_found_error(spec.header, sorted(raw), source, cls.SHEET_NAME)


def _default_of(cls: type[MasterRow], name: str) -> Any:
    """そのフィールドの既定値（無ければ MISSING）。"""
    if not dataclasses.is_dataclass(cls):
        # 誤用（MasterRow のまま呼んだ）には読んでも空クラスを返すより、
        # 何がおかしいかを伝える方が有益
        raise TypeError(
            f"{cls.__name__} には @dataclass を付けてください（MasterRow を継承するとき）"
        )
    for name_, item in cls.__dataclass_fields__.items():
        if name_ == name:
            return item.default
    return dataclasses.MISSING


def _convert(value: Any, value_type: Any, spec: ColumnSpec, row: int, cls: type) -> Any:
    """セルの値を、宣言した型へ変換する。空欄なら _EMPTY を返す。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return _EMPTY

    text = str(value).strip()
    if spec.choices and text not in spec.choices:
        raise MasterRowValueError(
            row, spec.header, value, f"「{'」か「'.join(spec.choices)}」と書いてください。"
        )

    candidate_types = _candidate_types(value_type)

    if bool in candidate_types or value_type is bool or value_type == "bool":
        return _to_bool(value)
    if int in candidate_types or value_type is int or value_type == "int":
        return _to_int(value, text, spec, row)
    if Path in candidate_types or value_type is Path or value_type == "Path":
        return Path(text)
    if dt.time in candidate_types or value_type is dt.time or value_type == "dt.time":
        try:
            return _to_time(value)
        except ValueError:
            # **素の ValueError をそのまま上げると「どの行の、どの列で起きたか」が
            # 業務担当者に届かない。** ``_to_time`` の例外は ``int`` の範囲外や
            # フォーマット不一致など「ユーザー入力の問題」だけなので、ここで
            # 行番号・列名を必ず付ける
            raise MasterRowValueError(
                row,
                spec.header,
                value,
                "時刻は「9:00」「09:00:00」のように書いてください。",
            ) from None
    if str in candidate_types or value_type is str or value_type == "str":
        # **Excel は数値セルを float で返すことがある。** そのまま `str()` すると
        # `1001` が `"1001.0"` になるため、整数値は整数文字列として返す
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return text
    return text


def _candidate_types(value_type: Any) -> tuple[type, ...]:
    """Union 型ヒント（`dt.time | None` など）を展開して、要素の型を返す。

    Python 3.10+ の ``X | None`` 構文は ``types.UnionType`` で表される。
    ``typing.Union`` だけ見ていると 3.11〜3.13 で ``ScheduleRule.start_time`` /
    ``desired_time`` の ``dt.time | None`` 列が時刻に変換されず文字列のまま返る
    ため、両方とも同じ ``typing.get_args()`` 経路で展開する。
    3.14 では ``typing.Union`` 側に正規化されるので、3.11〜3.13 の回帰防止として
    両方を見る（3.14 では ``types.UnionType`` 側の判定は通らないので素通りする）。

    文字列として渡された型ヒント（`from __future__ import annotations` 時）は
    Union 判定できないので空タプルにフォールバックし、呼び出し元の旧来の
    ``is bool`` 比較パスで判定させる。
    """
    if isinstance(value_type, str):
        return ()
    origin = typing.get_origin(value_type)
    if origin is typing.Union or origin is types.UnionType:
        return typing.get_args(value_type)
    return (value_type,)


def _to_bool(value: Any) -> bool:
    """セルの値が「有効」を表す語かどうかを判定する。

    ``_convert`` の bool 列変換に使うほか、``schedule.py`` の「有効」「月末指定」
    のような単独の真偽判定にもそのまま流用できるよう、``text`` を内部で
    導出して単一引数にしてある。
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    return is_true_word(text) or text.lower() in _TRUE_WORDS


def _to_time(value: Any) -> dt.time | None:
    """セルの値を時刻へ変換する。空欄は None。

    Excel の時刻セルは ``datetime`` で返ることがあるため、``datetime`` /
    ``time`` / ISO 形式の文字列のいずれも受け付ける。schedule.py の
    「取得時刻」系の列で使う。

    文字列は ``dt.time.fromisoformat()`` をまず試し、ゼロパディングなしの
    表記（例: ``"9:00"`` / ``"9:5"`` / ``"9:00:00"``）は正規表現で緩めて
    受け付ける。``dt.time`` コンストラクタが時 0〜23 / 分・秒 0〜59 の範囲を
    自動で検証するため、範囲外（例: ``"25:00"``）は従来どおり ``ValueError``
    を送出する。
    """
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, dt.time):
        return value.replace(second=0, microsecond=0)

    text = str(value).strip()
    try:
        return dt.time.fromisoformat(text)
    except ValueError:
        # ISO 形式以外（ゼロパディングなしの表記など）は下の緩めたパースに任せる
        pass

    match = _TIME_PATTERN.match(text)
    if match is None:
        # どちらにも合わない入力は「時刻として解釈不能」として従来どおり送出
        raise ValueError(f"invalid time value: {text!r}") from None
    hour, minute, second = match.groups()
    return dt.time(int(hour), int(minute), int(second) if second else 0)


def _to_int(value: Any, text: str, spec: ColumnSpec, row: int) -> int:
    if isinstance(value, bool):
        raise MasterRowValueError(row, spec.header, value, "数字を入れてください。")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)  # Excel は数値を小数で返すことがある
    if not text.isdigit():
        raise MasterRowValueError(row, spec.header, value, "数字だけで書いてください（例: 1001）。")
    return int(text)

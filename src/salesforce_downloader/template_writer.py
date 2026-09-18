"""src/salesforce_downloader/template_writer.py — 雛形生成（管理表・スケジュール・設定）。

**2026-09 に comken から切り出した。** comken の `report_master.py` には雛形生成の
API が無い（読み込み・検証に集中する）。雛形生成は利用側プロジェクトの運用ニーズに
合わせる必要があるため、ここに置く。

雛形は次の3つの関数で生成する:

- `create_template(path, row_cls, examples)` — 1クラス分の雛形を生成
- `apply_schedule_dropdowns(path)` — 「スケジュール」シートにドロップダウンだけ後付け
- `create_combined_workbook(path)` — 「管理表」「スケジュール」「設定」の3シートを
  1 つの Excel ブックにまとめて生成

記入例の背景色・Noto Sans JP フォント・選択列のドロップダウンなどの挙動は
旧 `MasterRow.create_template()` を踏襲する（comken 側の旧実装は 2026-09 に
このモジュールへ移設された）。
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any

from comken.constants import Color
from comken.core.table.model import Table as CoreTable
from comken.exceptions import ExcelFileNotFoundError, SheetNotFoundError
from comken.services.salesforce_downloader.report_master import ColumnSpec, MasterRow
from comken.services.salesforce_downloader.sheets.group_settings import GroupSetting
from comken.services.salesforce_downloader.sheets.master import EXAMPLES as REPORT_ENTRY_EXAMPLES
from comken.services.salesforce_downloader.sheets.master import ReportEntry
from comken.services.salesforce_downloader.sheets.schedule import SCHEDULE_SHEET_NAME, ScheduleRule
from comken.toolbox.excel import Excel
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

logger = logging.getLogger(__name__)

# 雛形の全セルに付けるフォント名。Windows 標準ではないため、入っていない PC では
# Excel が代替フォントを使う（これは予想される動作で、意図した見た目にならずとも
# 雛形自体は問題なく使える）
_TEMPLATE_FONT_NAME = "Noto Sans JP"

# ドロップダウンを適用するデータ行数。**行を足したときにも効くよう**、雛形時点での
# 想定行数より十分大きく取っておく（テンプレから1000行以上増える運用は基本無い）
_DATA_VALIDATION_ROWS = 1000

# 記入例の行に付ける背景色。本物のデータと見分けが付くよう、薄い灰色系で
# `Color.LIGHT_GRAY` を使う（強く出すと「エラー行」に見えるため）
_EXAMPLE_FILL_COLOR = Color.LIGHT_GRAY
_EXAMPLE_FILL = PatternFill(fill_type="solid", fgColor=_EXAMPLE_FILL_COLOR)

# 見出し行を除いた1行目が Excel の何行目か（見出しが1行目のため）
_FIRST_DATA_ROW = 2

# ドロップダウンを適用する行数（見出しの次の行から）。あとから行を足しても
# 効くよう、十分な行数を確保する
_SCHEDULE_DROPDOWN_ROW_COUNT = 1000

# 「管理表」シート向けの案内文（旧 `ReportEntry.GUIDE_INTRO` を移設）
REPORT_ENTRY_GUIDE_INTRO = (
    f"「{ReportEntry.SHEET_NAME}」シートに行を足すだけで、新しいレポートを取得できます。"
    "プログラム（コード）を直す必要はありません。"
)

# 雛形用の「スケジュール」シート向け記入例（`ScheduleRule` に既存 EXAMPLES が無いので
# ここで簡単なものを用意する）
SCHEDULE_EXAMPLES: list[dict[str, Any]] = [
    {
        "schedule_key": "S001",
        "report_key": "1001",
        "frequency": "毎日",
        "start_time": None,
        "desired_time": None,
        "raw_weekday": "",
        "raw_day_of_month": "",
        "holiday_policy": "取得しない",
        "enabled": True,
    },
]

# 雛形用の「設定」シート向け記入例
GROUP_SETTING_EXAMPLES: list[dict[str, Any]] = [
    {
        "group": "営業本部",
        "base_path": Path(r"\\server\share\営業本部"),
    },
]


def create_template(
    path: str | Path,
    row_cls: type[MasterRow],
    examples: list[dict] | None = None,
) -> Path:
    """1クラス分の雛形（記入例・「記入方法」シート付き）を生成する。

    **空の表を渡されるより、1行埋まっているほうが何をどう書くか伝わる**ので、
    記入例を入れておく（使う前に消す案内も「記入方法」に書く）。

    **`choices` を宣言した列には、自動でドロップダウン（Excel の入力規則）が付く。**
    ドロップダウン・案内文・エラーメッセージは `column()` の `choices` と `help` から
    組み立てるため、宣言を1か所に保ったまま入力補助が出る。

    **雛形全体のフォントは Noto Sans JP。** 既存のフォント属性（太字など）は、
    フォント名だけ書き換える方式で残す。

    Args:
        path: 作成先（.xlsx）。
        row_cls: 雛形のもとになる `MasterRow` のサブクラス。
        examples: 記入例。{Python の名前: 値} の形で渡す。
    """
    path = Path(path)
    specs = row_cls.column_specs()
    examples = examples or []
    headers = [spec.header for _, spec, _, _ in specs]
    rows = [
        {
            spec.header: _to_cell(example.get(name, ""), spec, value_type)
            for name, spec, value_type, _ in specs
        }
        for example in examples
    ]
    logger.debug(
        "雛形生成開始: class=%s, path=%s, sheet=%s, 記入例=%d 行, 列=%d",
        row_cls.__name__,
        path,
        row_cls.SHEET_NAME,
        len(rows),
        len(headers),
    )

    # 空の雛形でも Excel テーブルを成立させるため、API が要求する見出しだけを
    # 持つ Table を作る。実データが無い場合の仮行は create_table が保持しない。
    template_table = CoreTable(headers, rows)
    with Excel(path) as excel:
        excel.create_data_sheet(row_cls.SHEET_NAME).create_table(row_cls.__name__, template_table)
    logger.debug("雛形: データシート作成: path=%s, sheet=%s", path, row_cls.SHEET_NAME)

    book = load_workbook(path)
    sheet = book[f"PY_{row_cls.SHEET_NAME}"]

    # **全セルに雛形用のフォントを当てる。** 既存のフォント属性（太字など）は
    # そのまま使い回し、`name` だけ書き換える（後勝ちで上書きすると太字まで消える）。
    # データ行（見出し＋記入例＋将来の行追加ぶん）全体に適用するため
    # `_DATA_VALIDATION_ROWS` ぶんの余裕を足した範囲を渡す
    _apply_template_font(sheet, len(rows) + _DATA_VALIDATION_ROWS)
    logger.debug(
        "雛形: テンプレートフォント適用: sheet=%s, 対象=%d 行", row_cls.SHEET_NAME, len(rows)
    )
    # **`choices` がある列にドロップダウンを付ける。** データ行の先頭から
    # 十分な行数ぶんの範囲に適用し、あとから行を足しても効くようにする
    _apply_choice_validations(sheet, specs, len(rows))

    _auto_width(sheet)
    sheet.freeze_panes = "A2"

    # 記入例の行に薄い背景色を付ける。**本物のデータと見分けが付く**ように
    # するための目印で、エラー行に見える色は避ける（強すぎる色は業務側が
    # 「何か起きたのか」と不安になるため）
    for offset in range(len(rows)):
        row_number = _FIRST_DATA_ROW + offset
        for col in range(1, len(headers) + 1):
            sheet.cell(row=row_number, column=col).fill = _EXAMPLE_FILL

    _write_class_guide(book, row_cls, specs)

    # openpyxl が新規ブックに自動で作る空の「Sheet」が残っていれば削除する。
    # create_template() のどのステップからも書き込まれない、pristine な残骸。
    # 万一将来どこかで「Sheet」という名前のシートに実際にデータを書くようになった
    # 場合に誤って消さないよう、A1 が空のときだけ消す
    if "Sheet" in book.sheetnames and book["Sheet"]["A1"].value is None:
        del book["Sheet"]
        logger.debug("雛形: openpyxl 作成の空シート 'Sheet' を削除: path=%s", path)

    book.save(path)
    book.close()
    logger.debug("雛形書込完了: path=%s", path)
    return path


def create_combined_workbook(path: str | Path) -> Path:
    """「管理表」「スケジュール」「設定」の3シートを 1 つの Excel ブックにまとめて生成する。

    **3クラスぶんの列説明を 1 つの「記入方法」シートにまとめる。** シートごとに
    分けると、ブックを開いた担当者が「どれを見ればよいか」が分かりにくくなるため
    （非エンジニアが 1 枚で把握できるようにする）。

    「管理表」シートの冒頭案内（`REPORT_ENTRY_GUIDE_INTRO`）は、3シートまとめた
    「記入方法」シートの先頭に置く（旧 `ReportEntry.GUIDE_INTRO` を移設したもの）。

    Args:
        path: 作成先（.xlsx）。

    Returns:
        作成したブックのパス。

    Note:
        この関数は雛形生成用の関数であり、**実データ管理表（`レポート管理表.xlsx`）
        を上書き生成する実行はしない。** 呼び出し側で運用判断の上、tmp_path
        等を渡して呼び出す想定。
    """
    path = Path(path)
    specs_report = ReportEntry.column_specs()
    specs_schedule = ScheduleRule.column_specs()
    specs_settings = GroupSetting.column_specs()

    logger.debug("3シート雛形生成開始: path=%s", path)

    with Excel(path) as excel:
        _write_data_sheet(excel, ReportEntry, REPORT_ENTRY_EXAMPLES, specs_report)
        _write_data_sheet(excel, ScheduleRule, SCHEDULE_EXAMPLES, specs_schedule)
        _write_data_sheet(excel, GroupSetting, GROUP_SETTING_EXAMPLES, specs_settings)
    logger.debug("3シート雛形: データシート作成: path=%s", path)

    book = load_workbook(path)
    _finalize_sheet(book, f"PY_{ReportEntry.SHEET_NAME}", specs_report, REPORT_ENTRY_EXAMPLES)
    _finalize_sheet(
        book, f"PY_{ScheduleRule.SHEET_NAME}", specs_schedule, SCHEDULE_EXAMPLES
    )
    _finalize_sheet(
        book, f"PY_{GroupSetting.SHEET_NAME}", specs_settings, GROUP_SETTING_EXAMPLES
    )

    _write_combined_guide(
        book,
        [
            (ReportEntry, specs_report, REPORT_ENTRY_GUIDE_INTRO),
            (ScheduleRule, specs_schedule, ""),
            (GroupSetting, specs_settings, ""),
        ],
    )

    if "Sheet" in book.sheetnames and book["Sheet"]["A1"].value is None:
        del book["Sheet"]

    book.save(path)
    book.close()
    logger.debug("3シート雛形書込完了: path=%s", path)
    return path


def apply_schedule_dropdowns(path: str | Path) -> None:
    """既存の「スケジュール」シートの列に、Excel のドロップダウン（入力規則）を付ける。

    **シート自体は作らない。** 「スケジュール」シートは手で作る運用のため、
    ここでは既にあるシートに対して入力規則だけを追加・上書きする。見出し行
    （1行目）を読んで列位置を探すので、列の並び順は問わない。

    ドロップダウンを付ける対象は `ScheduleRule.column_specs()` から動的に拾う
    （``choices`` が宣言された列）。`祝日対応` は「取得しない」「取得する」の
    2値プルダウン（`HOLIDAY_SKIP` / `HOLIDAY_FETCH`）。「曜日」「日付」「取得時刻」
    （記録用）は自由記述のため対象外。

    Args:
        path: 「スケジュール」シートを持つ Excel ファイル（既存）。

    Raises:
        ExcelFileNotFoundError: ``path`` が存在しない場合。
        SheetNotFoundError: 「スケジュール」シートが無い場合。
    """
    source = Path(path)
    if not source.exists():
        raise ExcelFileNotFoundError(source)
    book = load_workbook(source)
    # `comken.toolbox.excel.Excel.create_data_sheet()` は `PY_` プレフィックスを
    # 自動付与するため、シート探索も `PY_` 付きで行う。プレフィックス無しの
    # 旧シート名もフォールバックとして受け付ける（手作業で「スケジュール」と
    # 付けた既存ブックへの後付けを想定）
    prefixed_sheet_name = f"PY_{SCHEDULE_SHEET_NAME}"
    if prefixed_sheet_name in book.sheetnames:
        sheet_name = prefixed_sheet_name
    elif SCHEDULE_SHEET_NAME in book.sheetnames:
        sheet_name = SCHEDULE_SHEET_NAME
    else:
        raise SheetNotFoundError(SCHEDULE_SHEET_NAME, book.sheetnames)
    sheet = book[sheet_name]

    last_row = 1 + _SCHEDULE_DROPDOWN_ROW_COUNT
    applied = 0
    # 列宣言 (`column_specs()`) から {見出し: choices} を組み立て、見出し名でシートに
    # 存在する列だけにドロップダウンを当てる。`ScheduleRule.column_specs()` 経由で
    # 宣言を1か所に保つ（`column()` の宣言を足せば自動でドロップダウンが付く）
    choices_by_header: dict[str, tuple[str, ...]] = {
        spec.header: spec.choices
        for _, spec, _, _ in ScheduleRule.column_specs()
        if spec.choices
    }
    # 既定値のない列は `allow_blank=False` にする（ドロップダウンからの空欄提出を
    # 許さない）。既定値の有無は `column_specs()` の 4 要素目から取る
    allow_blank_by_header: dict[str, bool] = {
        spec.header: default is not dataclasses.MISSING
        for _, spec, _, default in ScheduleRule.column_specs()
    }
    header_row = next(sheet.iter_rows(min_row=1, max_row=1))
    for column_index, cell in enumerate(header_row, start=1):
        header = str(cell.value) if cell.value is not None else ""
        choices = choices_by_header.get(header)
        if choices is None:
            continue
        letter = _column_letter(column_index)
        validation = DataValidation(
            type="list",
            formula1=f'"{",".join(choices)}"',
            allow_blank=allow_blank_by_header.get(header, False),
            showDropDown=False,
            showErrorMessage=True,
            errorTitle="書き方が違います",
            error=f"『{'』か『'.join(choices)}』のいずれかを入力してください。",
        )
        validation.add(f"{letter}2:{letter}{last_row}")
        sheet.add_data_validation(validation)
        applied += 1

    book.save(source)
    book.close()
    logger.debug(
        "スケジュールシートへドロップダウンを適用しました: path=%s, 列数=%d", source, applied
    )


# ── 内部ヘルパー ──────────────────────────────────────────────────────────


def _write_data_sheet(
    excel: Excel,
    row_cls: type[MasterRow],
    examples: list[dict],
    specs: list[tuple[str, ColumnSpec, Any, Any]],
) -> None:
    """`excel` インスタンスへ1シート分のデータシートを書き出す。"""
    headers = [spec.header for _, spec, _, _ in specs]
    rows = [
        {
            spec.header: _to_cell(example.get(name, ""), spec, value_type)
            for name, spec, value_type, _ in specs
        }
        for example in examples
    ]
    template_table = CoreTable(headers, rows)
    excel.create_data_sheet(row_cls.SHEET_NAME).create_table(row_cls.__name__, template_table)


def _finalize_sheet(
    book: Workbook,
    full_sheet_name: str,
    specs: list[tuple[str, ColumnSpec, Any, Any]],
    examples: list[dict],
) -> None:
    """`create_data_sheet` で書き出したシートに雛形用の装飾を後付けする。"""
    sheet = book[full_sheet_name]
    headers = [spec.header for _, spec, _, _ in specs]
    _apply_template_font(sheet, len(examples) + _DATA_VALIDATION_ROWS)
    _apply_choice_validations(sheet, specs, len(examples))
    _auto_width(sheet)
    sheet.freeze_panes = "A2"
    for offset in range(len(examples)):
        row_number = _FIRST_DATA_ROW + offset
        for col in range(1, len(headers) + 1):
            sheet.cell(row=row_number, column=col).fill = _EXAMPLE_FILL


def _write_class_guide(
    book: Workbook,
    row_cls: type[MasterRow],
    specs: list[tuple[str, ColumnSpec, Any, Any]],
) -> None:
    """1クラス分の「記入方法」シートを書く（`create_template()` から呼ぶ）。"""
    sheet = book.create_sheet("記入方法")
    logger.debug("雛形: ガイドシート作成: class=%s", row_cls.__name__)
    # 冒頭の説明文。設定が無ければ何も書かない（空の欄を増やさない）
    intro = _intro_for(row_cls)
    if intro:
        sheet.cell(row=1, column=1, value=intro)
        # 改行を含む説明文も1セルなので、空ける行数は変えない
        header_row = 3
    else:
        header_row = 1
    for column, value in enumerate(("列", "何を書くか", "書けない場合"), start=1):
        sheet.cell(row=header_row, column=column, value=value)
    for offset, (_, spec, value_type, default) in enumerate(specs, start=1):
        row_number = header_row + offset
        required = default is dataclasses.MISSING
        note = "空欄にできません" if required else "空欄にできます"
        if spec.choices:
            note = f"「{'」か「'.join(spec.choices)}」と書いてください"
        elif value_type is bool:
            note = "「○」か「×」と書いてください"
        for column, value in enumerate((spec.header, spec.help, note), start=1):
            sheet.cell(row=row_number, column=column, value=value)

    last = header_row + len(specs) + 2
    sheet.cell(row=last, column=1, value="注意")
    sheet.cell(row=last, column=2, value="1行目の見出しは変えないでください（列名で読みます）")
    sheet.cell(row=last + 1, column=2, value="記入例の行は、実際に使うときに消してください")
    for column in range(1, 4):
        sheet.cell(row=header_row, column=column).font = Font(bold=True)
    _auto_width(sheet, max_width=80)
    sheet.freeze_panes = f"A{header_row + 1}"

    # 太字属性を**保ったまま**フォント名を差し替える。先に `set_bold` してから
    # `_set_template_font` を呼ぶことで、既存の `bold=True` を引き継げる
    for row in sheet.iter_rows():
        for cell in row:
            _set_template_font(cell)


def _write_combined_guide(
    book: Workbook,
    sections: list[tuple[type[MasterRow], list[tuple[str, ColumnSpec, Any, Any]], str]],
) -> None:
    """3クラスぶんの列説明を 1 つの「記入方法」シートにまとめる。

    先頭に1番目のセクションの `intro`（旧 `ReportEntry.GUIDE_INTRO` 相当）を
    置き、続いて列定義を順番に並べる。各セクションは見出し行（クラス名）で区切る。
    """
    sheet = book.create_sheet("記入方法")
    logger.debug(
        "雛形: 結合ガイドシート作成: classes=%s",
        [cls.__name__ for cls, _, _ in sections],
    )
    current_row = 1
    for index, (row_cls, specs, intro) in enumerate(sections):
        if index == 0 and intro:
            sheet.cell(row=current_row, column=1, value=intro)
            current_row += 2  # 1行空けてから次の行を見出しに
        # セクション見出し: クラス名（シート名ではない。担当者が何の説明か分かるように）
        section_header_row = current_row
        section_label = f"▼ {row_cls.__name__}（{row_cls.SHEET_NAME}）"
        sheet.cell(row=section_header_row, column=1, value=section_label)
        sheet.cell(row=section_header_row, column=1).font = Font(bold=True)
        current_row += 1
        for column, value in enumerate(("列", "何を書くか", "書けない場合"), start=1):
            sheet.cell(row=current_row, column=column, value=value)
            sheet.cell(row=current_row, column=column).font = Font(bold=True)
        current_row += 1
        for _name, spec, value_type, default in specs:
            required = default is dataclasses.MISSING
            note = "空欄にできません" if required else "空欄にできます"
            if spec.choices:
                note = f"「{'」か「'.join(spec.choices)}」と書いてください"
            elif value_type is bool:
                note = "「○」か「×」と書いてください"
            for column, value in enumerate((spec.header, spec.help, note), start=1):
                sheet.cell(row=current_row, column=column, value=value)
            current_row += 1
        current_row += 1  # セクション間空き行

    # 注意書き
    sheet.cell(row=current_row, column=1, value="注意").font = Font(bold=True)
    note1 = "1行目の見出しは変えないでください（列名で読みます）"
    note2 = "記入例の行は、実際に使うときに消してください"
    sheet.cell(row=current_row + 1, column=2, value=note1)
    sheet.cell(row=current_row + 2, column=2, value=note2)
    _auto_width(sheet, max_width=80)
    sheet.freeze_panes = "A2"
    for row in sheet.iter_rows():
        for cell in row:
            _set_template_font(cell)


def _intro_for(row_cls: type[MasterRow]) -> str:
    """クラス別の冒頭案内文を返す。"""
    if row_cls is ReportEntry:
        return REPORT_ENTRY_GUIDE_INTRO
    return ""


def _apply_choice_validations(
    sheet: Worksheet,
    specs: list[tuple[str, ColumnSpec, Any, Any]],
    example_count: int,
) -> None:
    """`choices` を宣言した列に Excel の入力規則（ドロップダウン）を付ける。"""
    last_row = _FIRST_DATA_ROW + example_count - 1 + _DATA_VALIDATION_ROWS
    choice_column_count = 0
    for offset, (_, spec, _, default) in enumerate(specs, start=1):
        if not spec.choices:
            continue  # `choices` を宣言していない列には付けない
        letter = _column_letter(offset)
        choices_text = "、".join(f"「{choice}」" for choice in spec.choices)
        prompt = f"{spec.help}\n書き方: {choices_text}".strip()
        error = f"『{'』か『'.join(spec.choices)}』のいずれかを入力してください。"
        # showDropDown=False は「ボタンを表示する」指定（Excel の API は逆）。
        # 非エンジニアが見て選択できる必要があるため True（=ボタンを表示しない）
        # にはしない
        validation = DataValidation(
            type="list",
            formula1=f'"{",".join(spec.choices)}"',
            allow_blank=default is not dataclasses.MISSING,
            showDropDown=False,
            showErrorMessage=True,
            errorTitle="書き方が違います",
            error=error,
            showInputMessage=True,
            promptTitle=spec.header,
            prompt=prompt,
        )
        validation.add(f"{letter}{_FIRST_DATA_ROW}:{letter}{last_row}")
        sheet.add_data_validation(validation)
        choice_column_count += 1
    logger.debug(
        "雛形: ドロップダウン列=%d / 全列=%d, 適用範囲=%d 行目まで",
        choice_column_count,
        len(specs),
        last_row,
    )


def _apply_template_font(sheet: Worksheet, example_count: int) -> None:
    """雛形（表シート）の全セルに雛形用のフォント名を設定する。

    既存の設定（太字など）は `Font` オブジェクトをそのまま使い回し、`name` だけを
    書き換える。**他の属性（太字・サイズなど）に触らないため、雛形のもともとの
    見出し書式（太字）を崩さない。**
    """
    last_row = max(_FIRST_DATA_ROW + example_count - 1, 1)
    for row in sheet.iter_rows(min_row=1, max_row=last_row, min_col=1, max_col=sheet.max_column):
        for cell in row:
            _set_template_font(cell)


def _to_cell(value: Any, spec: ColumnSpec, value_type: Any) -> Any:
    """記入例をセルに書ける形にする。"""
    if isinstance(value, bool):
        if (value_type is bool or value_type == "bool") and spec.choices:
            return spec.choices[0] if value else spec.choices[1]
        # choices を宣言していない真偽列の既定表記。**案内文（「○」か「×」）と
        # そろえる。** ここだけ「有効/無効」を書くと、記入方法シートの案内と
        # 雛形の中身が食い違い、どちらに従えばよいか分からなくなる。
        return "○" if value else "×"
    if isinstance(value, Path):
        return str(value)
    return value


def _column_letter(index: int) -> str:
    """1 -> A, 27 -> AA。"""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _set_template_font(cell: Any) -> None:
    """セルに雛形用のフォント名（Noto Sans JP）を当てる。

    既存のフォント属性（太字・サイズ・色）はそのまま残し、`name` だけを書き換える。
    **`Font(...)` で全項目を指定すると太字などが消える**ため、openpyxl の現プロパティを
    引き継ぐ形で作る。
    """
    existing = cell.font
    cell.font = Font(
        name=_TEMPLATE_FONT_NAME,
        size=existing.size,
        bold=existing.bold,
        italic=existing.italic,
        color=existing.color,
    )


def _auto_width(sheet: Worksheet, *, max_width: int | None = None) -> None:
    """セル内容に合わせて列幅を設定する。"""
    from openpyxl.utils import get_column_letter

    for column_index, cells in enumerate(sheet.iter_cols(), start=1):
        width = max((len(str(cell.value or "")) for cell in cells), default=0) + 2
        if max_width is not None:
            width = min(width, max_width)
        sheet.column_dimensions[get_column_letter(column_index)].width = width


__all__ = [
    "REPORT_ENTRY_GUIDE_INTRO",
    "SCHEDULE_EXAMPLES",
    "GROUP_SETTING_EXAMPLES",
    "create_template",
    "create_combined_workbook",
    "apply_schedule_dropdowns",
]

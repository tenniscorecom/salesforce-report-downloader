"""src/soql_reports/column_names.py — ``列名の対応.csv`` の読み込み。

``soql-collector``（別プロジェクト）が生成する
``<ベースパス>/soql-collector/列名の対応.csv`` を読んで、管理番号ごとの
``{SOQL列名: 表示名}`` を返すヘルパー。1 ファイルに全レポート分が
積み上がる形式を想定しているので、管理番号で絞り込んで該当する行だけを返す。

**目的:** SOQL 経由のレポートで、``SoqlReport.COLUMN_NAMES`` を 1 つも
書かなくても、見出しが API 名のまま CSV に出る事故を防ぐ（soql-collector
の対応表で人が読む列名が既に揃っているならそのまま使える）。
``SoqlReport.COLUMN_NAMES`` は **手書きで優先**（同じキーがあれば手書きが勝つ）。

**このファイルが持つもの:**
- ``load_column_names_for(report_key, *, csv_path=None)``: 管理番号 -> ``{SOQL列名: 表示名}``
- フォーマット不正用の例外 ``ColumnNamesCSVFormatError``（``src.exceptions`` 由来）

**ここに書かないもの:**
- ``_apply_column_names()`` の本体 → ``src.soql_reports.runner``
- ファイルパスの組み立て → ``src.paths.COLUMN_NAMES_CSV_PATH``

**キャッシュ方針:** SOQL レポートは数本なので、保存のたびに読み直す（YAGNI）。
毎回読んでも無視できるコスト。
"""

from __future__ import annotations

import logging
from pathlib import Path

from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

from src.exceptions import ColumnNamesCSVFormatError
from src.paths import COLUMN_NAMES_CSV_PATH

logger = logging.getLogger(__name__)


# ``列名の対応.csv`` の必須列名。``soql-collector`` との取り決め。
# 名前を変えると soql-collector 側と合わなくなるので、テストの
# フィクスチャ CSV も含めてすべてこの文字列を使う。
# 「概要」は SOQL 結果には無いメタ列なので必須扱いにはしない
# （人が「この行は何のレポートか」を見るときのため）。
_REQUIRED_COLUMNS: tuple[str, ...] = ("管理番号", "SOQL列名", "表示名", "備考")

# 重複の判定に使う固定文字列。``soql-collector`` が同じ管理番号内で
# 表示名がぶつかったときにこの文字列で印を付ける
_DUPLICATE_MARKER = "表示名が重複"


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    """``列名の対応.csv`` を ``comken.toolbox.csv.CSV``（``read_only=True``）で
    読み込み、各行を ``dict`` で返す。

    フォーマット不正（必要な列が無い、見出しすら無い、読めない）は
    ``ColumnNamesCSVFormatError``（``DownloaderError`` 系）で失敗させる。
    呼び出し側は **取得を失敗扱い**にする（ファイル無しや該当行無し
    とは違い、CSV 形式そのものが壊れているので、保存を進めても何が
    正なのか分からないため）。

    見出しだけ有ってデータ行が無いケースは **正常**として空 list を
    返す（``soql-collector`` が走った直後で、まだ該当レポート分の行が
    書かれていないケース）。 ファイル無しはこの関数の外側で扱うので
    ここではファイルが有る前提。
    """
    try:
        with CSV(csv_path, read_only=True) as csv_file:
            table = csv_file.read()
    except CSVError as exc:
        # 見出しが無い／見出しに空欄・重複がある／文字コードを判定
        # できない／指定 encoding が使えない など、 comken 側のフォーマット
        # 不正を ``ColumnNamesCSVFormatError`` に揃えて、利用者へ同じ
        # 「対処」を出させる（メッセージ本文は二重にならないよう ``exc`` を
        # 「原因」欄にだけ書く）
        raise ColumnNamesCSVFormatError(
            f"列名の対応.csv が読めません: {csv_path}\n"
            f"原因: {exc}\n"
            "対処: ファイルが UTF-8 BOM 付き CRLF の CSV か確認してください。"
        ) from exc
    columns = list(table.columns)
    missing = [col for col in _REQUIRED_COLUMNS if col not in columns]
    if missing:
        joined_header = ", ".join(columns) if columns else "（空）"
        raise ColumnNamesCSVFormatError(
            f"列名の対応.csv に必要な列がありません: {csv_path}\n"
            f"不足列: {', '.join(missing)}\n"
            f"現在の見出し: {joined_header}\n"
            "対処: ファイルが UTF-8 BOM 付き CRLF の CSV で、"
            "列名が「管理番号, 概要, SOQL列名, 表示名, 備考」であることを確認してください。"
        )
    return list(table.to_rows())


def load_column_names_for(
    report_key: str,
    *,
    csv_path: Path | str | None = None,
) -> dict[str, str]:
    """``report_key`` に対応する ``{SOQL列名: 表示名}`` を返す。

    戻り値は ``runner._apply_column_names()`` に渡して使う想定。
    **手書きの ``SoqlReport.COLUMN_NAMES`` が優先**なので、呼び出し側で
    ``{**file_dict, **report_cls.COLUMN_NAMES}`` の形でマージする。

    挙動:
        - ``列名の対応.csv`` が無い → 空 dict + WARNING（取得は続行）
        - 見出しだけで行が無い（``soql-collector`` 走り立て直後）→ 空 dict + WARNING（取得は続行）
        - ファイルは有るが ``report_key`` の行が無い → 空 dict + WARNING（取得は続行）
        - 備考に ``表示名が重複`` がある行 → その行を捨てて WARNING に SOQL列名を並べる
        - ``SOQL列名`` / ``表示名`` が空の行 → 飛ばす
        - フォーマット不正 → ``ColumnNamesCSVFormatError``（取得を止める）

    Args:
        report_key: 管理表の「ID」列の値。``SoqlReport.KEY`` と同じもの。
        csv_path: テスト用差し替え口。省略時は ``paths.COLUMN_NAMES_CSV_PATH``。

    Returns:
        ``{SOQL列名: 表示名}`` の dict。1 行も該当しなかったときは ``{}``。
    """
    path = Path(csv_path) if csv_path is not None else COLUMN_NAMES_CSV_PATH
    if not path.exists():
        logger.warning(
            "列名の対応ファイルがありません。API 名のまま保存します: パス=%s",
            path,
        )
        return {}
    rows = _read_rows(path)

    # この管理番号に紐付く行だけを抜き出してチェックする。
    # 「管理番号」のセルが空の行は別管理番号の事故防止のため無視する
    matches: list[dict[str, str]] = []
    for row in rows:
        key = (row.get("管理番号") or "").strip()
        if key == report_key:
            matches.append(row)
    if not matches:
        logger.warning(
            "列名の対応に管理番号=%s の行がありません。"
            "soql-collector で取ってください。API 名のまま保存します: パス=%s",
            report_key,
            path,
        )
        return {}

    result: dict[str, str] = {}
    duplicate_columns: list[str] = []
    for row in matches:
        soql_col = (row.get("SOQL列名") or "").strip()
        display = (row.get("表示名") or "").strip()
        # 必要な列が空の行は飛ばす（事故防止のため例外にはしない）
        if not soql_col or not display:
            continue
        note = (row.get("備考") or "").strip()
        if note == _DUPLICATE_MARKER:
            duplicate_columns.append(soql_col)
            continue
        result[soql_col] = display
    if duplicate_columns:
        logger.warning(
            "列名の対応に表示名が重複している行があるため無視します: 管理番号=%s, SOQL列名=%s",
            report_key,
            ", ".join(duplicate_columns),
        )
    return result


# 公開 API は ``load_column_names_for()`` 1 つだけ
__all__ = ["load_column_names_for"]

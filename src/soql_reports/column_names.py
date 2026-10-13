"""src/soql_reports/column_names.py — ``列名の対応.csv`` の読み込み。

``soql-collector``（別プロジェクト）が生成する
``<ベースパス>/soql-collector/列名の対応.csv`` を読んで、
``(主オブジェクト, SOQL列名)`` ごとに ``表示名`` を返すヘルパー。

**2026-10 改定:** ``列名の対応.csv`` は **全体で 1 ファイル**にまとめ、
**(主オブジェクト, SOQL列名)** が同じならどのレポートでも同じ
``表示名`` を返す形にした（管理番号やレポート ID には依存しない）。
そのためこのモジュールは ``SoqlReport`` サブクラスを管理番号で引かず、
``SoqlReport.soql()`` の SOQL 文字列から **主オブジェクトを取り出して**
``主オブジェクト × SOQL列名`` で ``{SOQL列名: 表示名}`` を返す。

**目的:** SOQL 経由のレポートで、 ``SoqlReport.COLUMN_NAMES`` を 1 つも
書かなくても、見出しが API 名のまま CSV に出る事故を防ぐ（soql-collector
の対応表で人が読む列名が既に揃っているならそのまま使える）。
``SoqlReport.COLUMN_NAMES`` は **手書きで優先**（同じキーがあれば手書きが勝つ）。

**このファイルが持つもの:**
- ``extract_main_object(soql)``: SOQL 文字列から主オブジェクト（最初の ``FROM`` 名）を取り出す
- ``load_column_names_for(main_object, *, csv_path=None)``: 主オブジェクト -> ``{SOQL列名: 表示名}``
- フォーマット不正用の例外 ``ColumnNamesCSVFormatError``（``src.exceptions`` 由来）

**ここに書かないもの:**
- ``_apply_column_names()`` の本体 → ``src.soql_reports.runner``
- ファイルパスの組み立て → ``src.paths.COLUMN_NAMES_CSV_PATH``

**キャッシュ方針:** SOQL レポートは数本なので、保存のたびに読み直す（YAGNI）。
毎回読んでも無視できるコスト。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from comken.exceptions import CSVError
from comken.toolbox.csv import CSV

from src.exceptions import ColumnNamesCSVFormatError
from src.paths import COLUMN_NAMES_CSV_PATH

logger = logging.getLogger(__name__)


# ``列名の対応.csv`` の必須列名。 ``soql-collector`` との取り決め。
# 名前を変えると soql-collector 側と合わなくなるので、テストの
# フィクスチャ CSV も含めてすべてこの文字列を使う。
# 「備考」「更新日」は人が読むメタ列なので必須扱いにはしない
# （「別の表示名あり: …」のようなメモ用途）。
_REQUIRED_COLUMNS: tuple[str, ...] = ("主オブジェクト", "SOQL列名", "表示名")

# SOQL から最初の ``FROM <名前>`` を取り出す正規表現。
# - ``\bFROM\b`` で大文字小文字を区別しない（``from`` / ``From`` も対象）
# - 直後の空白は改行を含む（SOQL が改行を含む前提）
# - 識別子として ``[A-Za-z_][A-Za-z0-9_]*`` を採用（Salesforce の主オブジェクト名
#   は API 名の規約に従うので ``_`` と英数字のみ）
# - バックトラック抑止のため、キャプチャは識別子部分に限定
_FROM_PATTERN = re.compile(
    r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)",
    flags=re.IGNORECASE,
)


def extract_main_object(soql: str) -> str | None:
    """SOQL 文字列から **最初の** ``FROM <名前>`` の ``<名前>`` を返す。

    ``SalesforceBase.bulk_query()`` は **子サブクエリを扱えない**
    （``SELECT Id, (SELECT Id FROM Contacts) FROM Account`` のような形は
    ジョブ作成時に 400 で失敗する）。そのため SOQL 全体の最初の ``FROM`` が
    そのまま **主オブジェクト** になる（同じ SOQL 内に ``FROM`` が複数
    出現することは無い）。

    比較は Salesforce の API 名の仕様に合わせ、大文字小文字を区別しない
    ``re.IGNORECASE`` で取り出す（呼び出し側でもう一段小文字化してから
    比較する想定）。

    Args:
        soql: ``SoqlReport.soql()`` が返す SOQL 文字列（空文字も可）。

    Returns:
        主オブジェクト名（見つかった場合）、 ``None``（``FROM`` が無い、
        SOQL が空文字、 ``FROM`` の直後に識別子が続く形でない 等）。
    """
    if not soql:
        return None
    match = _FROM_PATTERN.search(soql)
    if match is None:
        return None
    return match.group(1)


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    """``列名の対応.csv`` を ``comken.toolbox.csv.CSV`` （``read_only=True``）で
    読み込み、各行を ``dict`` で返す。

    フォーマット不正（必要な列が無い、見出しすら無い、読めない）は
    ``ColumnNamesCSVFormatError`` （``DownloaderError`` 系）で失敗させる。
    呼び出し側は **取得を失敗扱い**にする（ファイル無しや該当行無し
    とは違い、 CSV 形式そのものが壊れているので、保存を進めても何が
    正なのか分からないため）。

    見出しだけ有ってデータ行が無いケースは **正常**として空 list を
    返す（``soql-collector`` が走った直後で、まだ該当行が書かれていない
    ケース）。 ファイル無しはこの関数の外側で扱うので
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
            "列名が「主オブジェクト, SOQL列名, 表示名, 備考, 更新日」であることを確認してください。"
            " 旧形式（管理番号 / 概要 を含む列構成）のファイルが残っていないか確認し、"
            "soql-collector を新しい版で実行し直して 列名の対応.csv を再生成してください。"
        )
    return list(table.to_rows())


def load_column_names_for(
    main_object: str,
    *,
    csv_path: Path | str | None = None,
) -> dict[str, str]:
    """``main_object`` に対応する ``{SOQL列名: 表示名}`` を返す。

    戻り値は ``runner._apply_column_names()`` に渡して使う想定。
    **手書きの ``SoqlReport.COLUMN_NAMES`` が優先**なので、呼び出し側で
    ``{**file_dict, **report_cls.COLUMN_NAMES}`` の形でマージする。

    ``主オブジェクト`` の比較は **大文字小文字を区別しない**（Salesforce
    の API 名は大文字小文字を区別しないため）。 SOQL 文字列から取り出した
    主オブジェクトをそのまま渡してよい。

    挙動:
        - ``列名の対応.csv`` が無い → 空 dict + WARNING （取得は続行）
        - 見出しだけで行が無い（ ``soql-collector`` 走り立て直後）→ 空 dict + WARNING （取得は続行）
        - ファイルは有るが ``main_object`` の行が無い → 空 dict + WARNING （取得は続行）
        - ``SOQL列名`` / ``表示名`` が空の行 → 飛ばす
        - フォーマット不正（旧形式の見出しも含む）→ ``ColumnNamesCSVFormatError``（取得を止める）

    Args:
        main_object: 主オブジェクト名（``extract_main_object()`` が返す名前）。
            大文字小文字は区別しない。
        csv_path: テスト用差し替え口。省略時は ``paths.COLUMN_NAMES_CSV_PATH``。

    Returns:
        ``{SOQL列名: 表示名}`` の dict。1 行も該当しなかったときは ``{}`` 。
    """
    path = Path(csv_path) if csv_path is not None else COLUMN_NAMES_CSV_PATH
    if not path.exists():
        logger.warning(
            "列名の対応ファイルがありません。API 名のまま保存します: パス=%s",
            path,
        )
        return {}
    rows = _read_rows(path)

    # 主オブジェクト名を揃える（大文字小文字を区別しない）。 呼び出し側は
    # ``extract_main_object()`` が返した名前をそのまま渡してくるが、人間が
    # ``主オブジェクト`` 列に直接書いた値も混ざり得るので、両側を ``lower()``
    # してから比較する。
    target = main_object.strip().lower()
    matches: list[dict[str, str]] = []
    for row in rows:
        row_object = (row.get("主オブジェクト") or "").strip().lower()
        if row_object and row_object == target:
            matches.append(row)
    if not matches:
        logger.warning(
            "列名の対応に主オブジェクト=%s の行がありません。"
            "soql-collector で取ってください。API 名のまま保存します: パス=%s",
            main_object,
            path,
        )
        return {}

    result: dict[str, str] = {}
    for row in matches:
        soql_col = (row.get("SOQL列名") or "").strip()
        display = (row.get("表示名") or "").strip()
        # 必要な列が空の行は飛ばす（事故防止のため例外にはしない）
        if not soql_col or not display:
            continue
        result[soql_col] = display
    return result


# 公開 API は ``extract_main_object()`` と ``load_column_names_for()`` の 2 つ
__all__ = ["extract_main_object", "load_column_names_for"]

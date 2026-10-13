"""src/soql_reports/runner.py — ``_apply_column_names()`` の置き場所。

SOQLレポートの取得・保存は **管理表ルートだけ** に統一した。
``src.service._save()`` が保存の直前にこの ``_apply_column_names()`` を
``SoqlReport`` 経由で呼ぶ。管理表を経由しない別ルートは廃止したため、
このモジュールには ``_apply_column_names()`` だけが残る。

``_apply_column_names()`` を ``src.service`` 側に置かないのは、SOQL の
列名変換の責務を ``soql_reports`` パッケージに閉じるため（``SoqlReport``
と ``SoqlColumnNameConflictError`` が同じ場所にあるほうが読みやすい）。

2026-10 に ``soql-collector`` の ``列名の対応.csv`` からの自動取り込みを
追加した。``SoqlReport.COLUMN_NAMES`` の手書きは **そのまま優先**され、
``extra``（``列名の対応.csv`` から読んだ dict）は補助としてマージされる。
"""

from __future__ import annotations

from comken.core.table.model import Table

from src.exceptions import SoqlColumnNameConflictError
from src.soql_reports.base import SoqlReport


def _apply_column_names(
    report_cls: type[SoqlReport],
    table: Table,
    *,
    extra: dict[str, str] | None = None,
) -> Table:
    """``SoqlReport.COLUMN_NAMES`` と ``extra`` をマージ（手書き優先）して
    SOQL の結果列を人が読む見出しへ置き換える。

    マージは ``{**extra, **report_cls.COLUMN_NAMES}`` の形で行うため、
    同じキーが ``extra`` と ``COLUMN_NAMES`` の両方にあると
    ``COLUMN_NAMES``（手書き）が勝つ。手書きは「直したい列だけ書けば
    よい」を意図しているので、soql-collector が拾いそこねた列や、
    意図的に別名にしている列が必ず反映される。

    ``extra`` と ``COLUMN_NAMES`` がどちらも空なら ``table`` をそのまま
    返す（API 名の見出しがそのまま CSV に出る）。

    結果の列に無いキーは無視する（SOQL を後から変えても対応表を貼り直さ
    ずに済むように）。列の並び順は元のまま。置き換え後の見出しが 2 つ
    以上重なる場合は ``SoqlColumnNameConflictError`` を送出する
    （CSV は書かれない）。
    """
    # マージは手書き優先。手書きの ``COLUMN_NAMES`` が無いときだけ
    # ``extra``（``列名の対応.csv`` 由来）が使われる形にする
    column_names: dict[str, str] = dict(extra or {})
    column_names.update(report_cls.COLUMN_NAMES)
    if not column_names:
        return table
    # 元の列順を保ったまま、マージ済みの対応にある列だけ表示名へ差し替える。
    new_columns = [column_names.get(col, col) for col in table.columns]
    # 衝突の見出しを先に集める（``table.select(...)`` に進む前に弾く）。
    # 重複の判定は ``new_columns`` 全体に対して行う。 ``column_names``
    # に登録されていない列も、置き換え後の見出しが他と重なれば衝突になる
    # （CSV に同じ列名が 2 回現れて何が何だかにするため）。 ``sources``
    # には元の列名をそのまま入れる（登録しなかった列も「元はどの列だったか」
    # 衝突の解消で困らないよう含める）。
    conflicts: dict[str, list[str]] = {}
    for original, new in zip(table.columns, new_columns, strict=True):
        # ``new_columns`` は ``str`` のリストだが Pyright の推論上 ``str | None``
        # になり得るので ``str`` だと確定させてから ``setdefault`` に渡す。
        assert new is not None
        conflicts.setdefault(new, []).append(original)
    duplicated = {header: sources for header, sources in conflicts.items() if len(sources) >= 2}
    if duplicated:
        raise SoqlColumnNameConflictError(report_key=report_cls.KEY, conflicts=duplicated)
    # ``table.select(*new_columns, aliases=aliases)`` は new の列名で並べ、
    # ``aliases`` にある new だけ既存の old 名の列から値を拾う。
    # 結果に無いキーは無視したいので、 ``table.columns`` に含まれる old だけ aliases に入れる。
    aliases = {new: original for original, new in column_names.items() if original in table.columns}
    return table.select(*new_columns, aliases=aliases)

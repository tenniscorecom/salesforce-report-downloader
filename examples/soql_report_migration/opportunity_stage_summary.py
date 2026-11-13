"""examples/soql_report_migration/opportunity_stage_summary.py — transform() を使った実装例。

サマリーレポートを Python で組み立てる例（今までサマリーレポートでやっていたものを
``SoqlReport.transform()`` で Python 加工して代わりにする）。
``docs/salesforce-downloader.md``「SOQLレポート（2000件超のレポートを移行する）」の
「書き方」に対応する。

**このファイルの中身はサンプル固有の ``KEY`` と ``soql()`` / ``transform()`` だけ。**
URL・保存先・0件可否は管理表の ``ReportEntry`` 側が決める。管理表の「SOQL」列を
「○」にして同じ管理番号の行を足せば、 ``download_scheduled()`` 経由で取得される。

``src/reports/`` 配下へコピーして使う（ファイル名を ``_`` で
始めない。``_`` で始まるファイルは ``registered_reports()`` の走査対象外）。
置いただけで自動登録される（明示的なタプル編集は不要）。
"""

from typing import ClassVar

from comken.core.table.model import Table

from src.reports.base import SoqlReport


class OpportunityStageSummary(SoqlReport):
    """商談を ``StageName`` ごとに件数と金額合計にまとめるサマリー。

    SOQL 側で ``StageName`` ごとに group_by する代わりに、 Python の
    ``Table.filter()`` / ``group_by()`` で同じ結果を作る例。
    """

    KEY = "9002"  # 社内で決める管理番号（Salesforce のレポートIDではない）
    # ``transform()`` は **表示名の列** を受け取る（列名の置き換えは
    # ``transform()`` の前に行われる）。 集計結果の列名は ``transform()`` の
    # 中でそのまま書けばよい（保存時はその名前で出る）。
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "StageName": "ステージ",
        "Amount": "金額",
    }

    def soql(self) -> str:
        """明細を取ってくる SOQL。 ``StageName`` と ``Amount`` を含むが
        group_by はせず、 加工の ``transform()`` で集計する。"""
        return """
            SELECT
                Id,
                StageName,
                Amount
            FROM Opportunity
            WHERE
                CloseDate >= 2024-01-01
        """

    def transform(self, table: Table) -> Table:
        """``ステージ`` ごとに件数と ``金額`` の合計を集計する。

        ``Table`` の標準 API（``group_by``）だけで書く。 受け取る ``table`` の
        列名は表示名（``COLUMN_NAMES`` で置き換えたもの）なので、 列の指定は
        表示名で行う。
        """
        groups = table.group_by("ステージ")
        rows: list[dict[str, object]] = []
        for stage_name, sub_table in groups.items():
            total = sum(int(row["金額"]) for row in sub_table)
            rows.append(
                {
                    "ステージ": stage_name,
                    "件数": len(sub_table),
                    "金額合計": str(total),
                },
            )
        return Table(["ステージ", "件数", "金額合計"], rows)

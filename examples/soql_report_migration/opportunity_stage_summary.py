"""examples/soql_report_migration/opportunity_stage_summary.py — transform() を使った実装例。

サマリーレポートを Python で組み立てる例（今までサマリーレポートでやっていたものを
``SoqlReport.transform()`` で Python 加工して代わりにする）。
``docs/salesforce-downloader.md``「SOQLレポート（2000件超のレポートを移行する）」の
「書き方」に対応する。

**このファイルの中身はサンプル固有の ``KEY`` と ``soql()`` / ``transform()`` だけ。**
URL・保存先・0件可否は管理表の ``ReportEntry`` 側が決める。管理表の「SOQL」列を
「○」にして同じ管理番号の行を足せば、 ``download_scheduled()`` 経由で取得される。

``src/soql_reports/reports/`` 配下へコピーして使う（ファイル名を ``_`` で
始めない。``_`` で始まるファイルは ``registered_reports()`` の走査対象外）。
置いただけで自動登録される（明示的なタプル編集は不要）。
"""

from typing import ClassVar

from comken.core.table.model import Table

from src.soql_reports.base import SoqlReport


class OpportunityStageSummary(SoqlReport):
    """商談を ``StageName`` ごとに件数と金額合計にまとめるサマリー。

    SOQL 側で ``StageName`` ごとに group_by する代わりに、 Python の
    ``Table.filter()`` / ``group_by()`` で同じ結果を作る例。 pandas に
    依存しないため、 ダウンロード側に pandas が無くても動く。
    """

    KEY = "9002"  # 社内で決める管理番号（Salesforce のレポートIDではない）
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "StageName": "ステージ",
        "件数": "件数",
        "金額合計": "金額合計",
    }

    def soql(self) -> str:
        """明細を取ってくる SOQL。 ``StageName`` と ``Amount`` を含むが
        group_by はせず、 加工の ``transform()`` で集計する。"""
        return "SELECT Id, StageName, Amount FROM Opportunity WHERE CloseDate >= 2024-01-01"

    def transform(self, table: Table) -> Table:
        """``StageName`` ごとに件数と ``Amount`` の合計を集計する。

        pandas（ ``df.groupby(...)["Amount"].sum()`` ）でも同じことができるが、
        ここでは ``Table`` の標準 API だけで書いて依存を増やさない例にしている。
        """
        groups = table.group_by("StageName")
        rows: list[dict[str, object]] = []
        for stage_name, sub_table in groups.items():
            total = sum(int(row["Amount"]) for row in sub_table)
            rows.append(
                {
                    "StageName": stage_name,
                    "件数": len(sub_table),
                    "金額合計": str(total),
                },
            )
        return Table(["StageName", "件数", "金額合計"], rows)

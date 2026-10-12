"""examples/soql_report_migration/large_sales_report.py — SoqlReport の実装例。

Report API（2000行上限）の切り捨てに当たった商談明細レポートを、SOQL で取り直す例。
docs/salesforce-downloader.md「SOQLレポート（2000件超のレポートを移行する）」の
「書き方」に対応する。

**このファイルの中身はサンプル固有の ``KEY`` と ``soql()`` だけ。** URL・保存先・
0件可否は管理表の ``ReportEntry`` 側が決める。管理表の「SOQL」列を「○」にして
同じ管理番号の行を足せば、``download_scheduled()`` 経由で取得される。

``src/soql_reports/reports/`` 配下へコピーして使う（ファイル名を ``_`` で
始めない。``_`` で始まるファイルは ``registered_reports()`` の走査対象外）。
置いただけで自動登録される（明示的なタプル編集は不要）。
"""

from typing import ClassVar

from src.soql_reports.base import SoqlReport


class LargeSalesReport(SoqlReport):
    """商談明細（2000件超のため Report API では切り捨てられ、SOQL で取得する）。"""

    KEY = "9001"  # 社内で決める管理番号（Salesforce のレポートIDではない）
    # 出力 CSV の見出しを人が読む列名に置き換える（任意）。
    # **貼り付け元は soql-collector の対応表**（この例は手で書いています）。
    # キーは ``soql()`` の結果列名（``Account.Name`` のドット付きもそのまま）、
    # 値は CSV に書く見出し。SELECT に無い列は書いても無視される。
    # 置き換え後の見出しが 2 つ以上重なると ``SoqlColumnNameConflictError`` で
    # 失敗扱い（CSV は書かれない）。
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "商談名",
        "Amount": "金額",
        "CloseDate": "完了予定日",
    }

    def soql(self) -> str:
        # レポートの絞り込み条件を WHERE 句にした SOQL の例。
        # 実際のフィールド名は soql-collector の対応表の
        # 「対応フィールドAPI名」列を見て埋める。
        # 書けない SOQL と値の扱い（bulk の制約・CSV の文字列扱い）は
        # ``_template.py`` の ``soql()`` を参照。
        return (
            "SELECT Id, Name, Amount, CloseDate, StageName "
            "FROM Opportunity "
            "WHERE CloseDate >= 2024-01-01 AND StageName != 'Closed Won' "
            "ORDER BY CloseDate DESC"
        )

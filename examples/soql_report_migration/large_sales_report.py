"""examples/soql_report_migration/large_sales_report.py — SoqlReport の実装例。

Report API（2000行上限）の切り捨てに当たった商談明細レポートを、SOQL で取り直す例。
docs/salesforce-downloader.md「SOQLレポート（2000件超のレポートを移行する）」の
「書き方」に対応する。

**このファイルの中身はサンプル固有の部分（``OUTPUT_DIR``）を除けば本番そのまま。**
実際に使うときは ``src/soql_reports/reports/`` 配下へコピーし、``FOLDER`` を実際の
保存先へ書き換える。ファイル名は ``_`` で始めない（``_`` で始まるファイルは
``registered_reports()`` の走査対象外）。
置いただけで自動登録される（明示的なタプル編集は不要）。
"""

from pathlib import Path

from src.soql_reports.base import SoqlReport

# サンプル用に、このフォルダ内で完結させる（本番では会社の共有フォルダのパスにする）。
# ``download_soql_reports()`` は保存先フォルダが無いと ``ReportFolderNotFoundError`` に
# なる（``service._require_folder()`` と同じ設計で、書き間違いに気づけるよう勝手には
# 作らない）。このサンプルでは実行前に ``run.py`` 側で ``mkdir`` している。
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


class LargeSalesReport(SoqlReport):
    """商談明細（2000件超のため Report API では切り捨てられ、SOQL で取得する）。"""

    KEY = "9001"  # 社内で決める管理番号（Salesforce のレポートIDではない）
    SUMMARY = "商談明細（SOQL、2000件超）"
    URL = "https://example.my.salesforce.com"  # site_for() が組織を解決するために使う
    FOLDER = str(OUTPUT_DIR)
    ALLOW_EMPTY = False  # 0件を失敗として扱う（対象月に商談が無いのは想定外のため）

    def soql(self) -> str:
        # レポートの絞り込み条件を WHERE 句にした SOQL の例。
        # 実際のフィールド名は describe_fields() の
        # 「対応フィールドAPI名」列を見て埋める。
        return (
            "SELECT Id, Name, Amount, CloseDate, StageName "
            "FROM Opportunity "
            "WHERE CloseDate >= 2024-01-01 AND StageName != 'Closed Won' "
            "ORDER BY CloseDate DESC"
        )

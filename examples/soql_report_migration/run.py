"""examples/soql_report_migration/run.py — SOQLレポート移行の一連の流れを実演する。

2000件超で Report API の上限に当たったレポートを SOQL で取り直す流れを、
実際に動くコードで示す。手順の全体は
docs/salesforce-downloader.md「SOQLレポート（2000件超のレポートを移行する）」参照。

**外部の Salesforce 組織には接続しない。** ``site_for()`` をこのファイルの中だけで
差し替え、疑似的な明細データを返す最小限のスタブに置き換えて実行する
（``tests/test_soql_reports.py`` と同じ手法）。
**``_fake_site()`` とそれを差し替えている ``with patch(...)`` の節は、このサンプル
だけの都合。** 本番コードでは一切書かない — ``site_for()`` が ``LargeSalesReport.URL``
から実際の組織を自動解決する。

実行方法:
    リポジトリのルートで python -m examples.soql_report_migration.run

- 入力: 疑似データ（このファイルの中で組み立てる。外部システム・ネット接続は不要）
- 出力: このフォルダの output/9001_商談明細SOQL2000件超_日付_時刻.csv
"""

import logging
from unittest.mock import MagicMock, patch

from comken.core import Table
from comken.core.logger import setup_local_logging
from comken.toolbox.csv import CSV

from examples.soql_report_migration.large_sales_report import OUTPUT_DIR, LargeSalesReport
from src.soql_reports.runner import download_soql_reports

logger = logging.getLogger(__name__)

# 疑似の商談明細（LargeSalesReport.soql() を実際の組織で実行したとみなす）。
# このサンプルは SOQL の文字列を解釈しない（本物の組織なら Salesforce 側が解釈する）。
FAKE_ROWS = [
    {
        "Id": "0061",
        "Name": "案件A",
        "Amount": "1200000",
        "CloseDate": "2024-03-01",
        "StageName": "Negotiation",
    },
    {
        "Id": "0062",
        "Name": "案件B",
        "Amount": "800000",
        "CloseDate": "2024-04-15",
        "StageName": "Prospecting",
    },
]


def _fake_site() -> MagicMock:
    """疑似 Salesforce 組織。``with site() as sf: sf.query(soql)`` の形を保ったまま、
    実際の HTTP 通信をせずに固定の ``Table`` を返す。
    """
    table = Table(list(FAKE_ROWS[0]), FAKE_ROWS)
    client = MagicMock()
    client.__enter__.return_value.query.return_value = table
    client.__exit__.return_value = False
    return MagicMock(return_value=client)  # site_for() の戻り値（呼ぶと client を返す「組織」）


def main() -> None:
    # 保存先フォルダを用意する。download_soql_reports() は無いフォルダへは書かない
    # （書き間違いに気づけるよう勝手に作らない設計。docs/salesforce-downloader.md 参照）。
    OUTPUT_DIR.mkdir(exist_ok=True)

    # ↓↓↓ ここから疑似APIへの差し替え（本番では不要）↓↓↓
    with patch(
        "src.soql_reports.runner.site_for",
        return_value=_fake_site(),
    ):
        # reports に明示的にリストを渡すとテスト・デモ用に対象を絞れる。
        # 本番では reports/ に置くだけで registered_reports() が拾うので
        # 引数なしで呼べる
        saved_paths = download_soql_reports([LargeSalesReport])
    # ↑↑↑ ここまで ↑↑↑

    for path in saved_paths:
        logger.info("保存: %s", path)
        with CSV(path) as csv_file:
            table = csv_file.read()
        logger.info("取得件数: %d 件", len(table))
        for row in table:
            logger.info("  %s", row)


if __name__ == "__main__":
    # ログ設定は comken 側の標準ヘルパーを使う（examples/logger.py と同じ形）。
    # 生の logging.basicConfig() は使わない — ファイル出力・二重設定防止など、
    # setup_local_logging() が持つ既定の仕組みを外してしまうため。
    setup_local_logging(path=OUTPUT_DIR.parent / "logs")
    main()

"""examples/soql_report_migration/production_main.py — 本番用テンプレート。

``run.py`` は疑似APIでの動作確認用（外部システムなしで動く）。**このファイルは
モックを一切使わない、実際にSalesforceへ接続する本番コードそのもの。**
実プロジェクトへ移すときは ``main.py`` にリネームしてコピーする
（``run.py`` という名前についての注意は examples/README.md 参照）。

docs/salesforce-downloader.md「SOQLレポート（2000件超のレポートを移行する）」
の動作確認を、``run.py`` の疑似APIではなく本物の組織で行う版。

事前準備（このまま実行しても、以下が済んでいなければ失敗する。それが正しい挙動）:

1. ``LargeSalesReport.URL`` のドメインが ``comken/toolbox/salesforce/sites/`` の
   ``SITES`` に登録済みであること（未登録だと ``site_for()`` が
   ``SalesforceError`` を投げる）
2. その組織の ``api_client_id`` / ``api_client_secret`` / ``api_refresh_token`` が、
   実行するユーザー・PCで DPAPI に登録済みであること（項目名の ``api_`` は
   Salesforce 専用の認証情報だと分かるようにする接頭辞）。**登録は
   「サイト名: {項目名: 値}」の入れ子 JSON を一時的に用意し、取り込みコマンドで
   流し込む**（平文はその場で消える。comken の docs/機能/credentials.md「登録」参照）:

       {
         "solution": {
           "api_client_id": "...",
           "api_client_secret": "...",
           "api_refresh_token": "..."
         }
       }

       python -m comken cred import 認証情報.json

   ``"solution"`` の部分は ``LargeSalesReport.URL`` が解決する組織の
   ``CREDENTIAL_PREFIX``（``comken/toolbox/salesforce/sites/`` の各クラス定数）
   に合わせる。登録したユーザー・PC以外では復号できない。未登録だと
   ``CredentialNotFoundError`` になる（実際に何も登録していない環境でこのファイルを
   そのまま実行して確認済み）
3. ``large_sales_report.py`` の ``FOLDER`` を実際の保存先（共有フォルダ等）へ
   書き換えてあること（フォルダが無いと ``ReportFolderNotFoundError``。
   書き間違いに気づけるよう勝手には作らない設計）
4. ``soql()`` の中身が、対象組織で実際に通る SOQL であること

実プロジェクトへ組み込むときは、``large_sales_report.py`` を
``src/soql_reports/reports/`` 配下へコピーするだけで、ファイル名を ``_`` で
始めなければ ``registered_reports()`` が自動で拾う。``reports`` を明示せず
``download_soql_reports()`` を引数なしで呼べる（このテンプレートのように
呼び出し側でリストを渡す形のままでもよい）。

**いつ呼ぶか（スケジュール）はこのファイルに書かない。** 呼び出し側の
プロジェクトが決める（docs/salesforce-downloader.md「利用プロジェクト側の
設計判断」と同じ理由: 「今すぐ取りに行く」専用の仕組みを増やすと、定期取得が
止まっていることに誰も気づかなくなる）。

実行方法:
    python -m examples.soql_report_migration.production_main
"""

import logging

from comken.exceptions import ComkenError
from comken.run import backoffice

from examples.soql_report_migration.large_sales_report import LargeSalesReport
from src.soql_reports.runner import download_soql_reports

BATCH_NAME = "商談明細SOQL取得"

logger = logging.getLogger(__name__)


def main() -> None:
    # 自動で拾わせるなら download_soql_reports() を引数なしで呼べるが、
    # このサンプルは「対象のレポートを明示する形」を見せるために
    # 引数で渡している
    saved_paths = download_soql_reports([LargeSalesReport])
    for path in saved_paths:
        logger.info("保存: %s", path)


if __name__ == "__main__":
    try:
        # main を直接呼ばず社内 RPA 基盤に渡す。基盤が設定の初期化と時間計測をしてから呼ぶ
        backoffice(main, BATCH_NAME)
    except ComkenError as e:
        # comken のエラーはメッセージに対処法が入っている → ログを調査の起点にする
        logger.error("処理を中断しました: %s", e)
        raise
    except Exception:
        logger.exception("予期しないエラーが発生しました")
        raise

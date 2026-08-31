r"""examples/Salesforceダウンロード.py — オンデマンド取得 (`download_report()`) のサンプル。

`main.py` / `src/run.py` が毎日決まった時刻にまとめて取る「定期取得」なのに対し、
このサンプルは**自分のプロジェクトの処理の中で「今」取りたい**ときのために、
`download_report()` / `download_report_path()` を直接呼ぶ形を示す。

`main.py` のように `download_scheduled()` を呼ぶ構成はここに書かない。「定期」の
増減は comken の管理表で完結するため、このリポジトリには定期実行の入口しか残らない。
個別に取りたいプロジェクトは comken を import して、このサンプルのように直接呼ぶ。

**実行には実際の顧客表登録・Salesforce認証が必要なため、実際に実行されることは
想定していない。** 読んで理解するためのサンプル。
"""

import logging

from comken.services.salesforce_downloader import download_report, download_report_path

logger = logging.getLogger(__name__)

# 管理番号は comken の管理表（レポート管理表.xlsx）で決めた「社内の管理番号」。
# Salesforce のレポート ID ではない。コードに Salesforce の URL もレポート ID も
# 書かないため、識別子には必ず管理表で決めた番号（と、意味の分かる別名）を使う。
CUSTOMER_LIST = "1001"


def fetch_and_read() -> None:
    r"""`download_report()` で `Table` を受け取り、中身を直接読む例。

    取り込んだ値をこのプロジェクトの DB に書きたい、変換処理にかけたい等、
    ファイルパスを介さずに中身が欲しいときに使う。
    """
    # `download_report` は comken 側で PEP 562 の `__getattr__` 経由で遅延 import されるため、
    # pyright の静的解析からは callable に見えない。実行時は問題ない。
    table = download_report(CUSTOMER_LIST)  # type: ignore[reportCallIssue]
    rows = table.read_rows()
    logger.info("読み取った行数: %d", len(rows))


def fetch_path_only() -> None:
    r"""`download_report_path()` で保存した CSV のパスだけを受け取る例。

    ファイル本体は comken が Salesforce から取って保存まで済ませる。
    別ツール（Excel マクロ、Power BI、別システムの loader）にファイルパスだけ
    渡したいときに使う。
    """
    # `download_report_path` も同上（PEP 562 経由の遅延 import）。
    path = download_report_path(CUSTOMER_LIST)  # type: ignore[reportCallIssue]
    logger.info("保存先: %s", path)


if __name__ == "__main__":
    # デモ用。実際に動かすには comken の管理表に "1001" が登録されていて、
    # Salesforce の認証情報も整っている必要がある。読了確認が目的なので、
    # 2 つの関数を順に呼ぶだけで中身は出さない。
    fetch_and_read()
    fetch_path_only()

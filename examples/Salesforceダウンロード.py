r"""examples/Salesforceダウンロード.py — 定期取得済みキャッシュを読むサンプル。

`main.py` / `src/run.py` がタスクスケジューラから高頻度で `download_scheduled()`
を呼ぶ「定期取得」なのに対し、このサンプルは**別のプログラムから、
このバッチが既に取ってある本日のキャッシュだけを読みたい**ときのために、
`cached_report()` / `cached_report_path()` を直接呼ぶ形を示す。

定期取得は行わない（Salesforce への取りに行く処理は呼ばない）。
本日のキャッシュがまだ無い場合（定期取得がまだ走っていない等）は
`CachedReportNotFoundError` が出るので、呼び出し側で扱う必要がある。
例えば「今回は諦めて次回のポーリングで再試行する」「ログだけ出してスキップする」
等のポリシーを呼び出し側に委ねる想定。

`main.py` のように `download_scheduled()` を呼ぶ構成はここに書かない。「定期」の
増減は comken の管理表で完結するため、このリポジトリには定期実行の入口しか残らない。
定期取得済みキャッシュを読みたいだけのプロジェクトは comken を import して、
このサンプルのように直接呼ぶ。

**実行には実際の顧客表登録・Salesforce 認証・本日の定期取得済みキャッシュが
必要なため、実際に実行されることは想定していない。** 読んで理解するための
サンプル。
"""

import logging

from comken.exceptions import CachedReportNotFoundError
from comken.services.salesforce_downloader import cached_report, cached_report_path

logger = logging.getLogger(__name__)

# 管理番号は comken の管理表（レポート管理表.xlsx）で決めた「社内の管理番号」。
# Salesforce のレポート ID ではない。コードに Salesforce の URL もレポート ID も
# 書かないため、識別子には必ず管理表で決めた番号（と、意味の分かる別名）を使う。
CUSTOMER_LIST = "1001"


def read_cached_table() -> None:
    r"""`cached_report()` で `Table` を受け取り、中身を直接読む例。

    取り込んだ値をこのプロジェクトの DB に書きたい、変換処理にかけたい等、
    ファイルパスを介さずに中身が欲しいときに使う。
    """
    try:
        # `cached_report` は comken 側で PEP 562 の `__getattr__` 経由で遅延 import されるため、
        # pyright の静的解析からは callable に見えない。実行時は問題ない。
        table = cached_report(CUSTOMER_LIST)  # type: ignore[reportCallIssue]
    except CachedReportNotFoundError:
        # 本日分のキャッシュが無い（定期取得がまだ走っていない、等）。
        # 今回の試行は諦めて、次回のポーリング／呼び出しで再試行するのが
        # 呼び出し側に委ねる想定。必要なら logger.warning(...) でログだけ出す。
        logger.warning("本日のキャッシュがまだ無いため、今回はスキップします")
        return

    rows = table.read_rows()
    logger.info("読み取った行数: %d", len(rows))


def cached_path_only() -> None:
    r"""`cached_report_path()` で保存先の CSV のパスだけを受け取る例。

    ファイル本体は comken の定期取得が既に取ってある。中身はここでは読まない。
    別ツール（Excel マクロ、Power BI、別システムの loader）にファイルパスだけ
    渡したいときに使う。`cached_report()` と違い、
    `CachedReportNotFoundError` は送出されない（ファイルが存在しなくてもパスは返る）。
    """
    # `cached_report_path` も同上（PEP 562 経由の遅延 import）。
    path = cached_report_path(CUSTOMER_LIST)  # type: ignore[reportCallIssue]
    logger.info("本日のキャッシュ保存先: %s", path)


if __name__ == "__main__":
    # デモ用。実際に動かすには comken の管理表に "1001" が登録されていて、
    # 本日の定期取得が既に走っている必要がある。読了確認が目的なので、
    # 2 つの関数を順に呼ぶだけで中身は出さない。
    read_cached_table()
    cached_path_only()

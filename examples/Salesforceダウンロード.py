r"""examples/Salesforceダウンロード.py — 定期取得済みキャッシュを読むサンプル。

`main.py` / `src/run.py` が WinActor（社内RPA基盤）から高頻度で `download_scheduled()`
を呼ぶ「定期取得」なのに対し、このサンプルは**別のプログラムから、
このバッチが既に取ってある本日のキャッシュだけを読みたい**ときのために、
`cached_report()` / `cached_report_path()` を直接呼ぶ形を示す。

定期取得は行わない（Salesforce への取りに行く処理は呼ばない）。
本日のキャッシュがまだ無い場合（定期取得がまだ走っていない等）の扱いは
関数によって違う。詳細は各関数の docstring を参照。

`main.py` のように `download_scheduled()` を呼ぶ構成はここに書かない。「定期」の
増減は comken の管理表で完結するため、このリポジトリには定期実行の入口しか残らない。
定期取得済みキャッシュを読みたいだけのプロジェクトは comken を import して、
このサンプルのように直接呼ぶ。

保存形式は常に CSV（管理表に「出力形式」列は無い。選べる形式が CSV だけになったため
2026-09 に列ごと廃止した）。

**実行には実際の顧客表登録・Salesforce 認証・本日の定期取得済みキャッシュが
必要なため、実際に実行されることは想定していない。** 読んで理解するための
サンプル。
"""

import csv
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

    comken に依存できるプロジェクト（同じ `Table` の API を使い慣れている、
    取り込んだ値をこのプロジェクトの DB に書く・変換処理にかける等）向け。
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


def read_cached_file_directly() -> None:
    r"""`cached_report_path()` で得たパスを、素の `csv` 標準ライブラリで直接開く例。

    comken を import できない・comken の `Table` に依存したくない別プログラム
    （別リポジトリのスクリプト、社内の他ツール等）が、このバッチの定期取得済み
    CSV を読みたいときの形。実際にこのバッチの利用先で使われている形に近い。

    `cached_report_path()` は`cached_report()`と違い**取りに行かないし、
    ファイルの存在確認もしない**（パスを組み立てて返すだけ）。そのため
    ファイルが無いかどうかは呼び出し側で `Path.is_file()` を見て判断する。
    """
    # `cached_report_path` は comken 側で PEP 562 の `__getattr__` 経由で遅延 import
    # されるため、pyright の静的解析からは callable に見えない。実行時は問題ない。
    path = cached_report_path(CUSTOMER_LIST)  # type: ignore[reportCallIssue]
    if not path.is_file():
        logger.warning("本日のキャッシュがまだ無いため、今回はスキップします")
        return

    # 保存形式は常に CSV。comken を経由せず、素の csv モジュールで直接開ける。
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    logger.info("読み取った行数: %d（%s）", len(rows), path)


if __name__ == "__main__":
    # デモ用。実際に動かすには comken の管理表に "1001" が登録されていて、
    # 本日の定期取得が既に走っている必要がある。読了確認が目的なので、
    # 2 つの関数を順に呼ぶだけで中身は出さない。
    read_cached_table()
    read_cached_file_directly()

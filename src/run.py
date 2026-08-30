r"""src/run.py — 定期取得の対象になっているレポートを、まとめて落とす。

毎日決まった時刻に1回だけ動かす想定。**何を落とすかはここに書かない。** comken の
管理表（レポート管理表.xlsx）で「実行方式 = 定期」かつ「有効」になっているものが対象で、
増減は管理表を直すだけで済む（このコードは触らない）。

    レポート管理表.xlsx（非エンジニアが編集）
            ↓
    comken_salesforce_downloader.download_scheduled()
            ↓
    Salesforce ──→ 管理表に書かれた保存先

**1日に何度も最新が必要なプロジェクトは、このバッチを増やさない。** そのプロジェクト側から
`download_report()` を呼べば、その場で取りに行ける。ここに「土日祝を除く」のような
スケジュールを持たせないのも同じ理由で、**それは呼び出す側の予定にもともとある**。

失敗があれば例外で止まる（取得できたものは保存済み）。ログだけ出して正常終了すると、
スケジューラから見て成功と区別が付かない。
"""

import logging

from comken_salesforce_downloader import download_scheduled

logger = logging.getLogger(__name__)

# 履歴の「プロジェクト」列に残る名前。誰が取ったかを後から追えるようにする
PROJECT_NAME = "Salesforceレポートダウンローダー"


def run() -> None:
    """管理表で「定期」かつ有効なレポートをまとめて取得する。

    Raises:
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。
    """
    saved = download_scheduled(PROJECT_NAME)
    logger.info("%d 件を取得しました。", len(saved))

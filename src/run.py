r"""src/run.py — 定期取得の対象になっているレポートを、まとめて落とす。

WinActor（社内RPA基盤）等から**高頻度（例: 1 時間おき）で繰り返し呼ぶ**想定。
`download_scheduled()` は内部でスケジュール判定と重複防止をするため、
何度呼んでも安全（二重取得しない）。呼ぶ頻度を上げるほど「取るべき時刻」に
早く追従できる、というだけの違い。

**何を落とすかはここに書かない。** comken の管理表（レポート管理表.xlsx）で
「有効」になっているものが対象で、増減は管理表を直すだけで済む（このコードは
触らない）。**API・ブラウザ経由・SOQLのどれで取るかも管理表の列（「2000件超」
「SOQL」）で決まる**ため、ここでは意識しない。

    レポート管理表.xlsx（非エンジニアが編集）
            ↓
    src.salesforce_downloader.download_scheduled()
            ↓
    Salesforce ──→ 管理表に書かれた保存先

**1 日に何度も欲しいレポートは、comken 側の「スケジュール」シートにスケジュール
キーを分けて複数行登録すれば、このバッチの中で完結する。**

このバッチとは別に、今すぐの最新値（定期取得済みキャッシュ）だけを読みたい
下流プログラムは、このバッチを増やすのではなく
`comken.services.salesforce_downloader.cached_report()` /
`cached_report_path()` を直接呼べばよい
（→ `examples/Salesforceダウンロード.py` のサンプル参照）。

失敗があれば例外で止まる（取得できたものは保存済み）。ログだけ出して正常終了すると、
スケジューラから見て成功と区別が付かない。
"""

import logging

from src.salesforce_downloader import download_scheduled

logger = logging.getLogger(__name__)

# 履歴の「プロジェクト」列に残る名前。誰が取ったかを後から追えるようにする
PROJECT_NAME = "Salesforceレポートダウンローダー"


def run() -> None:
    """管理表で有効なレポートのうち、スケジュール上いま取るべきものをまとめて取得する。

    Raises:
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。
    """
    saved = download_scheduled(PROJECT_NAME)
    logger.info("%d 件を取得しました。", len(saved))

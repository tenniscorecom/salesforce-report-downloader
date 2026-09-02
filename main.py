"""
main.py — エントリポイント

このプロジェクトの入口。`python main.py` で実行できる（非エンジニアは 実行.bat をダブルクリック）。

処理の本体は src/ 以下に書き、ここでは「実行 → エラーの受け止め」だけを行う。
社内 RPA 基盤から動かす場合は、下の「社内 RPA 基盤から実行する場合」を参照。
"""

import logging

from comken import comken_logger
from comken.exceptions import ComkenError

from src.run import run

logger = logging.getLogger(__name__)


def main() -> None:
    # 設定の読み取りも処理も src/run.py に書く。ここは呼ぶだけにしておく
    run()


if __name__ == "__main__":
    # 単体で動かすので、ログの出力先をここで用意する（コンソールと logs/local-YYYY-MM-DD.log）。
    # 動作確認だけしたいときは保存・送信をスキップできる:
    #   from comken import dry_run
    #   with dry_run():
    #       main()
    comken_logger.setup_local_logging()
    try:
        main()
    except ComkenError as e:
        # comken のエラーはメッセージに対処法が入っている（docs/ERRORS.md も参照）
        logger.error("処理を中断しました: %s", e)
        raise
    except Exception:
        logger.exception("予期しないエラーが発生しました")
        raise

# ── 社内 RPA 基盤から実行する場合 ─────────────────────────────────────────────
# 上の `comken_logger.setup_local_logging()` と `main()` の2行を、次の形に差し替える。
# 基盤が設定の初期化・時間計測・ログ設定をしてから main を呼ぶので、
# comken_logger.setup_local_logging() は呼ばない（呼んでも二重設定にはならないが、
# 基盤の設定が正になる）。
#
#     from comken.toolbox.rpa import backoffice   # イントラネットのツールなら intranet に変える
#
#     PROJECT_NAME = "Salesforceレポートダウンローダー"   # 基盤へ渡す名前。ログの識別に使われる
#
#     if __name__ == "__main__":
#         try:
#             backoffice(main, PROJECT_NAME)
#         except ComkenError as e:
#             logger.error("処理を中断しました: %s", e)
#             raise

"""comken_salesforce_downloader/_paths.py — 管理表・履歴の置き場所。

`service.py`（取りに行く側）と `provider.py`（読み取る側）の両方が読む定数を、
依存関係を持ち込まない形で共有する。
`comken.toolbox.salesforce` を経由しないため、`requests` なしで import できる。

配置するときに実際の場所へ書き換える（公開リポジトリなので仮名にしてある）。
"""

from pathlib import Path

# レポート管理表（Excel）。非エンジニアが編集する。雛形は次のコマンドで作れる:
#     python -m comken_salesforce_downloader init レポート管理表.xlsx
# **config ファイルへは外出ししない。** 利用側がパスを渡せるようにすると、
# プロジェクト側に定数を持たせて管理表と食い違う事故が起きる（場所を変えるなら
# ここ1か所を変える）。設定ファイルに集約する案は試して戻した
# （仕様書 8章「配置時に書き換える3ファイル」）。
MASTER_PATH = Path(r"\\server\share\tools\salesforce\レポート管理表.xlsx")

# ダウンロード履歴（CSV）。プログラムが追記する（人は編集しない）
HISTORY_PATH = Path(r"\\server\share\tools\salesforce\ダウンロード履歴.csv")


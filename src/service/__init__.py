r"""src/service/ — 取得の本体（パッケージ）。

管理表・スケジュール・SOQL・取得の実行と「管理表の行から履歴に書く値を組み立てる
部分（``record()``）」を 1 つのリポジトリ（`src/`）に置いた形の本体。

    from src.service import download_scheduled

    CUSTOMER_LIST = "1001"        # 各プロジェクトで、意味の分かる名前を付ける

`download_scheduled()` は「**今この瞬間にまとめて取りに行く**」。管理表で
`有効` になっているレポートを全て対象に、定期実行のプロジェクトから呼ばれる。
戻り値は `list[Path]` で、定期取得の呼び出し側が中身を読まず「取らせる」
のが目的なので、`Table` を返さない（役割の違いが戻り値の型に出ている）。

**境界は履歴（ダウンロード履歴.csv）。** 履歴の形式・読み取り・ロック・置き場所は
``comken.services.salesforce_downloader.history`` / ``history_file_lock`` /
``paths`` 側に集約され、ダウンローダーは自分で持たない。``record()``
（``src.history``）だけがダウンローダーに残った「管理表の行から履歴に書く値を
組み立てる」部分で、実際の書き込みは comken の ``append_history()`` へ委譲する。

**急いでその場の最新値が必要なときは、`download_scheduled()` をスケジュール外に
直接実行する。** それ専用のAPIは用意していない。実行タイミングを分ける必要が
あれば、呼び出し側でスケジューラを増やす。Downloader 側に「今すぐ取りに行く」
だけの関数を残すと、定期取得が動いていないことに誰も気づかなくなるため。

プロジェクト側のコードに Salesforce の URL もレポート ID も現れない。管理表の
参照先を差し替えても、`CUSTOMER_LIST = "1001"` はそのままでよい。

このパッケージが持つもの:
- 1件を取得して保存し履歴に残す流れ（``attempt.py`` の ``download_scheduled`` と
  ``_download_scheduled_locked`` / ``_download`` / ``_Attempt``）
- Salesforce への問い合わせ経路の選択（``routes.py`` の ``_fetch`` / ``fetch_with_auto_fallback``）
- Report API / ブラウザ / SOQL の各取得（``api.py`` / ``browser.py`` / ``soql.py``）
- 保存とフォルダ検査（``save.py`` の ``save_report`` / ``_write_csv`` / ``require_folder``）
- スケジュールによる対象選定（``scheduler.py`` の ``select_targets`` / ``_matched_schedule_key``）

ここに書かないもの:
- 「このプロジェクトのときは」という分岐 → main.py / src/run.py
- 管理表にどんな列があるか → src.sheets.master
- 履歴の形式・読み取り関数 → comken.services.salesforce_downloader.history
- 履歴のロック → comken.services.salesforce_downloader.history_file_lock
- 履歴の置き場所（`HISTORY_PATH`）→ comken.services.salesforce_downloader.paths
- 履歴への値の組み立て（`record()`）→ src.history
- Salesforce の認証・API の叩き方 → comken/toolbox/salesforce/
- 取得済みファイルの取り出し（`output_path`）→ src.paths
- 管理表の置き場所（`MASTER_PATH`）→ src.paths
"""

from __future__ import annotations

# ``download_scheduled`` だけを公開する。 他の実装は同名モジュールから直接
# import する想定（テストなど、 ``src.service`` の ``__init__`` 配下だけで
# 完結する）。
from src.service.attempt import download_scheduled

__all__ = ["download_scheduled"]

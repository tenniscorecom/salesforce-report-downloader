"""src/salesforce_downloader/__init__.py — Salesforce レポートの定期取得（実行側）。

**2026-09 に comken から切り出した。** 管理表・履歴の形式は comken 側
（`comken.services.salesforce_downloader`）の共有契約のままで、ここには
「実際に Salesforce へ取りに行き、保存し、履歴へ書く」実行部分だけを置く。
経緯は comken の `salesforce_downloader/__init__.py` の履歴メモを参照。

    from src.salesforce_downloader import download_scheduled

    download_scheduled("定期実行")
"""

from src.salesforce_downloader.service import download_scheduled

__all__ = ["download_scheduled"]

"""src/ — Salesforce レポートの定期取得（実行側）。

管理表・履歴の形式（列定義・読み取り・書き込み）と取得実行部分
（`download_scheduled()` / `service.py`）を同じパッケージに置いている。

    from src.service import download_scheduled

    download_scheduled("定期実行")
"""

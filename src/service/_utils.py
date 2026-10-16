r"""src/service/_utils.py — 履歴パスを返すだけの内部ヘルパー。

``src/service/attempt.py`` から ``scheduler.py`` の依存を避けるため、
``comken.services.salesforce_downloader.paths.HISTORY_PATH`` を
``_history_path()`` でラップした関数を置く。

    from src.service._utils import history_path

履歴CSVの置き場は ``comken.services.salesforce_downloader.paths.HISTORY_PATH``
で、テストでは ``monkeypatch.setattr`` で comken 側のモジュール属性を
差し替える運用なので、呼び出し時点で ``history_paths.HISTORY_PATH`` を読む
（``from ... import HISTORY_PATH`` で名前を固定すると差し替えが効かない）。
"""

from __future__ import annotations

from pathlib import Path

from comken.services.salesforce_downloader import paths as history_paths


def history_path() -> Path:
    """呼び出し時点の履歴CSVのパスを ``Path`` で返す。"""
    return Path(history_paths.HISTORY_PATH)

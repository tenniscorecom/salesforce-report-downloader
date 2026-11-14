r"""src/service/_utils.py — 履歴パスを返すだけの内部ヘルパー。

履歴CSVのパスを読み取る箇所を 1 か所に寄せるため、
``comken.services.salesforce_downloader.paths.HISTORY_PATH`` を
``history_path()`` で返す関数を置く。

    from src.service._utils import history_path

``from ... import HISTORY_PATH`` で名前を固定すると差し替えが効かないので、
呼び出し時点で ``history_paths.HISTORY_PATH`` を読む（テストでは
``monkeypatch.setattr`` で comken 側のモジュール属性を差し替える運用）。
"""

from __future__ import annotations

from pathlib import Path

from comken.services.salesforce_downloader import paths as history_paths


def history_path() -> Path:
    """呼び出し時点の履歴CSVのパスを ``Path`` で返す。"""
    return Path(history_paths.HISTORY_PATH)

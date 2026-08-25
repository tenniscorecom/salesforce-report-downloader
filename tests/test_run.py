"""tests/test_run.py — main.py / src.run の薄いテスト。

何を確認するかを README と同じ薄さに保つ:
  - ``main`` と ``src.run`` を import するだけでエラーが出ないこと（import 段階の改名・型ミス）
  - ``main.py`` の ``if __name__ == "__main__":`` ブロックが
    ``comken_logger.setup_local_logging()`` と ``download_scheduled()`` を
    **実際に呼ぶ**こと（呼び出し時点の改名ミスは import では検出できないので、
    ``runpy`` で main.py を __main__ として実行して確かめる）
  - ``src.run.run()`` が ``download_scheduled(PROJECT_NAME)`` を呼ぶこと

網羅的なテストスイートにはしない。``csv-excel-transfer`` と ``延期積上集計`` の
テスト置き場に合わせ、``tests/__init__.py`` は作らない。
"""

from __future__ import annotations

import importlib
import logging
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock

import comken.core.logger
import comken.services.salesforce_downloader.service as _service
from _pytest.logging import LogCaptureFixture
from _pytest.monkeypatch import MonkeyPatch

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ensure_path() -> None:
    """プロジェクトルートを sys.path に足す（main.py と src/ を import するため）。"""
    path = str(PROJECT_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)


def _reload(*names: str) -> None:
    """``monkeypatch`` の前に該当モジュールを捨てて、再 import でパッチを効かせる。"""
    for name in names:
        sys.modules.pop(name, None)


def test_main_and_src_run_import_without_error() -> None:
    """``main`` と ``src.run`` を import するだけでエラーが出ないこと。

    import 段階での改名・型ミスを検出する。``comken_logger.setup_local_logging()``
    のように呼び出しで落ちる改名ミスは import では出ないので、次の test で別途扱う。
    """
    _ensure_path()
    _reload("main", "src", "src.run")

    assert importlib.import_module("main") is not None
    assert importlib.import_module("src.run") is not None


def test_main_block_calls_setup_local_logging_and_download_scheduled(
    monkeypatch: MonkeyPatch,
) -> None:
    """``if __name__ == "__main__":`` ブロックが ``comken_logger.setup_local_logging()`` と
    ``download_scheduled()`` を実際に呼ぶこと。

    ``comken_logger.local()`` → ``setup_local_logging()`` のような呼び出し時点の
    改名ミスは ``TypeError: 'module' object is not callable`` で発覚する。
    import だけではここを通らないので、``runpy`` で ``main.py`` を __main__ として
    実行し、両関数がモックに到達することを確かめる。
    """
    _ensure_path()
    # ``comken.services.salesforce_downloader`` の __init__ は ``download_scheduled`` を
    # ``__getattr__`` で遅延 import し、初回アクセス時に ``__init__.__dict__`` にキャッシュする。
    # テスト 1 で ``import src.run`` が走るとこのキャッシュが残り、ここでの monkeypatch では
    # 古い wrapper が取り出されてしまう。__init__ も捨てて再 import で fresh にする。
    _reload(
        "main",
        "src",
        "src.run",
        "comken.services.salesforce_downloader",
    )

    fake_setup = MagicMock()
    fake_download = MagicMock(return_value=["a.xlsx", "b.xlsx"])

    # main.py は ``comken_logger.setup_local_logging()`` を呼ぶ。
    # ``comken_logger`` は ``comken.core.logger`` のエイリアスなので、
    # そちら側の ``setup_local_logging`` を差し替える。
    monkeypatch.setattr(comken.core.logger, "setup_local_logging", fake_setup)

    # ``download_scheduled`` は ``__getattr__`` で遅延 import される。
    # 遅延 import 先（service モジュール）の関数を直接差し替える。
    monkeypatch.setattr(_service, "download_scheduled", fake_download)

    # root logger にテスト由来の handler が残らないよう、実行後の差分を掃除する。
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    try:
        runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name="__main__")
    finally:
        for handler in list(root_logger.handlers):
            if handler not in handlers_before:
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

    fake_setup.assert_called_once()
    fake_download.assert_called_once()


def test_run_calls_download_scheduled_with_project_name(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    """``src.run.run()`` が ``download_scheduled(PROJECT_NAME)`` を呼び、
    件数ログが出ること。
    """
    _ensure_path()
    _reload("src", "src.run", "comken.services.salesforce_downloader")

    fake_download = MagicMock(return_value=["a.xlsx", "b.xlsx"])
    monkeypatch.setattr(_service, "download_scheduled", fake_download)

    from src.run import PROJECT_NAME, run

    with caplog.at_level(logging.INFO, logger="src.run"):
        run()

    fake_download.assert_called_once_with(PROJECT_NAME)
    assert PROJECT_NAME == "Salesforceレポートダウンローダー"
    assert any("2 件を取得しました" in record.getMessage() for record in caplog.records)
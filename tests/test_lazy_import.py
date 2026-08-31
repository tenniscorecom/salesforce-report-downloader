"""comken_salesforce_downloader の `__init__.py` が lazy import することを保証する。

BO 環境で `requests` が無いときでも、provider.py の関数だけ動かせる形が
設計の本意。`__init__.py` が `service` を import すると requests が読まれてしまうので、
`__getattr__` で lazy に振り分ける。

このテストは「`__init__.py` 経由の import で service.py が import されないこと」を
sys.modules で確認する。`download_report` / `download_scheduled` のような
service.py 側の関数を import したときは、service.py が読み込まれて良い。
"""

import sys

import pytest

# ─────────────────────────────────────────────────────────────────────
# service を import しないことが正しい関数 (provider / master 側)
# ─────────────────────────────────────────────────────────────────────


def _drop_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.modules から service サブモジュールを取り除く。"""
    for mod_name in list(sys.modules):
        if mod_name == "comken_salesforce_downloader.service":
            monkeypatch.delitem(sys.modules, mod_name, raising=False)


def test_cached_report_does_not_load_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cached_report` を import しても `service` は読み込まれない。"""
    _drop_service(monkeypatch)
    from comken_salesforce_downloader import cached_report  # noqa: F401

    assert "comken_salesforce_downloader.service" not in sys.modules, (
        "cached_report の import で service まで読み込まれている。"
        "__init__.py の __getattr__ が service を引いている可能性"
    )


def test_file_path_of_does_not_load_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`file_path_of` も同様。"""
    _drop_service(monkeypatch)
    from comken_salesforce_downloader import file_path_of  # noqa: F401

    assert "comken_salesforce_downloader.service" not in sys.modules


def test_load_master_does_not_load_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_master` は master 側に常駐する関数。service は要らない。"""
    _drop_service(monkeypatch)
    from comken_salesforce_downloader import load_master  # noqa: F401

    assert "comken_salesforce_downloader.service" not in sys.modules


def test_report_entry_does_not_load_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ReportEntry` も master 側に常駐。"""
    _drop_service(monkeypatch)
    from comken_salesforce_downloader import ReportEntry  # noqa: F401

    assert "comken_salesforce_downloader.service" not in sys.modules


def test_shared_report_ids_does_not_load_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """`shared_report_ids` も master 側。"""
    _drop_service(monkeypatch)
    from comken_salesforce_downloader import shared_report_ids  # noqa: F401

    assert "comken_salesforce_downloader.service" not in sys.modules


# ─────────────────────────────────────────────────────────────────────
# service を import して良い関数 (service 側)
# ─────────────────────────────────────────────────────────────────────


def test_download_report_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`download_report` を import しても AttributeError 等で落ちない。

    service.py を読むので `requests` が sys.modules に入る前提だが、
    ここでは「import 経路が壊れていない」ことだけを確認する。
    """
    from comken_salesforce_downloader import download_report

    assert download_report is not None


def test_download_scheduled_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`download_scheduled` も同様。"""
    from comken_salesforce_downloader import download_scheduled

    assert download_scheduled is not None


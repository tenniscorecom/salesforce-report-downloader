"""tests/test_conftest_isolation.py — ``tests/conftest.py`` の autouse fixture が
本番パスを ``tmp_path`` 配下に倒していることの確認。

``tests/conftest.py`` の ``_isolate_production_paths`` fixture は
``autouse=True`` で全テストに適用され、 本番の固定パスを指す
モジュール変数を ``tmp_path`` 配下に差し替える。 このファイルは
「**差し替えが効いている**」ことと「**差し替えを解除すると本番パスが
見える**」ことの両方を回帰テストで押さえる。

差し替える変数（``comken.services.salesforce_downloader.paths`` 側 /
``src.paths`` 側の、本番の固定パスを指すモジュール変数）:

- ``src.paths.MASTER_PATH``
- ``src.paths.NOTIFICATION_FOLDER``
- ``comken.services.salesforce_downloader.paths.HISTORY_DB_PATH``
- ``comken.services.salesforce_downloader.paths.HISTORY_PATH``

派生元の ``SALESFORCE_DOWNLOADER_FOLDER`` （sfdl 側 / comken 側の両方）は
モジュール読み込み時に ``MASTER_PATH`` / ``HISTORY_*_PATH`` に展開済み
なので、 派生先だけ差し替えれば十分。 派生元の値も本番の固定パスである
ことを ``TestAutouseCanBeOptedOut`` のテストで一緒に確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from comken.services.salesforce_downloader import paths as history_paths_module

import src.paths as sfdl_paths_module

# autouse fixture が ``_isolate_production_paths`` であることを前提に、
# 「fixture が外れた場合だけ」このマーカーで opt-out する。
_TEST_NO_ISOLATE = pytest.mark.no_isolate_production_paths


class TestAutouseProductionPathIsolation:
    """autouse fixture が本番パスを ``tmp_path`` 配下に倒していることの確認。

    ``tmp_path`` 自体は autouse fixture の引数で受け取っているわけでは
    ない（個別のテストが pytest 組み込み ``tmp_path`` を要求している）。
    各テストの ``tmp_path`` と autouse fixture が当てたパスが一致する
    ことは、 ``monkeypatch.setattr(_sfdl_paths, "MASTER_PATH", ...)``
    が autouse fixture 内で動いている証拠になる。
    """

    def test_master_path_is_under_tmp_path(self, tmp_path: Path) -> None:
        """``src.paths.MASTER_PATH`` が ``tmp_path`` 配下を指している。"""
        assert str(sfdl_paths_module.MASTER_PATH).startswith(str(tmp_path)), (
            f"MASTER_PATH={sfdl_paths_module.MASTER_PATH!r} is not under tmp_path={tmp_path!r}"
        )

    def test_notification_folder_is_under_tmp_path(self, tmp_path: Path) -> None:
        """``src.paths.NOTIFICATION_FOLDER`` が ``tmp_path`` 配下を指している。"""
        assert str(sfdl_paths_module.NOTIFICATION_FOLDER).startswith(str(tmp_path)), (
            f"NOTIFICATION_FOLDER={sfdl_paths_module.NOTIFICATION_FOLDER!r}"
            f" is not under tmp_path={tmp_path!r}"
        )

    def test_history_db_path_is_under_tmp_path(self, tmp_path: Path) -> None:
        """``comken.services.salesforce_downloader.paths.HISTORY_DB_PATH``
        が ``tmp_path`` 配下を指している。"""
        assert str(history_paths_module.HISTORY_DB_PATH).startswith(str(tmp_path)), (
            f"HISTORY_DB_PATH={history_paths_module.HISTORY_DB_PATH!r}"
            f" is not under tmp_path={tmp_path!r}"
        )

    def test_history_path_is_under_tmp_path(self, tmp_path: Path) -> None:
        """``comken.services.salesforce_downloader.paths.HISTORY_PATH``
        が ``tmp_path`` 配下を指している。"""
        assert str(history_paths_module.HISTORY_PATH).startswith(str(tmp_path)), (
            f"HISTORY_PATH={history_paths_module.HISTORY_PATH!r} is not under tmp_path={tmp_path!r}"
        )


# ─────────────────────────────────────────────────────────────────────────
# 「fixture を外すと本番パスが見える」検証
# ─────────────────────────────────────────────────────────────────────────
# ここでの「本番パス」は autouse fixture が当てていない **既定値**。
# autouse が壊れていれば opt-out しても ``MASTER_PATH`` などは ``tmp_path``
# 配下のまま、 =テストが落ちる（=壊れ検知）。
class TestAutouseCanBeOptedOut:
    """``@pytest.mark.no_isolate_production_paths`` で autouse fixture を
    外すと、 モジュール変数の **既定値** が本番パスを指すことを確認する。

    autouse が壊れていれば opt-out しても ``MASTER_PATH`` は
    ``tmp_path`` 配下のままになり、 このテスト群が落ちる（=回帰検知）。
    """

    @_TEST_NO_ISOLATE
    def test_opt_out_recovers_production_master_path(self, tmp_path: Path) -> None:
        """``no_isolate_production_paths`` を付ければ ``MASTER_PATH`` は
        本番の UNC 共有を指す。

        autouse が壊れて **常に tmp_path に倒し続ける** と、 opt-out を
        付けても ``MASTER_PATH`` が ``tmp_path`` 配下のままになり、
        このテストが落ちる（=回帰検知）。
        """
        assert not str(sfdl_paths_module.MASTER_PATH).startswith(str(tmp_path)), (
            f"opt-out したのに MASTER_PATH が tmp_path 配下を指している "
            f"（autouse fixture が壊れている）: {sfdl_paths_module.MASTER_PATH!r}"
        )
        # 派生元の SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME から
        # 組み立てた「既定値の MASTER_PATH」と一致する
        expected = (
            sfdl_paths_module.SALESFORCE_DOWNLOADER_FOLDER / sfdl_paths_module.MASTER_FILENAME
        )
        assert sfdl_paths_module.MASTER_PATH == expected, (
            f"既定値の MASTER_PATH が組み立て式と一致しない: "
            f"actual={sfdl_paths_module.MASTER_PATH!r} expected={expected!r}"
        )
        # sfdl 側の派生元 SALESFORCE_DOWNLOADER_FOLDER は UNC 共有
        sfdl_folder = str(sfdl_paths_module.SALESFORCE_DOWNLOADER_FOLDER)
        assert sfdl_folder.startswith("\\\\") or sfdl_folder.startswith("//"), (
            f"sfdl 側の SALESFORCE_DOWNLOADER_FOLDER が UNC ではない: {sfdl_folder!r}"
        )

    @_TEST_NO_ISOLATE
    def test_opt_out_recovers_production_history_paths(self, tmp_path: Path) -> None:
        """``no_isolate_production_paths`` を付ければ ``HISTORY_DB_PATH`` /
        ``HISTORY_PATH`` は ``C:\\SalesforceDownloader`` 配下を指す。

        autouse が壊れて **常に tmp_path に倒し続ける** と、 opt-out を
        付けても ``HISTORY_DB_PATH`` が ``tmp_path`` 配下のままになり、
        このテストが落ちる（=回帰検知）。
        """
        assert not str(history_paths_module.HISTORY_DB_PATH).startswith(str(tmp_path)), (
            f"opt-out したのに HISTORY_DB_PATH が tmp_path 配下を指している "
            f"（autouse fixture が壊れている）: {history_paths_module.HISTORY_DB_PATH!r}"
        )
        assert history_paths_module.HISTORY_DB_PATH == (
            history_paths_module.SALESFORCE_DOWNLOADER_FOLDER
            / history_paths_module.HISTORY_DB_FILENAME
        )
        assert history_paths_module.HISTORY_PATH == (
            history_paths_module.SALESFORCE_DOWNLOADER_FOLDER
            / history_paths_module.HISTORY_CSV_FILENAME
        )
        # comken 側の派生元 SALESFORCE_DOWNLOADER_FOLDER は ``C:\\SalesforceDownloader``
        # （v3 の仮置き）
        assert str(history_paths_module.SALESFORCE_DOWNLOADER_FOLDER) == (
            r"C:\SalesforceDownloader"
        ), (
            f"comken 側の SALESFORCE_DOWNLOADER_FOLDER が想定の"
            f" C:\\SalesforceDownloader ではない: "
            f"{history_paths_module.SALESFORCE_DOWNLOADER_FOLDER!r}"
        )

    @_TEST_NO_ISOLATE
    def test_opt_out_recovers_production_notification_folder(self, tmp_path: Path) -> None:
        """``no_isolate_production_paths`` を付ければ ``NOTIFICATION_FOLDER``
        は ``Path.home()`` 配下（ ``~/Box/Salesforceレポートダウンローダー通知``）
        を指す。

        autouse が壊れて **常に tmp_path に倒し続ける** と、 opt-out を
        付けても ``NOTIFICATION_FOLDER`` が ``tmp_path`` 配下のままになり、
        このテストが落ちる（=回帰検知）。
        """
        # ``NOTIFICATION_FOLDER`` は ``tmp_path`` 配下を指していない（=本番パス）
        assert not str(sfdl_paths_module.NOTIFICATION_FOLDER).startswith(str(tmp_path)), (
            f"opt-out したのに NOTIFICATION_FOLDER が tmp_path 配下を指している "
            f"（autouse fixture が壊れている）: "
            f"{sfdl_paths_module.NOTIFICATION_FOLDER!r}"
        )
        # 派生式 ``Path.home() / "Box" / "Salesforceレポートダウンローダー通知"``
        # と一致する
        expected = Path.home() / "Box" / "Salesforceレポートダウンローダー通知"
        assert sfdl_paths_module.NOTIFICATION_FOLDER == expected, (
            f"既定値の NOTIFICATION_FOLDER が組み立て式と一致しない: "
            f"actual={sfdl_paths_module.NOTIFICATION_FOLDER!r} expected={expected!r}"
        )

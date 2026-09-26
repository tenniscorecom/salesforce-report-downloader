"""SOQL レポート取得の基盤（``download_soql_reports()``）を、Salesforce をモックして検証する。

実際のレポート（サブクラス）は作らず、テスト内でダミーの ``SoqlReport`` サブクラスを
定義して使う。``SalesforceBase.query()`` をモックして Table を返す。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.exceptions import (
    SalesforceError,
)
from comken.toolbox.csv import CSV

from src.exceptions import (
    DownloaderError,
    SoqlReportNotRegisteredError,
)
from src.soql_reports import _registry, soql_report_for
from src.soql_reports import runner as runner_module
from src.soql_reports.base import SoqlReport
from src.soql_reports.runner import download_soql_reports

URL = "https://example.my.salesforce.com"
ROWS = [{"Id": "001", "Name": "山田"}, {"Id": "002", "Name": "鈴木"}]


class _DummyReport(SoqlReport):
    """``SoqlReport`` のテスト用ダミー実装。"""

    KEY = "9001"
    SUMMARY = "売上明細（テスト用）"
    URL = URL
    FOLDER = ""  # テストで上書きする

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _AllowEmptyReport(SoqlReport):
    """``ALLOW_EMPTY=True`` のテスト用ダミー実装。"""

    KEY = "9002"
    SUMMARY = "空を許すレポート（テスト用）"
    URL = URL
    FOLDER = ""
    ALLOW_EMPTY = True

    def soql(self) -> str:
        return "SELECT Id FROM Account"


class _FailingReport(SoqlReport):
    """``SalesforceRequestError``（``ComkenError`` 派生）を投げるテスト用ダミー。"""

    KEY = "9003"
    SUMMARY = "失敗するレポート（テスト用）"
    URL = URL
    FOLDER = ""

    def soql(self) -> str:
        return "SELECT Id FROM Bogus"


class _EmptyReport(SoqlReport):
    """``ALLOW_EMPTY=False`` で 0 件を返すテスト用ダミー。"""

    KEY = "9004"
    SUMMARY = "空レポート（テスト用）"
    URL = URL
    FOLDER = ""

    def soql(self) -> str:
        return "SELECT Id FROM Account WHERE Id = NULL"


def fake_salesforce(rows: list[dict] | None = None) -> MagicMock:
    """``query()`` が ``Table`` を返す Salesforce クライアント。"""
    table = Table(["Id", "Name"], rows if rows is not None else ROWS)
    client = MagicMock()
    client.__enter__.return_value.query.return_value = table
    site = MagicMock(return_value=client)
    return site


def fake_empty_salesforce() -> MagicMock:
    """``query()`` が 0 行の ``Table`` を返す Salesforce クライアント。"""
    client = MagicMock()
    client.__enter__.return_value.query.return_value = Table(["Id", "Name"], [])
    site = MagicMock(return_value=client)
    return site


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """保存先フォルダ（tmp_path 直下に1つ用意する）。"""
    target = tmp_path / "reports"
    target.mkdir()
    return target


@pytest.fixture
def isolated_reports_dir(tmp_path: Path, monkeypatch):
    """``reports/`` の ``__path__`` を ``tmp_path`` に差し替えるフィクスチャ。

    ``registered_reports()`` は ``pkgutil.iter_modules(reports.__path__)`` で
    走査するので、``reports`` モジュールの ``__path__`` を一時ディレクトリに
    差し替えれば、その中へ ``.py`` を書くだけでテスト用レポートを追加できる。
    monkeypatch が終了時に ``__path__`` と ``sys.modules`` を元に戻す。
    """
    from src.soql_reports import reports as reports_module

    monkeypatch.setattr(reports_module, "__path__", [str(tmp_path)])
    yield tmp_path


def _load_module(reports_module, stem: str) -> ModuleType:
    """``reports/__path__`` 内の ``<stem>.py`` を ``importlib.import_module`` で読み込む。

    既に ``sys.modules`` にあれば削除してから読み直す（テスト間のキー衝突を避ける）。
    """
    full_name = f"{reports_module.__name__}.{stem}"
    if full_name in sys.modules:
        del sys.modules[full_name]
    return importlib.import_module(full_name)


class TestSoqlReportBase:
    """``SoqlReport`` 基底クラスの挙動確認。"""

    def test_soql_raises_not_implemented(self):
        """``soql()`` はサブクラスで実装する前提。基底のままだと ``NotImplementedError``。"""
        with pytest.raises(NotImplementedError):
            SoqlReport().soql()

    def test_default_attributes(self):
        """``KEY`` / ``SUMMARY`` / ``URL`` / ``FOLDER`` の既定値と ``ALLOW_EMPTY`` の既定値。"""
        assert SoqlReport.KEY == ""
        assert SoqlReport.SUMMARY == ""
        assert SoqlReport.URL == ""
        assert SoqlReport.FOLDER == ""
        assert SoqlReport.ALLOW_EMPTY is False


class TestDownloadSoqlReports:
    """``download_soql_reports()`` のメインシナリオ。"""

    def test_saves_csv_in_folder(self, folder):
        """取得した Table が指定フォルダへ CSV として保存される。"""
        _DummyReport.FOLDER = str(folder)
        with patch.object(runner_module, "site_for", return_value=fake_salesforce()):
            saved = download_soql_reports([_DummyReport])
        assert len(saved) == 1
        csv_path = saved[0]
        assert csv_path.is_file()
        assert csv_path.parent == folder
        with CSV(csv_path, read_only=True) as csv_file:
            assert csv_file.read().to_rows() == ROWS

    def test_uses_default_registered_reports_when_argument_is_none(self, folder):
        """``reports=None`` のときは ``registered_reports()`` が使われる。"""
        _DummyReport.FOLDER = str(folder)
        with (
            patch.object(_registry, "registered_reports", return_value=(_DummyReport,)),
            patch.object(runner_module, "site_for", return_value=fake_salesforce()),
        ):
            saved = download_soql_reports()
        assert len(saved) == 1

    def test_empty_registered_reports_returns_empty_list_when_default(self):
        """``registered_reports()`` が空のときは何もせず空リストを返す。"""
        with patch.object(_registry, "registered_reports", return_value=()):
            saved = download_soql_reports()
        assert saved == []

    def test_continues_after_one_failure(self, folder):
        """1件失敗しても他のレポートの取得は止めない。"""

        def query_side_effect(soql: str) -> Table:
            # ``9003`` だけ例外を投げる（SOQL 文字列で識別）。
            if "FROM Bogus" in soql:
                raise SalesforceError(f"この URL の組織が登録されていません: {URL}")
            table = Table(["Id", "Name"], ROWS)
            return table

        _DummyReport.FOLDER = str(folder)
        _FailingReport.FOLDER = str(folder)
        client = MagicMock()
        client.__enter__.return_value.query.side_effect = query_side_effect
        site = MagicMock(return_value=client)
        with (
            pytest.raises(DownloaderError) as caught,
            patch.object(runner_module, "site_for", return_value=site),
        ):
            download_soql_reports([_DummyReport, _FailingReport])
        # 失敗したのは ``_FailingReport``（KEY="9003"）だけ
        assert "9003" in str(caught.value)
        assert isinstance(caught.value.__cause__, SalesforceError)

    def test_raises_soql_download_failed_when_all_fail(self, folder):
        """全件失敗のときも ``DownloaderError`` が送出される。"""
        _DummyReport.FOLDER = str(folder)

        def query_side_effect(_soql: str) -> Table:
            raise SalesforceError(f"この URL の組織が登録されていません: {URL}")

        client = MagicMock()
        client.__enter__.return_value.query.side_effect = query_side_effect
        site = MagicMock(return_value=client)
        with (
            pytest.raises(DownloaderError) as caught,
            patch.object(runner_module, "site_for", return_value=site),
        ):
            download_soql_reports([_DummyReport])
        assert "9001" in str(caught.value)

    def test_propagates_unexpected_exception(self, folder):
        """想定外の例外（``TypeError`` 等）はそのまま伝播する。"""
        _DummyReport.FOLDER = str(folder)

        def query_side_effect(_soql: str) -> Table:
            raise TypeError("プログラムバグ")

        client = MagicMock()
        client.__enter__.return_value.query.side_effect = query_side_effect
        site = MagicMock(return_value=client)
        with (
            pytest.raises(TypeError),
            patch.object(runner_module, "site_for", return_value=site),
        ):
            download_soql_reports([_DummyReport])


class TestSaveSemantics:
    """``_save()`` の ``ALLOW_EMPTY`` 制御とフォルダ存在チェック。"""

    def test_empty_rows_with_allow_empty_false_raises(self, folder):
        """0 行・``ALLOW_EMPTY=False`` は ``DownloaderError`` に変換される。"""
        _EmptyReport.FOLDER = str(folder)
        with (
            patch.object(runner_module, "site_for", return_value=fake_empty_salesforce()),
            pytest.raises(DownloaderError) as caught,
        ):
            download_soql_reports([_EmptyReport])
        assert "9004" in str(caught.value)

    def test_empty_rows_with_allow_empty_true_saves_empty_csv(self, folder):
        """0 行・``ALLOW_EMPTY=True`` は空 CSV を保存する（失敗扱いしない）。"""
        _AllowEmptyReport.FOLDER = str(folder)
        with patch.object(runner_module, "site_for", return_value=fake_empty_salesforce()):
            saved = download_soql_reports([_AllowEmptyReport])
        assert len(saved) == 1
        csv_path = saved[0]
        assert csv_path.is_file()
        with CSV(csv_path, read_only=True) as csv_file:
            table = csv_file.read()
        assert table.to_rows() == []
        # 0 行でも列は見出しとして残る（``SalesforceBase.query()`` が ``columns`` を返すため）
        assert table.columns == ["Id", "Name"]

    def test_missing_folder_raises(self, tmp_path):
        """保存先フォルダが無ければ ``DownloaderError`` に変換される。"""
        missing = tmp_path / "存在しないフォルダ"
        _DummyReport.FOLDER = str(missing)
        with (
            pytest.raises(DownloaderError) as caught,
            patch.object(runner_module, "site_for", return_value=fake_salesforce()),
        ):
            download_soql_reports([_DummyReport])
        assert "9001" in str(caught.value)


class TestReservePath:
    """``_reserve_path()`` の連番制御（``service._reserve_path()`` と同じアルゴリズム）。"""

    def test_existing_file_does_not_get_overwritten(self, folder, monkeypatch):
        """既存ファイルと衝突したら連番（``_1`` / ``_2`` …）で別名を確保する。"""
        _DummyReport.FOLDER = str(folder)
        base_name = f"{_DummyReport.KEY}_売上明細（テスト用）_fixed.csv"
        collision = folder / base_name
        collision.write_text("既存", encoding="utf-8")
        monkeypatch.setattr(runner_module, "_file_path_of", lambda *_args, **_kwargs: collision)
        with patch.object(runner_module, "site_for", return_value=fake_salesforce()):
            saved = download_soql_reports([_DummyReport])
        # 既存ファイルは上書きされていない
        assert collision.read_text(encoding="utf-8") == "既存"
        # 別ファイルが連番付きで保存された
        assert saved[0].name == base_name.replace(".csv", "_1.csv")
        assert saved[0].is_file()

    def test_limit_exceeded_raises(self, folder, monkeypatch):
        """連番の上限に達したら ``DownloaderError`` に変換する。"""
        monkeypatch.setattr(runner_module, "RESERVE_PATH_LIMIT", 5)
        _DummyReport.FOLDER = str(folder)
        base_name = f"{_DummyReport.KEY}_売上明細（テスト用）_limit.csv"
        base = folder / base_name
        monkeypatch.setattr(runner_module, "_file_path_of", lambda *_args, **_kwargs: base)
        for sequence in range(5):
            candidate = (
                base if sequence == 0 else folder / base_name.replace(".csv", f"_{sequence}.csv")
            )
            candidate.write_text("埋まり", encoding="utf-8")
        with (
            pytest.raises(DownloaderError) as caught,
            patch.object(runner_module, "site_for", return_value=fake_salesforce()),
        ):
            download_soql_reports([_DummyReport])
        assert "9001" in str(caught.value)


class TestRegisteredReports:
    """``registered_reports()`` の自動登録挙動（``reports/`` を ``tmp_path`` に差し替えて検証）。"""

    def test_returns_empty_tuple_when_no_reports(self, isolated_reports_dir):
        """``reports/`` に実レポートが無い状態では空タプル。"""
        from src.soql_reports import _registry as registry_module

        # フィクスチャで ``reports/__path__`` は ``isolated_reports_dir`` に差し替え済み。
        # このテストでは何も置かないので何も見つからない
        assert registry_module.registered_reports() == ()

    def test_picks_up_real_module_under_reports(self, isolated_reports_dir):
        """``reports/`` に置いたファイルのクラスが拾われる。"""
        from src.soql_reports import _registry as registry_module
        from src.soql_reports import reports as reports_module

        # 一意な KEY にして他のテストとぶつけない
        body = (
            "from src.soql_reports.base import SoqlReport\n"
            "class RealPickReport(SoqlReport):\n"
            "    KEY = '8001'\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        (isolated_reports_dir / "real_pick.py").write_text(body, encoding="utf-8")
        _load_module(reports_module, "real_pick")

        classes = registry_module.registered_reports()
        assert len(classes) == 1
        assert classes[0].__name__ == "RealPickReport"
        assert classes[0].KEY == "8001"

    def test_skips_underscored_module(self, isolated_reports_dir):
        """``_`` で始まるモジュール（``_template.py`` 等）は走査対象外。"""
        from src.soql_reports import _registry as registry_module

        body = (
            "from src.soql_reports.base import SoqlReport\n"
            "class SkipMe(SoqlReport):\n"
            "    KEY = '8002'\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        (isolated_reports_dir / "_skipped.py").write_text(body, encoding="utf-8")
        # 走査対象外なので import すらされない（sys.modules に登録されないことを確認するため
        # ここで ``importlib.import_module`` は呼ばない）

        assert registry_module.registered_reports() == ()

    def test_does_not_pick_up_imported_only_class(self, isolated_reports_dir):
        """他モジュールから ``from ... import`` しただけのクラスは拾わない。"""
        from src.soql_reports import _registry as registry_module
        from src.soql_reports import reports as reports_module

        # ``definitions.py`` で ``SoqlReport`` サブクラスを定義し、
        # ``importer.py`` はそれを再 import するだけにする
        definitions_body = (
            "from src.soql_reports.base import SoqlReport\n"
            "class ImportedReport(SoqlReport):\n"
            "    KEY = '8003'\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        (isolated_reports_dir / "definitions.py").write_text(definitions_body, encoding="utf-8")
        _load_module(reports_module, "definitions")

        importer_body = (
            "from src.soql_reports.reports.definitions import (\n    ImportedReport,\n)\n"
        )
        (isolated_reports_dir / "importer.py").write_text(importer_body, encoding="utf-8")
        _load_module(reports_module, "importer")

        classes = registry_module.registered_reports()
        # ``ImportedReport`` は ``definitions`` モジュールで定義されているので拾われるが、
        # ``importer`` モジュールでは再 import しているだけなので件数が増えない
        assert len(classes) == 1
        assert classes[0].__name__ == "ImportedReport"
        assert classes[0].__module__.endswith(".definitions")

    def test_returns_reports_sorted_by_key(self, isolated_reports_dir):
        """複数レポートを ``KEY`` 昇順で返す。"""
        from src.soql_reports import _registry as registry_module
        from src.soql_reports import reports as reports_module

        for key, name in [("8103", "c_report"), ("8101", "a_report"), ("8102", "b_report")]:
            body = (
                "from src.soql_reports.base import SoqlReport\n"
                f"class {name.capitalize()}(SoqlReport):\n"
                f"    KEY = '{key}'\n"
                "    def soql(self):\n"
                "        return 'SELECT Id FROM Account'\n"
            )
            (isolated_reports_dir / f"{name}.py").write_text(body, encoding="utf-8")
            _load_module(reports_module, name)

        keys = [cls.KEY for cls in registry_module.registered_reports()]
        assert keys == ["8101", "8102", "8103"]

    def test_duplicate_keys_raise_downloader_error_with_filenames(self, isolated_reports_dir):
        """``KEY`` 重複は、どのファイルのどのクラスかをメッセージに出して ``DownloaderError``。"""
        from src.soql_reports import _registry as registry_module
        from src.soql_reports import reports as reports_module

        # 同じ KEY="9001" を2つのモジュールで定義
        body_a = (
            "from src.soql_reports.base import SoqlReport\n"
            "class DupA(SoqlReport):\n"
            "    KEY = '9001'\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        body_b = (
            "from src.soql_reports.base import SoqlReport\n"
            "class DupB(SoqlReport):\n"
            "    KEY = '9001'\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        (isolated_reports_dir / "dup_a.py").write_text(body_a, encoding="utf-8")
        _load_module(reports_module, "dup_a")
        (isolated_reports_dir / "dup_b.py").write_text(body_b, encoding="utf-8")
        _load_module(reports_module, "dup_b")

        with pytest.raises(DownloaderError) as caught:
            registry_module.registered_reports()
        message = str(caught.value)
        # 両方のファイル名とクラス名が出ていること（1件だけだと直せないので両方必要）
        assert "DupA" in message and "dup_a.py" in message
        assert "DupB" in message and "dup_b.py" in message
        # 対処が書いてある
        assert "対処" in message

    def test_empty_key_raises_downloader_error_with_filename(self, isolated_reports_dir):
        """``KEY`` 空のレポートはファイル名付きで ``DownloaderError``。"""
        from src.soql_reports import _registry as registry_module
        from src.soql_reports import reports as reports_module

        body = (
            "from src.soql_reports.base import SoqlReport\n"
            "class EmptyKey(SoqlReport):\n"
            "    KEY = ''\n"
            "    def soql(self):\n"
            "        return 'SELECT Id FROM Account'\n"
        )
        (isolated_reports_dir / "empty_key.py").write_text(body, encoding="utf-8")
        _load_module(reports_module, "empty_key")

        with pytest.raises(DownloaderError) as caught:
            registry_module.registered_reports()
        message = str(caught.value)
        assert "EmptyKey" in message and "empty_key.py" in message
        assert "対処" in message


class TestSoqlReportFor:
    """``soql_report_for()`` — 管理番号から ``SoqlReport`` サブクラスを引く。"""

    def test_returns_matching_report_class(self):
        """差し替えた ``registered_reports()`` から該当する ``KEY`` のクラスを返す。"""
        with patch.object(
            _registry,
            "registered_reports",
            return_value=(_DummyReport, _AllowEmptyReport),
        ):
            assert soql_report_for("9001") is _DummyReport
            assert soql_report_for("9002") is _AllowEmptyReport

    def test_raises_when_key_not_registered(self):
        """該当 ``KEY`` が無いと ``SoqlReportNotRegisteredError``。"""
        with (
            patch.object(_registry, "registered_reports", return_value=(_DummyReport,)),
            pytest.raises(SoqlReportNotRegisteredError) as caught,
        ):
            soql_report_for("9999")
        assert "9999" in str(caught.value)
        assert "9001" in str(caught.value)

    def test_picks_up_registry_changes(self):
        """import 時点のスナップショットではなく、``_registry.registered_reports()`` を都度呼ぶ。"""
        with patch.object(_registry, "registered_reports", return_value=()):
            with pytest.raises(SoqlReportNotRegisteredError):
                soql_report_for("9001")
            with patch.object(
                _registry,
                "registered_reports",
                return_value=(_DummyReport,),
            ):
                assert soql_report_for("9001") is _DummyReport

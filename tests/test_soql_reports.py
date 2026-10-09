"""SOQL レポート取得の基盤（``download_soql_reports()``）を、Salesforce をモックして検証する。

実際のレポート（サブクラス）は作らず、テスト内でダミーの ``SoqlReport`` サブクラスを
定義して使う。``SalesforceBase.bulk_query()`` をモックして Table を返す。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.exceptions import (
    DownloaderError,
    SalesforceError,
)
from comken.toolbox.csv import CSV

from src.exceptions import SoqlReportNotRegisteredError
from src.soql_reports import _registry, soql_report_for
from src.soql_reports import runner as runner_module
from src.soql_reports.base import SoqlReport
from src.soql_reports.runner import _apply_column_names, download_soql_reports

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
    """``bulk_query()`` が ``Table`` を返す Salesforce クライアント。"""
    table = Table(["Id", "Name"], rows if rows is not None else ROWS)
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = table
    site = MagicMock(return_value=client)
    return site


def fake_empty_salesforce() -> MagicMock:
    """``bulk_query()`` が 0 行の ``Table`` を返す Salesforce クライアント。"""
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = Table(["Id", "Name"], [])
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
        # ``COLUMN_NAMES`` の既定値は空辞書（``runner`` 側で何もしない）
        assert SoqlReport.COLUMN_NAMES == {}


class TestDownloadSoqlReports:
    """``download_soql_reports()`` のメインシナリオ。"""

    def test_saves_csv_in_folder(self, folder):
        """取得した Table が ``FOLDER / 概要 /`` 配下へ CSV として保存される。

        2026-09 に定期取得と SOQL レポートを「ベース / 概要」の 2 階層に揃えたので、
        ファイルは ``FOLDER`` 直下ではなく ``FOLDER / 概要 /`` 配下に作られる
        （``src.paths.summary_folder_name()`` が ``_DummyReport.SUMMARY``
        を安全化して ``folder`` の直下に掘る）。
        """
        _DummyReport.FOLDER = str(folder)
        from src.paths import summary_folder_name

        expected_summary = summary_folder_name(_DummyReport.SUMMARY)
        with patch.object(runner_module, "site_for", return_value=fake_salesforce()):
            saved = download_soql_reports([_DummyReport])
        assert len(saved) == 1
        csv_path = saved[0]
        assert csv_path.is_file()
        # ベース直下ではなく、「ベース / 概要」の 2 階層目にある
        assert csv_path.parent == folder / expected_summary
        # ファイル名には概要が混ざらない（管理番号_日時_マイクロ秒.csv）
        assert csv_path.name.startswith(f"{_DummyReport.KEY}_")
        assert "_" + _DummyReport.SUMMARY not in csv_path.name
        with CSV(csv_path, read_only=True) as csv_file:
            assert csv_file.read().to_rows() == ROWS

    def test_saves_csv_under_summary_subfolder_created_on_demand(self, folder):
        """概要のフォルダが無いときは ``_save()`` が ``mkdir`` で自動作成する。

        ``FOLDER`` だけ用意して概要フォルダは作らないでおき、ダウンロードを走らせると
        フォルダが無ければファイルが無いエラー（``FileNotFoundError``）になるはずだが、
        ``mkdir(exist_ok=True)`` を入れることで自動作成され、保存できることを確認する。
        """
        _DummyReport.FOLDER = str(folder)
        from src.paths import summary_folder_name

        summary_dir = folder / summary_folder_name(_DummyReport.SUMMARY)
        assert not summary_dir.exists()
        with patch.object(runner_module, "site_for", return_value=fake_salesforce()):
            saved = download_soql_reports([_DummyReport])
        assert saved[0].is_file()
        assert summary_dir.is_dir()

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

        def bulk_query_side_effect(soql: str) -> Table:
            # ``9003`` だけ例外を投げる（SOQL 文字列で識別）。
            if "FROM Bogus" in soql:
                raise SalesforceError(f"この URL の組織が登録されていません: {URL}")
            table = Table(["Id", "Name"], ROWS)
            return table

        _DummyReport.FOLDER = str(folder)
        _FailingReport.FOLDER = str(folder)
        client = MagicMock()
        client.__enter__.return_value.bulk_query.side_effect = bulk_query_side_effect
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

        def bulk_query_side_effect(_soql: str) -> Table:
            raise SalesforceError(f"この URL の組織が登録されていません: {URL}")

        client = MagicMock()
        client.__enter__.return_value.bulk_query.side_effect = bulk_query_side_effect
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

        def bulk_query_side_effect(_soql: str) -> Table:
            raise TypeError("プログラムバグ")

        client = MagicMock()
        client.__enter__.return_value.bulk_query.side_effect = bulk_query_side_effect
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
        from src.paths import summary_folder_name

        with patch.object(runner_module, "site_for", return_value=fake_empty_salesforce()):
            saved = download_soql_reports([_AllowEmptyReport])
        assert len(saved) == 1
        csv_path = saved[0]
        # ベース直下ではなく、「ベース / 概要」の 2 階層目にある
        assert csv_path.parent == folder / summary_folder_name(_AllowEmptyReport.SUMMARY)
        assert csv_path.is_file()
        with CSV(csv_path, read_only=True) as csv_file:
            table = csv_file.read()
        assert table.to_rows() == []
        # 0 行でも列は見出しとして残る（``SalesforceBase.bulk_query()`` が ``columns`` を返すため）
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
    """``paths.move_into_place()`` の連番制御（``service._save()`` と同じアルゴリズム）。

    2026-09 にファイル名から概要を外したので、衝突テストで作る「ベース名」も
    概要を含まない形（``{KEY}_fixed.csv`` / ``{KEY}_limit.csv``）に揃える。

    2026-10 に ``_reserve_path()`` を ``src.paths.move_into_place()`` （``os.rename``
    ベースの連番探索）に置き換えたので、上限は ``src.paths.RESERVE_PATH_LIMIT``
    側を差し替える。
    """

    def test_existing_file_does_not_get_overwritten(self, folder, monkeypatch):
        """既存ファイルと衝突したら連番（``_1`` / ``_2`` …）で別名を確保する。"""
        _DummyReport.FOLDER = str(folder)
        base_name = f"{_DummyReport.KEY}_fixed.csv"
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
        import src.paths as paths_module

        monkeypatch.setattr(paths_module, "RESERVE_PATH_LIMIT", 5)
        _DummyReport.FOLDER = str(folder)
        base_name = f"{_DummyReport.KEY}_limit.csv"
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


# ``COLUMN_NAMES`` 用のテスト用レポート。 ``_DummyReport`` を上書きすると
# 他のテストへの影響が出るので別クラスにする。
class _ColumnNamesReport(SoqlReport):
    """``COLUMN_NAMES`` のテスト用ダミー実装。"""

    KEY = "9010"
    SUMMARY = "COLUMN_NAMESテスト（テスト用）"
    URL = URL
    FOLDER = ""  # テストで上書きする
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名前",
        "Amount": "金額",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Opportunity"


class _ColumnNamesEmptyReport(SoqlReport):
    """``COLUMN_NAMES`` が空のときのテスト用ダミー実装。"""

    KEY = "9011"
    SUMMARY = "COLUMN_NAMES空テスト（テスト用）"
    URL = URL
    FOLDER = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _ColumnNamesOverlapReport(SoqlReport):
    """``COLUMN_NAMES`` で同じ見出しに 2 列割り当てたテスト用ダミー実装。"""

    KEY = "9012"
    SUMMARY = "COLUMN_NAMES衝突テスト（テスト用）"
    URL = URL
    FOLDER = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名称",
        "Account.Name": "名称",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Account.Name FROM Opportunity"


class _ColumnNamesExtraReport(SoqlReport):
    """``COLUMN_NAMES`` に結果に無い列があるテスト用ダミー実装。"""

    KEY = "9013"
    SUMMARY = "COLUMN_NAMES余分テスト（テスト用）"
    URL = URL
    FOLDER = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名前",
        "Missing": "存在しない列",  # ``soql()`` の結果には無い列
    }

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _ColumnNamesMergeExistingReport(SoqlReport):
    """``COLUMN_NAMES`` の置き換え先が結果に元々ある列名と重なるときのテスト用。

    ``A`` を ``B`` に置き換える ``COLUMN_NAMES`` で、結果にも ``B`` があるケース
    （置換後に見出し ``B`` が 2 つ並ぶ）を再現する。 ``runner._apply_column_names()``
    の衝突検出は ``new_columns`` 全列を見る必要があり、元の実装は
    ``original == new`` の列を飛ばしていたためにこのケースを見逃していた。
    """

    KEY = "9015"
    SUMMARY = "COLUMN_NAMES衝突（既存見出しテスト用）"
    URL = URL
    FOLDER = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "A": "B",  # ``B`` 列は結果に元々ある
    }

    def soql(self) -> str:
        return "SELECT Id, A, B FROM Something"


def _column_names_salesforce(rows: list[dict] | None = None) -> MagicMock:
    """``_ColumnNamesReport`` 用の Table を返す Salesforce クライアント。"""
    table = Table(
        ["Id", "Name", "Amount"],
        rows if rows is not None else [{"Id": "001", "Name": "山田", "Amount": "100"}],
    )
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = table
    site = MagicMock(return_value=client)
    return site


def _overlap_salesforce() -> MagicMock:
    """``_ColumnNamesOverlapReport`` 用の Table を返す Salesforce クライアント。"""
    table = Table(
        ["Id", "Name", "Account.Name"],
        [{"Id": "001", "Name": "x", "Account.Name": "y"}],
    )
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = table
    site = MagicMock(return_value=client)
    return site


def _merge_existing_salesforce() -> MagicMock:
    """``_ColumnNamesMergeExistingReport`` 用の Table を返すクライアント。"""
    table = Table(["Id", "A", "B"], [{"Id": "001", "A": "a", "B": "b"}])
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = table
    site = MagicMock(return_value=client)
    return site


class TestApplyColumnNames:
    """``runner._apply_column_names()`` の挙動確認。"""

    def test_replaces_only_headers(self):
        """値・行順・列順は変わらず、見出しだけ置き換わる。"""
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "山田"}])
        result = _apply_column_names(_ColumnNamesReport, table)
        assert result.columns == ["Id", "名前"]  # Amount は COLUMN_NAMES に無いので残る
        assert result.to_rows() == [{"Id": "1", "名前": "山田"}]

    def test_keeps_columns_not_in_column_names(self):
        """``COLUMN_NAMES`` に無い列は API 名のまま。"""
        table = Table(["Id", "Name", "Amount"], [{"Id": "1", "Name": "x", "Amount": "100"}])
        # ``_ColumnNamesReport.COLUMN_NAMES`` は ``Name`` / ``Amount`` のみ
        result = _apply_column_names(_ColumnNamesReport, table)
        assert result.columns == ["Id", "名前", "金額"]
        assert result.to_rows() == [{"Id": "1", "名前": "x", "金額": "100"}]

    def test_ignores_keys_not_in_result(self):
        """結果に無いキーは無視される（SOQL を変えても対応表を貼り直さずに済む）。"""
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        # ``_ColumnNamesExtraReport`` の ``Missing`` キーは結果に無いので無視
        result = _apply_column_names(_ColumnNamesExtraReport, table)
        assert result.columns == ["Id", "名前"]
        assert result.to_rows() == [{"Id": "1", "名前": "x"}]

    def test_empty_column_names_returns_input_unchanged(self):
        """``COLUMN_NAMES`` が空なら入力がそのまま返る（オブジェクト同一でなくてよい）。"""
        table = Table(["Id", "Name"], [{"Id": "1", "Name": "x"}])
        result = _apply_column_names(_ColumnNamesEmptyReport, table)
        assert result.columns == ["Id", "Name"]
        assert result.to_rows() == [{"Id": "1", "Name": "x"}]

    def test_raises_when_headers_collide(self):
        """置き換え後の見出しが 2 つ以上重なるとエラー。"""
        from src.exceptions import SoqlColumnNameConflictError

        table = Table(
            ["Id", "Name", "Account.Name"],
            [{"Id": "1", "Name": "x", "Account.Name": "y"}],
        )
        with pytest.raises(SoqlColumnNameConflictError) as caught:
            _apply_column_names(_ColumnNamesOverlapReport, table)
        # ``KEY`` と、衝突した見出しと元の列名が両方出ている
        message = str(caught.value)
        assert "9012" in message
        assert "名称" in message
        assert "Name" in message
        assert "Account.Name" in message

    def test_raises_when_replacement_clashes_with_existing_header(self):
        """``COLUMN_NAMES`` の置き換え先が見出しに元々ある列と重なるとエラー。

        ``COLUMN_NAMES = {"A": "B"}`` で結果に ``A`` と ``B`` が両方あるケース。
        置き換え後は見出し ``B`` が 2 つ並ぶ。 ``B → B`` は ``original == new``
        で素通りされるため、 ``new_columns`` 全体を見ないと検出できない。
        """
        from src.exceptions import SoqlColumnNameConflictError

        table = Table(["Id", "A", "B"], [{"Id": "1", "A": "x", "B": "y"}])
        with pytest.raises(SoqlColumnNameConflictError) as caught:
            _apply_column_names(_ColumnNamesMergeExistingReport, table)
        # ``KEY`` と、衝突した見出し ``B``、元の列名 ``A`` / ``B`` が両方出ている
        # （``B`` を元の列としても持つため、 ``B → B`` の素通りも衝突とみなす）
        message = str(caught.value)
        assert "9015" in message
        assert "B" in message
        assert "A" in message


class TestSaveWithColumnNames:
    """``download_soql_reports()`` が保存直前に ``COLUMN_NAMES`` を適用する。"""

    def test_csv_has_replaced_headers(self, folder):
        """保存された CSV の見出しが ``COLUMN_NAMES`` で置き換わっている。"""
        _ColumnNamesReport.FOLDER = str(folder)
        with patch.object(runner_module, "site_for", return_value=_column_names_salesforce()):
            saved = download_soql_reports([_ColumnNamesReport])
        assert len(saved) == 1
        with CSV(saved[0], read_only=True) as csv_file:
            table = csv_file.read()
        # ``Id`` / ``Amount`` は COLUMN_NAMES の対象列だが ``Id`` は未登録、
        # ``Amount`` は COLUMN_NAMES で「金額」になり、 ``Name`` は「名前」になる
        assert table.columns == ["Id", "名前", "金額"]
        assert table.to_rows() == [{"Id": "001", "名前": "山田", "金額": "100"}]

    def test_empty_column_names_produces_api_headers(self, folder):
        """``COLUMN_NAMES`` が空のときは API 名そのまま（従来どおり）。"""
        _ColumnNamesEmptyReport.FOLDER = str(folder)
        site = MagicMock()
        site.return_value.__enter__.return_value.bulk_query.return_value = Table(
            ["Id", "Name"], ROWS
        )
        with patch.object(runner_module, "site_for", return_value=site):
            saved = download_soql_reports([_ColumnNamesEmptyReport])
        with CSV(saved[0], read_only=True) as csv_file:
            table = csv_file.read()
        assert table.columns == ["Id", "Name"]

    def test_collision_does_not_write_file(self, folder):
        """見出しが衝突するとエラーで、ファイルが残らない。"""
        from src.exceptions import SoqlColumnNameConflictError
        from src.paths import summary_folder_name

        _ColumnNamesOverlapReport.FOLDER = str(folder)
        summary_dir = folder / summary_folder_name(_ColumnNamesOverlapReport.SUMMARY)
        with (
            patch.object(runner_module, "site_for", return_value=_overlap_salesforce()),
            pytest.raises(DownloaderError) as caught,
        ):
            download_soql_reports([_ColumnNamesOverlapReport])
        # ``SoqlColumnNameConflictError`` が ``__cause__`` に乗っている
        assert isinstance(caught.value.__cause__, SoqlColumnNameConflictError)
        # ``KEY`` と、衝突した見出し・列名がメッセージに含まれること
        message = str(caught.value.__cause__)
        assert "9012" in message
        assert "名称" in message
        # 概要フォルダ以下に CSV が**作られていない**こと（一時ファイルも残らない）
        assert not summary_dir.exists() or not any(summary_dir.glob("*.csv"))

    def test_replacement_clash_with_existing_header_does_not_write_file(self, folder):
        """置き換え先が見出しに元々ある列と重なると、エラーで CSV が書かれない。

        ``COLUMN_NAMES = {"A": "B"}`` で結果に ``A`` と ``B`` が両方あるケース。
        置き換え後は見出し ``B`` が 2 つ並ぶため ``SoqlColumnNameConflictError``
        を送出してファイルを書かない。
        """
        from src.exceptions import SoqlColumnNameConflictError
        from src.paths import summary_folder_name

        _ColumnNamesMergeExistingReport.FOLDER = str(folder)
        summary_dir = folder / summary_folder_name(_ColumnNamesMergeExistingReport.SUMMARY)
        with (
            patch.object(runner_module, "site_for", return_value=_merge_existing_salesforce()),
            pytest.raises(DownloaderError) as caught,
        ):
            download_soql_reports([_ColumnNamesMergeExistingReport])
        # ``SoqlColumnNameConflictError`` が ``__cause__`` に乗っている
        assert isinstance(caught.value.__cause__, SoqlColumnNameConflictError)
        message = str(caught.value.__cause__)
        assert "9015" in message
        # 概要フォルダ以下に CSV が**作られていない**こと（一時ファイルも残らない）
        assert not summary_dir.exists() or not any(summary_dir.glob("*.csv"))

    def test_empty_rows_with_allow_empty_replaces_headers(self, folder):
        """0 行（``ALLOW_EMPTY`` ○）でも見出しだけ置き換わる。"""
        _ColumnNamesReport.FOLDER = str(folder)
        # ``_ColumnNamesReport`` は ``ALLOW_EMPTY=False`` なので、
        # 衝突を避けるために ``_AllowEmptyReport`` を継承して ``COLUMN_NAMES`` を
        # 後付けで持つクラスを作って使う
        # ここでは ``ALLOW_EMPTY=True`` で ``COLUMN_NAMES`` のあるクラスを別途定義する。

        class _AllowEmptyWithColumnNames(_AllowEmptyReport):
            KEY = "9014"
            SUMMARY = "0行+COLUMN_NAMES（テスト用）"
            COLUMN_NAMES: ClassVar[dict[str, str]] = {
                "Name": "名前",
                "Account.Name": "取引先名",
            }

            def soql(self) -> str:
                return "SELECT Id, Name, Account.Name FROM Opportunity"

        _AllowEmptyWithColumnNames.FOLDER = str(folder)
        site = MagicMock()
        site.return_value.__enter__.return_value.bulk_query.return_value = Table(
            ["Id", "Name", "Account.Name"], []
        )
        with patch.object(runner_module, "site_for", return_value=site):
            saved = download_soql_reports([_AllowEmptyWithColumnNames])
        assert len(saved) == 1
        with CSV(saved[0], read_only=True) as csv_file:
            table = csv_file.read()
        assert table.columns == ["Id", "名前", "取引先名"]
        assert table.to_rows() == []

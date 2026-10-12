"""``SoqlReport`` の基底クラス・登録・``soql_report_for()``・``_apply_column_names()`` の検証。

SOQL レポートの取得・保存は管理表ルート（``src.service._save()``）に統一
したため、 別ルート用のテストは持たない。 ``_apply_column_names()`` の単体
テストと、 ``SoqlReport`` サブクラスの登録・引き当ての挙動を確認する。
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import ClassVar
from unittest.mock import patch

import pytest
from comken.core.table import Table
from comken.exceptions import DownloaderError

from src.exceptions import SoqlReportNotRegisteredError
from src.soql_reports import _registry, soql_report_for
from src.soql_reports.base import SoqlReport
from src.soql_reports.runner import _apply_column_names

ROWS = [{"Id": "001", "Name": "山田"}, {"Id": "002", "Name": "鈴木"}]


class _DummyReport(SoqlReport):
    """``SoqlReport`` のテスト用ダミー実装。"""

    KEY = "9001"

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _OtherReport(SoqlReport):
    """``_DummyReport`` とは別の ``KEY`` を持つダミー。"""

    KEY = "9002"

    def soql(self) -> str:
        return "SELECT Id FROM Bogus"


def _load_module(reports_module, stem: str) -> ModuleType:
    """``reports/__path__`` 内の ``<stem>.py`` を ``importlib.import_module`` で読み込む。

    既に ``sys.modules`` にあれば削除してから読み直す（テスト間のキー衝突を避ける）。
    """
    full_name = f"{reports_module.__name__}.{stem}"
    if full_name in sys.modules:
        del sys.modules[full_name]
    return importlib.import_module(full_name)


@pytest.fixture
def isolated_reports_dir(tmp_path, monkeypatch):
    """``reports/`` の ``__path__`` を ``tmp_path`` に差し替えるフィクスチャ。

    ``registered_reports()`` は ``pkgutil.iter_modules(reports.__path__)`` で
    走査するので、``reports`` モジュールの ``__path__`` を一時ディレクトリに
    差し替えれば、その中へ ``.py`` を書くだけでテスト用レポートを追加できる。
    monkeypatch が終了時に ``__path__`` と ``sys.modules`` を元に戻す。
    """
    from src.soql_reports import reports as reports_module

    monkeypatch.setattr(reports_module, "__path__", [str(tmp_path)])
    yield tmp_path


class TestSoqlReportBase:
    """``SoqlReport`` 基底クラスの挙動確認。"""

    def test_soql_raises_not_implemented(self):
        """``soql()`` はサブクラスで実装する前提。基底のままだと ``NotImplementedError``。"""
        with pytest.raises(NotImplementedError):
            SoqlReport().soql()

    def test_default_attributes(self):
        """``KEY`` と ``COLUMN_NAMES`` の既定値。 接続情報（URL / 保存先 /
        0件可否）は管理表側が単一の正なので、 ``SoqlReport`` には持たせない。"""
        assert SoqlReport.KEY == ""
        assert SoqlReport.COLUMN_NAMES == {}
        # 管理表の単一正にした属性はここには存在しない
        attrs = set(vars(SoqlReport).keys())
        assert "KEY" in attrs
        assert "COLUMN_NAMES" in attrs
        for forbidden in ("SUMMARY", "URL", "FOLDER"):
            assert forbidden not in attrs, forbidden


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
            return_value=(_DummyReport, _OtherReport),
        ):
            assert soql_report_for("9001") is _DummyReport
            assert soql_report_for("9002") is _OtherReport

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
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名前",
        "Amount": "金額",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Opportunity"


class _ColumnNamesEmptyReport(SoqlReport):
    """``COLUMN_NAMES`` が空のときのテスト用ダミー実装。"""

    KEY = "9011"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


class _ColumnNamesOverlapReport(SoqlReport):
    """``COLUMN_NAMES`` で同じ見出しに 2 列割り当てたテスト用ダミー実装。"""

    KEY = "9012"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名称",
        "Account.Name": "名称",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Account.Name FROM Opportunity"


class _ColumnNamesExtraReport(SoqlReport):
    """``COLUMN_NAMES`` に結果に無い列があるテスト用ダミー実装。"""

    KEY = "9013"
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
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "A": "B",  # ``B`` 列は結果に元々ある
    }

    def soql(self) -> str:
        return "SELECT Id, A, B FROM Something"


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

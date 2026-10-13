"""_fetch() の SOQL経由フォールバックと、_save() の COLUMN_NAMES 適用テスト。

管理表の「SOQL」列（``ReportEntry.use_soql``）が真だと、_fetch() がレポートAPIでも
ブラウザ経由でもなく、comken の ``soql_report_for()`` で引いた ``SoqlReport`` 経由で
取得する（「2000件超」列より優先。tests/test_service_browser_fetch.py 側と対）。

取得は ``SalesforceBase.bulk_query()`` 経由（同期の ``query()`` ではない）。

``_save()`` 側では、SOQL 経由のときだけ ``SoqlReport.COLUMN_NAMES`` を保存直前に
適用する（Report API / ブラウザ経由では適用しない）。管理表ルートに統一して
いるので、``SoqlReport`` 経由で取得した Table もここで同じ動きを保証する。

2026-10 に ``soql-collector`` の ``列名の対応.csv`` からの自動取り込みを
``主オブジェクト × SOQL列名`` に切り替えた。 ``_save()`` は ``soql()``
の ``FROM <名前>`` から主オブジェクトを取り、 ``COLUMN_NAMES`` の手書きに
加えてこの CSV からも自動で見出しを差し替える。 手書きが優先される。
ファイルが無い / 該当主オブジェクトの行が無い / 取れない SOQL ケースは
WARNING だけで API 名保存に倒れる（テストでも検証する）。
"""

import logging
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import (
    ColumnNamesCSVFormatError,
    SoqlColumnNameConflictError,
    SoqlReportNotRegisteredError,
)
from src.service import _fetch, _fetch_via_soql, _save
from src.sheets.master import ReportEntry
from src.soql_reports import _registry
from src.soql_reports import column_names as column_names_module
from src.soql_reports.base import SoqlReport

URL = "https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"

ENTRY = ReportEntry(
    key="9001",
    summary="顧客一覧",
    url=URL,
    group="営業本部",
    assignee="山田",
    enabled=True,
    allow_empty=False,
)
SOQL_ENTRY = replace(ENTRY, use_soql=True)
# 「2000件超」も同時に○の行では「SOQL」列が優先される、を確かめる用
SOQL_AND_EXCEEDS_ENTRY = replace(ENTRY, use_soql=True, exceeds_row_limit=True)


class _StubReport(SoqlReport):
    """テスト用の ``SoqlReport`` 実装。"""

    KEY = "9001"

    def soql(self) -> str:
        return "SELECT Id, Name FROM Account"


@pytest.fixture(autouse=True)
def _registered_soql_reports(monkeypatch):
    """``registered_reports()`` がテスト用の1件だけを返すようにする。"""
    monkeypatch.setattr(_registry, "registered_reports", lambda: (_StubReport,))


class TestFetchRoutesToSoql:
    """_fetch() — 「SOQL」列が真の管理番号はSOQL経由になる（「2000件超」より優先）。"""

    def test_uses_soql_when_use_soql_is_true(self):
        table = MagicMock()
        with patch(
            "src.service._fetch_via_soql",
            return_value=table,
        ) as fetch_via_soql:
            result = _fetch(SOQL_ENTRY)

        fetch_via_soql.assert_called_once_with(SOQL_ENTRY)
        assert result is table

    def test_soql_takes_priority_over_exceeds_row_limit(self):
        with (
            patch("src.service._fetch_via_soql") as fetch_via_soql,
            patch("src.service._fetch_via_browser") as fetch_via_browser,
        ):
            _fetch(SOQL_AND_EXCEEDS_ENTRY)

        fetch_via_soql.assert_called_once_with(SOQL_AND_EXCEEDS_ENTRY)
        fetch_via_browser.assert_not_called()

    def test_uses_api_when_use_soql_is_false(self):
        with (
            patch("src.service.site_for") as site_for,
            patch("src.service._fetch_via_soql") as fetch_via_soql,
        ):
            _fetch(ENTRY)

        site_for.assert_called_once_with(ENTRY.url)
        fetch_via_soql.assert_not_called()

    def test_raises_when_filters_given_for_soql_report(self):
        with pytest.raises(ValueError, match=SOQL_ENTRY.key):
            _fetch(SOQL_ENTRY, filters=[{"column": "x", "operator": "equals", "value": "1"}])


class TestFetchViaSoql:
    """_fetch_via_soql() — SOQL経由で取得し、_fetch() と同じ Table を返す。"""

    def test_returns_table_from_bulk_query(self):
        table = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.bulk_query.return_value = table
        site = MagicMock(return_value=client)
        with patch("src.service.site_for", return_value=site) as site_for:
            result = _fetch_via_soql(SOQL_ENTRY)

        site_for.assert_called_once_with(SOQL_ENTRY.url)
        client.__enter__.return_value.bulk_query.assert_called_once_with(
            "SELECT Id, Name FROM Account"
        )
        assert result is table

    def test_raises_when_no_matching_soql_report_registered(self):
        """管理表の「SOQL」列が○なのに、対応する SoqlReport が無ければ設定ミスとして止める。"""
        unregistered = replace(SOQL_ENTRY, key="9999")
        with pytest.raises(SoqlReportNotRegisteredError, match="9999"):
            _fetch_via_soql(unregistered)


# ---------------------------------------------------------------
# 以下、_save() の COLUMN_NAMES 適用テスト（管理表ルートで動く新規仕様）
# ---------------------------------------------------------------


class _ColumnNamesReport(SoqlReport):
    """``_StubReport`` とは別の管理番号で、``COLUMN_NAMES`` を持つダミー。"""

    KEY = "9100"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名前",
        "Amount": "金額",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Opportunity"


class _ColumnNamesOverlapReport(SoqlReport):
    """``COLUMN_NAMES`` で同じ見出しに 2 列割り当てた衝突用のダミー。"""

    KEY = "9101"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名称",
        "Account.Name": "名称",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Account.Name FROM Opportunity"


class _ColumnNamesManualReport(SoqlReport):
    """``COLUMN_NAMES`` で ``Name`` を別名にする手書きのダミー。

    ``列名の対応.csv`` との優先順位確認用。
    """

    KEY = "9200"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {
        "Name": "名前（手書き）",
    }

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Opportunity"


class _ColumnNamesFromFileOnlyReport(SoqlReport):
    """``COLUMN_NAMES`` が空で、 ``列名の対応.csv`` だけから拾うダミー。"""

    KEY = "9201"
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        return "SELECT Id, Name, Amount FROM Opportunity"


def _patched_registry(monkeypatch, *classes: type[SoqlReport]) -> None:
    """``_registry.registered_reports()`` をテスト用のクラス群に差し替える。"""
    monkeypatch.setattr(_registry, "registered_reports", lambda: classes)


def _patched_paths_output(monkeypatch, folder: Path, *, fixed_name: str | None = None) -> None:
    """``paths.output_path()`` を ``folder`` 配下の固定パスに差し替える。

    ベースの実装は管理表・設定シートを引いてくるので、テストではそこを
    すっ飛ばして ``tmp_path`` 直下へ向ける。 ``fixed_name`` を指定すると
    ファイル名も固定できる（衝突テスト用）。
    """
    from src import paths as paths_module_inner

    name = fixed_name or "9201_20240101_0900.csv"

    def fake_output_path(entry, schedule_run_time=None, *, now=None):
        return folder / name

    monkeypatch.setattr(paths_module_inner, "output_path", fake_output_path)


def _registered_for_save(monkeypatch, *classes: type[SoqlReport]) -> None:
    """``soql_report_for()`` がテスト用クラス群を返す状態にする。"""
    # ``_registry`` 側を差し替えれば ``soql_report_for()`` も自動で切り替わる
    _patched_registry(monkeypatch, *classes)


@pytest.fixture
def output_folder(monkeypatch, tmp_path) -> Path:
    """``_save()`` の出力先（``paths.output_path`` の戻り値）を ``tmp_path`` 配下に向ける。"""
    folder = tmp_path / "out"
    folder.mkdir()
    _patched_paths_output(monkeypatch, folder)
    return folder


class TestSaveAppliesColumnNames:
    """管理表ルート（``_save()`` 経由）で ``COLUMN_NAMES`` が保存 CSV の見出しへ反映される。"""

    def test_column_names_applied_to_csv_headers(
        self, monkeypatch, output_folder,
    ):
        """SOQL 経由（``use_soql=True``）のとき、保存 CSV の見出しが ``COLUMN_NAMES`` で
        置き換わる。"""
        _registered_for_save(monkeypatch, _ColumnNamesReport)
        # 管理表の KEY と ``_ColumnNamesReport.KEY`` を合わせる
        entry = replace(SOQL_ENTRY, key=_ColumnNamesReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # ``Id`` は COLUMN_NAMES に無いのでそのまま、 ``Name`` → 「名前」、
        # ``Amount`` → 「金額」になる
        assert result.columns == ["Id", "名前", "金額"]
        assert result.to_rows() == [{"Id": "001", "名前": "山田", "金額": "100"}]

    def test_api_route_does_not_apply_column_names(
        self, monkeypatch, output_folder,
    ):
        """Report API 経由（``use_soql=False``）のときは見出しが変わらない。

        ``SoqlReport.COLUMN_NAMES`` を持つレポートが登録されていても、
        API 経路では適用しない（``SoqlReport`` 経由でしか取らないので
        そもそも ``_apply_column_names()`` は呼ばれない、というのが正）。
        """
        # ``_ColumnNamesReport`` を登録しておく（API 経路では使われない）
        _registered_for_save(monkeypatch, _ColumnNamesReport)
        entry = replace(ENTRY, key=_ColumnNamesReport.KEY)  # use_soql=False のまま
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # 見出しは API 名のまま
        assert result.columns == ["Id", "Name", "Amount"]
        assert result.to_rows() == [{"Id": "001", "Name": "山田", "Amount": "100"}]

    def test_column_names_conflict_raises_and_does_not_write_file(
        self, monkeypatch, output_folder,
    ):
        """管理表ルートで見出しが衝突すると ``SoqlColumnNameConflictError`` で
        失敗し、 CSV が一切残らない（一時ファイルも本番ファイルも）。"""
        from src import paths as paths_module_inner

        _registered_for_save(monkeypatch, _ColumnNamesOverlapReport)
        entry = replace(SOQL_ENTRY, key=_ColumnNamesOverlapReport.KEY)
        table = Table(
            ["Id", "Name", "Account.Name"],
            [{"Id": "001", "Name": "x", "Account.Name": "y"}],
        )

        # 期待する本番パスとその一時ファイル（``~`` プレフィックス）が
        # 保存後に残らないことを確認する
        final_csv = output_folder / "9201_20240101_0900.csv"
        # 一時ファイル名のフォーマットは ``paths.make_temp_csv_path`` に従う
        tmp_csv = paths_module_inner.make_temp_csv_path(final_csv)

        with pytest.raises(SoqlColumnNameConflictError) as caught:
            _save(entry, table)

        # 衝突した見出し・元列名・KEY がメッセージに出る
        message = str(caught.value)
        assert _ColumnNamesOverlapReport.KEY in message
        assert "名称" in message
        # CSV は何も残らない
        assert not final_csv.exists()
        assert not tmp_csv.exists()
        # 出力フォルダ内に CSV が一つも無いこと
        assert not any(output_folder.glob("*.csv"))


# ---------------------------------------------------------------
# 以下、_save() の ``列名の対応.csv`` 経由の COLUMN_NAMES 適用テスト
# 2026-10 から (主オブジェクト, SOQL列名) で引く形になっている
# ---------------------------------------------------------------


# テストで使う ``列名の対応.csv`` のヘッダ順。 soql-collector との取り決め
_HEADER: list[str] = ["主オブジェクト", "SOQL列名", "表示名", "備考", "更新日"]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    """``列名の対応.csv`` を ``comken.toolbox.csv.CSV`` で書く（soql-collector と同じ形）。

    soql-collector は UTF-8 BOM 付き CRLF の CSV を出力する（ ``CSV`` クラスの既定）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    dict_rows = [dict(zip(_HEADER, row, strict=True)) for row in rows]
    with CSV(path, columns=_HEADER) as csv_file:
        csv_file.replace(dict_rows)


@pytest.fixture
def column_csv_path(tmp_path, monkeypatch):
    """``paths.COLUMN_NAMES_CSV_PATH`` を ``tmp_path`` 配下に差し替える。"""
    path = tmp_path / "soql-collector" / "列名の対応.csv"
    monkeypatch.setattr(paths_module, "COLUMN_NAMES_CSV_PATH", path)
    monkeypatch.setattr(column_names_module, "COLUMN_NAMES_CSV_PATH", path)
    yield path


class TestSaveAppliesFileColumnNames:
    """``_save()`` が ``列名の対応.csv`` から自動で ``COLUMN_NAMES`` を組み立てる検証。
    """

    def test_save_applies_file_column_names_to_csv(
        self, monkeypatch, output_folder, column_csv_path,
    ):
        """``COLUMN_NAMES`` が空のレポートでも、 そのレポートの SOQL の主オブジェクトに
        該当する ``列名の対応.csv`` の対応があれば保存 CSV の見出しが置き換わる。

        ``Opportunity`` が主オブジェクトなので、 ``Opportunity`` 行だけが残る形になっている。
        """
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        _write_csv(
            column_csv_path,
            [
                ["Opportunity", "Name", "名前", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        entry = replace(SOQL_ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        assert result.columns == ["Id", "名前", "金額"]
        assert result.to_rows() == [{"Id": "001", "名前": "山田", "金額": "100"}]

    def test_save_does_not_apply_other_main_object_rows(
        self, monkeypatch, output_folder, column_csv_path,
    ):
        """``列名の対応.csv`` に他主オブジェクトの行があっても、 このレポートの
        SOQL の主オブジェクトの行だけ使われる。"""
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        _write_csv(
            column_csv_path,
            [
                ["Account", "Name", "混入してはいけない名前", "", "2026-10-01"],
                ["Account", "Amount", "混入してはいけない金額", "", "2026-10-01"],
                ["Contact", "LastName", "姓", "", "2026-10-01"],
                ["Opportunity", "Name", "名前", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        entry = replace(SOQL_ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # ``Opportunity`` の行だけ反映され、 ``Account`` 行は見出しに入らない
        assert result.columns == ["Id", "名前", "金額"]
        assert "混入してはいけない名前" not in result.columns
        assert "混入してはいけない金額" not in result.columns

    def test_save_manual_column_names_wins_over_file(
        self, monkeypatch, output_folder, column_csv_path,
    ):
        """``COLUMN_NAMES`` と ``列名の対応.csv`` の両方に同じキーがあると、
        手書き（ ``COLUMN_NAMES`` ）が勝つ。"""
        _registered_for_save(monkeypatch, _ColumnNamesManualReport)
        # ファイル側は ``Name -> ファイル表示名`` を提案。手書き ``名前（手書き）`` が勝つ
        _write_csv(
            column_csv_path,
            [
                ["Opportunity", "Name", "ファイル表示名", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        entry = replace(SOQL_ENTRY, key=_ColumnNamesManualReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # Name は手書き（=「名前（手書き）」）が勝ち、 Amount はファイル由来（=「金額」）
        assert result.columns == ["Id", "名前（手書き）", "金額"]
        assert result.to_rows() == [
            {"Id": "001", "名前（手書き）": "山田", "金額": "100"},
        ]

    def test_save_missing_file_keeps_api_names_with_warning(
        self, monkeypatch, output_folder, column_csv_path, caplog,
    ):
        """``列名の対応.csv`` が無いときは API 名のまま保存され、 WARNING が出る。
        取得は失敗させない。"""
        # ``column_csv_path`` は指すがファイルは作らない（fixture の効果）
        assert not column_csv_path.exists()
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        entry = replace(SOQL_ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # API 名のまま
        assert result.columns == ["Id", "Name", "Amount"]
        # WARNING は出ている
        assert "列名の対応ファイルがありません" in caplog.text

    def test_save_no_matching_main_object_keeps_api_names_with_warning(
        self, monkeypatch, output_folder, column_csv_path, caplog,
    ):
        """``列名の対応.csv`` が有っても該当主オブジェクトの行が無いときは
        API 名のまま保存され、 WARNING が出る。"""
        _write_csv(
            column_csv_path,
            [
                ["Account", "Name", "混入してはいけない名前", "", "2026-10-01"],
            ],
        )
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        entry = replace(SOQL_ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # Opportunity の主オブジェクトには該当行が無いので API 名のまま
        assert result.columns == ["Id", "Name", "Amount"]
        # WARNING に主オブジェクト名が出ている（ ``列名の対応に主オブジェクト=Opportunity`` ）
        assert (
            "列名の対応に主オブジェクト=" in caplog.text
        )
        assert "Opportunity" in caplog.text

    def test_save_format_error_raises_and_does_not_write_csv(
        self, monkeypatch, output_folder, column_csv_path,
    ):
        """``列名の対応.csv`` のフォーマットが壊れている（旧形式も見出しに必要な列が無い）
        と ``ColumnNamesCSVFormatError`` で失敗し、 CSV が一切残らない。"""
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        # 旧形式の見出し（ ``主オブジェクト`` 列が無い）= フォーマット不正
        column_csv_path.parent.mkdir(parents=True, exist_ok=True)
        with CSV(
            column_csv_path,
            columns=["管理番号", "概要", "SOQL列名", "表示名", "備考"],
        ) as csv_file:
            csv_file.replace(
                [
                    {
                        "管理番号": _ColumnNamesFromFileOnlyReport.KEY,
                        "概要": "案件一覧",
                        "SOQL列名": "Name",
                        "表示名": "名前",
                        "備考": "",
                    },
                ],
            )
        entry = replace(SOQL_ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        # 期待する本番パスとその一時ファイルが保存後に残らないことを確認する
        from src import paths as paths_inner
        final_csv = output_folder / "9201_20240101_0900.csv"
        tmp_csv = paths_inner.make_temp_csv_path(final_csv)

        with pytest.raises(ColumnNamesCSVFormatError):
            _save(entry, table)

        assert not final_csv.exists()
        assert not tmp_csv.exists()
        assert not any(output_folder.glob("*.csv"))

    def test_save_api_route_unchanged_even_when_file_present(
        self, monkeypatch, output_folder, column_csv_path,
    ):
        """Report API 経由（ ``use_soql=False`` ）のときは ``列名の対応.csv`` が
        有っても見出しは変わらない。 ``SoqlReport`` 経由でしか取らないので、
        そもそも ``_apply_column_names()`` は呼ばれないのが正。"""
        # ``_ColumnNamesFromFileOnlyReport`` を登録しておく（API 経路では使われない）
        _registered_for_save(monkeypatch, _ColumnNamesFromFileOnlyReport)
        _write_csv(
            column_csv_path,
            [
                ["Opportunity", "Name", "名前", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        entry = replace(ENTRY, key=_ColumnNamesFromFileOnlyReport.KEY)
        # ``use_soql=False`` のまま
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # API 名のまま（COLUMN_NAMES もファイルも適用されない）
        assert result.columns == ["Id", "Name", "Amount"]

    def test_save_log_message_includes_breakdown(
        self, monkeypatch, output_folder, column_csv_path, caplog,
    ):
        """``COLUMN_NAMES で N 列を置き換えました（ファイル=M, 手書き=K）`` の
        ログが出る（ファイル由来と手書きの内訳が分かる文言）。"""
        _registered_for_save(monkeypatch, _ColumnNamesManualReport)
        _write_csv(
            column_csv_path,
            [
                ["Opportunity", "Name", "ファイル表示名", "", "2026-10-01"],
                ["Opportunity", "Amount", "金額", "", "2026-10-01"],
            ],
        )
        entry = replace(SOQL_ENTRY, key=_ColumnNamesManualReport.KEY)
        table = Table(
            ["Id", "Name", "Amount"],
            [{"Id": "001", "Name": "山田", "Amount": "100"}],
        )

        with caplog.at_level(logging.INFO, logger="src.service"):
            _save(entry, table)

        # ``ファイル=`` と ``手書き=`` の両方が出ている
        assert "ファイル=" in caplog.text
        assert "手書き=" in caplog.text
        # Name は手書きで置換、 Amount はファイルから置換 → 両方カウント 1
        assert "ファイル=1" in caplog.text
        assert "手書き=1" in caplog.text


class TestSaveNoFromInSoql:
    """SOQL から主オブジェクトを取り出せないケース（ ``FROM`` が無い等）の挙動確認。

    取得は失敗させず、 WARNING を 1 行出して API 名のまま保存する。
    """

    def test_save_soql_without_from_keeps_api_names_with_warning(
        self, monkeypatch, output_folder, column_csv_path, caplog,
    ):
        """``FROM`` の無い SOQL では主オブジェクトが取れず、 API 名のまま保存される
        （ ``column_names`` 側ではなく ``service._save()`` 側で WARNING が出る）。"""

        class _NoFromReport(SoqlReport):
            KEY = "9300"
            COLUMN_NAMES: ClassVar[dict[str, str]] = {"Name": "名前"}

            def soql(self) -> str:
                # ``FROM`` が無い、壊れた SOQL
                return "SELECT Id, Name"

        _registered_for_save(monkeypatch, _NoFromReport)
        # ファイルには Account 行を書いておくが、主オブジェクトが取れないので何も適用しない
        _write_csv(
            column_csv_path,
            [
                ["Account", "Name", "ファイル表示名", "", "2026-10-01"],
            ],
        )
        entry = replace(SOQL_ENTRY, key=_NoFromReport.KEY)
        table = Table(
            ["Id", "Name"],
            [{"Id": "001", "Name": "山田"}],
        )

        with caplog.at_level(logging.WARNING, logger="src.service"):
            saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # 手書きの COLUMN_NAMES は適用されない（ extra が空のため ``_apply_column_names`` の
        # 引数 ``extra={}`` で COLUMN_NAMES がそのまま使われるはずだが、
        # バグが無いようにするためにも結果を確認）
        assert result.columns == ["Id", "名前"]
        # WARNING に ``SOQL から主オブジェクトを取り出せませんでした`` が出ている
        assert "SOQL から主オブジェクトを取り出せませんでした" in caplog.text

    def test_save_warning_does_not_leak_soql_body(
        self, monkeypatch, output_folder, column_csv_path, caplog,
    ):
        """主オブジェクトが取り出せない SOQL の WARNING 本文に、 SOQL 本文
        （WHERE の値等）が漏れていない。 SOQL 本文は同じ場所の DEBUG にだけ出る
        （既存の ``logger.debug("SOQL: %s", ...)`` と同ルール）。"""

        marker = "SECRET_LEAK_MARKER_value@example.com"

        class _NoFromReport(SoqlReport):
            KEY = "9301"
            COLUMN_NAMES: ClassVar[dict[str, str]] = {}

            def soql(self) -> str:
                # ``FROM`` が無いので主オブジェクトが取れない壊れた SOQL。
                # 本文に目印を入れて、 WARNING 以上に漏れないことを確認する
                return f"SELECT Id, Name, {marker}"

        _registered_for_save(monkeypatch, _NoFromReport)
        entry = replace(SOQL_ENTRY, key=_NoFromReport.KEY)
        table = Table(
            ["Id", "Name"],
            [{"Id": "001", "Name": "山田"}],
        )

        # DEBUG まで拾わないと SOQL 本文の DEBUG 記録を確認できない
        with caplog.at_level(logging.DEBUG, logger="src.service"):
            _save(entry, table)

        service_records = [r for r in caplog.records if r.name == "src.service"]
        warnings_and_above = [
            r for r in service_records if r.levelno >= logging.WARNING
        ]
        debugs = [r for r in service_records if r.levelno == logging.DEBUG]

        # WARNING 以上の ``src.service`` ログに、 SOQL 本文（目印）が含まれていない
        for record in warnings_and_above:
            assert marker not in record.getMessage(), (
                f"WARNING 以上のログに SOQL 本文が漏れています: "
                f"level={record.levelname}, message={record.getMessage()!r}"
            )
        # DEBUG には SOQL 本文が出ている（= DEBUG に移された動作も同時に検証）
        assert any(marker in record.getMessage() for record in debugs), (
            "DEBUG に SOQL 本文が出ていません"
        )

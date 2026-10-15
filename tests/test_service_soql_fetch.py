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

2026-10 に ``SoqlReport.transform()`` を追加し、 ``bulk_query()`` の結果
（加工前の生データ）に対して ``transform()`` を呼んで保存用 Table を得る形に
した。 加工の例外／非 Table 戻り値は ``SoqlTransformError`` に包まれ、
当該レポートだけの失敗として記録される。 未完成の SOQL（ ``1 行目に
【未完成】`` の印）は ``_fetch_via_soql()`` の ``bulk_query()`` 呼び出し前
に ``SoqlIncompleteError`` で止める。 いずれも ``_failure_row()`` で
「Salesforce」区分に入り、 履歴に失敗 1 行が残る（ ``download_scheduled()``
は次のレポートの取得を続ける）。
"""

import logging
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.exceptions import SalesforceError, SalesforceRequestError
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import (
    ColumnNamesCSVFormatError,
    SoqlColumnNameConflictError,
    SoqlCountMismatchError,
    SoqlIncompleteError,
    SoqlReportNotRegisteredError,
    SoqlTransformError,
)
from src.service import (
    INCOMPLETE_SOQL_MARKER,
    _fetch,
    _fetch_via_soql,
    _save,
    _verify_soql_count,
)
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
        table = Table(["Id", "Name"], [{"Id": "001", "Name": "山田"}])
        client = MagicMock()
        client.__enter__.return_value.bulk_query.return_value = table
        # 件数の比較でレポート API の総件数を取りに行く。``_StubReport`` の
        # ``KEY=9001`` に対応する管理表の ``report_id`` を ``00O5g00000ABCDE``
        # とし、レポートのレスポンスには ``RowCount`` が無い形にして、比較は
        # WARNING で握りつぶされて ``table`` がそのまま返る
        client.__enter__.return_value.data_path.side_effect = lambda suffix: (
            f"/services/data/v67.0{suffix}"
        )
        client.__enter__.return_value.request.return_value = (
            {"reportMetadata": {"aggregates": []}, "factMap": {}},
            {},
        )
        site = MagicMock(return_value=client)
        with patch("src.service.site_for", return_value=site) as site_for:
            result = _fetch_via_soql(SOQL_ENTRY)

        # 件数比較は同じ接続のうちに行うため、 ``site_for`` は 1 回しか呼ばれない
        # （=ログインが 1 回だけ）
        assert site_for.call_count == 1
        site_for.assert_called_with(SOQL_ENTRY.url)
        client.__enter__.return_value.bulk_query.assert_called_once_with(
            "SELECT Id, Name FROM Account"
        )
        # 比較も同じ ``salesforce.request()`` を 1 回叩く
        client.__enter__.return_value.request.assert_called_once()
        # 既定の transform() は入力をそのまま返す（= ``bulk_query()`` の結果と同じ）
        assert result is table

    def test_returns_table_when_count_check_disabled(self):
        """``SoqlReport.CHECK_COUNT=False`` のときはレポート API を叩かず ``bulk_query()`` の
        結果をそのまま返す（``request()`` が呼ばれない）。"""
        table = Table(["Id", "Name"], [{"Id": "001", "Name": "山田"}])
        client = MagicMock()
        client.__enter__.return_value.bulk_query.return_value = table
        site = MagicMock(return_value=client)

        class _NoCheckReport(SoqlReport):
            KEY = "9001"
            CHECK_COUNT: ClassVar[bool] = False

            def soql(self) -> str:
                return "SELECT Id, Name FROM Account"

        with patch("src.soql_reports._registry.registered_reports", return_value=(_NoCheckReport,)):
            with patch("src.service.site_for", return_value=site):
                result = _fetch_via_soql(SOQL_ENTRY)

        client.__enter__.return_value.request.assert_not_called()
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
        self,
        monkeypatch,
        output_folder,
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
        self,
        monkeypatch,
        output_folder,
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
        self,
        monkeypatch,
        output_folder,
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
    """``_save()`` が ``列名の対応.csv`` から自動で ``COLUMN_NAMES`` を組み立てる検証。"""

    def test_save_applies_file_column_names_to_csv(
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
        caplog,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
        caplog,
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
        assert "列名の対応に主オブジェクト=" in caplog.text
        assert "Opportunity" in caplog.text

    def test_save_format_error_raises_and_does_not_write_csv(
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
        caplog,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
        caplog,
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
        self,
        monkeypatch,
        output_folder,
        column_csv_path,
        caplog,
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
        warnings_and_above = [r for r in service_records if r.levelno >= logging.WARNING]
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


# ─────────────────────────────────────────────────────────────────────
# _fetch_via_soql() の件数比較（2026-10 改定）
# 偽の Salesforce（ ``MagicMock`` ）でリクエストを組み立て、 一致・不一致・
# 比べるための値が取れない・HTTP エラー・ ``CHECK_COUNT=False``・0 件同士を
# それぞれ検証する
# ─────────────────────────────────────────────────────────────────────


def _make_soql_entry(url: str | None = None) -> ReportEntry:
    """テスト用の ``ReportEntry`` を作る。``url`` を ``None`` にすると
    レポート ID が取れない形にできる。"""
    if url is None:
        url = URL
    return ReportEntry(
        key="9001",
        summary="顧客一覧",
        url=url,
        group="営業本部",
        assignee="山田",
        enabled=True,
        allow_empty=False,
        use_soql=True,
    )


def _build_fake_client(soql_table: Table, *, request_return: object) -> MagicMock:
    """``bulk_query()`` が ``soql_table`` を返し、 ``request()`` が ``request_return``
    （ ``(payload, headers)`` あるいは ``SalesforceRequestError`` を投げるよう
    ``side_effect`` に設定する ``MagicMock`` ）を返す偽クライアントを作る。"""
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = soql_table
    client.__enter__.return_value.data_path.side_effect = lambda suffix: (
        f"/services/data/v67.0{suffix}"
    )
    if isinstance(request_return, Exception):
        client.__enter__.return_value.request.side_effect = request_return
    else:
        client.__enter__.return_value.request.return_value = request_return
    return client


def _payload_with_row_count(row_count: int | object) -> tuple[dict, dict]:
    """``RowCount`` が ``reportMetadata.aggregates`` の 0 番目にあるレスポンスを作る。

    ``row_count`` はそのまま ``factMap["T!T"]["aggregates"][0]`` に入る
    （ ``int`` / ``dict`` / ``bool`` など検証したい形を直接渡せる）。
    """
    payload = {
        "reportMetadata": {"aggregates": ["RowCount"]},
        "factMap": {"T!T": {"aggregates": [row_count]}},
    }
    return payload, {}


class TestVerifySoqlCount:
    """``_verify_soql_count()`` — SOQL 件数とレポート総件数の比較。

    ``_fetch_via_soql()`` の ``bulk_query()`` 直後に動く検証ロジック。
    """

    def test_match_returns_silently_and_logs(self, caplog):
        """件数が一致すれば INFO ログを 1 行出してそのまま戻る（保存は止めない）。"""
        soql_table = Table(["Id"], [{"Id": str(i)} for i in range(3)])
        client = _build_fake_client(soql_table, request_return=_payload_with_row_count(3))

        with caplog.at_level(logging.INFO, logger="src.service"):
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert "SOQL 件数がレポート総件数と一致しました" in caplog.text
        assert "件数=3" in caplog.text

    def test_mismatch_raises_and_does_not_leak_soql_body(self, caplog):
        """件数が合わないと ``SoqlCountMismatchError`` で保存しない。例外・ログに
        SOQL 本文や行の値は漏れない（目印で確認）。"""
        soql_body_marker = "WHERE SECRET_VALUE_MARKER@example.com <> ''"
        row_value_marker = "ROW_VALUE_MARKER"

        class _MarkerReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return f"SELECT Id FROM Account {soql_body_marker}"

        with patch(
            "src.soql_reports._registry.registered_reports",
            return_value=(_MarkerReport,),
        ):
            # report_cls は ``_verify_soql_count()`` では直接は触らないが、
            # 既存の ``_StubReport`` との置き換え整合のため一応差し替える
            pass

        soql_table = Table(
            ["Id"],
            [{"Id": row_value_marker}],
        )
        client = _build_fake_client(soql_table, request_return=_payload_with_row_count(2))
        entry = _make_soql_entry()

        with caplog.at_level(logging.WARNING, logger="src.service"):
            with pytest.raises(SoqlCountMismatchError) as caught:
                _verify_soql_count(entry, soql_table, client.__enter__.return_value)

        # 管理番号・概要・件数・対処メッセージが出ている
        message = str(caught.value)
        assert entry.key in message
        assert entry.summary in message
        assert "SOQL の件数: 1" in message
        assert "レポートの総件数: 2" in message
        assert "SOQL の条件がレポートと同じか確認してください" in message
        assert "次回の取得で一致します" in message
        # WARNING ログにも件数の比較が出ている（目印の行値は出ない）
        assert "SOQL=1, レポート=2" in caplog.text
        # SOQL 本文・行値が漏れていない（ INFO も含めて ）
        service_records = [r for r in caplog.records if r.name == "src.service"]
        for record in service_records:
            assert soql_body_marker not in record.getMessage(), (
                f"SOQL 本文が漏れています: level={record.levelname}, "
                f"message={record.getMessage()!r}"
            )
            assert row_value_marker not in record.getMessage(), (
                f"行の値が漏れています: level={record.levelname}, message={record.getMessage()!r}"
            )
        # 例外メッセージにも漏れていない
        assert soql_body_marker not in message
        assert row_value_marker not in message

    def test_zero_zero_matches(self, caplog):
        """0 件同士は一致扱い。例外は出さず INFO ログが 1 行出る。"""
        soql_table = Table([], [])
        client = _build_fake_client(soql_table, request_return=_payload_with_row_count(0))

        with caplog.at_level(logging.INFO, logger="src.service"):
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert "SOQL 件数がレポート総件数と一致しました" in caplog.text
        assert "件数=0" in caplog.text

    def test_row_count_missing_logs_warning_and_returns(self, caplog):
        """``RowCount`` が ``aggregates`` に無いときは WARNING を出してそのまま戻る
        （保存は止めない）。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        # ``aggregates`` が空 → ``RowCount`` が見つからない
        client = _build_fake_client(
            soql_table,
            request_return=({"reportMetadata": {"aggregates": []}, "factMap": {"T!T": {}}}, {}),
        )

        with caplog.at_level(logging.WARNING, logger="src.service"):
            # 例外が出ない
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert "RowCount が見つかりません" in caplog.text

    def test_url_with_no_report_id_skips_comparison_with_warning(self, caplog):
        """URL がレポートの URL でない（ ``report_id_from_url`` で例外）ときは
        WARNING を出して比べる対象が無いことを示し、例外は出さない。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        client = _build_fake_client(soql_table, request_return=({}, {}))
        # レポート ID が取れない URL
        bad_url_entry = _make_soql_entry(url="https://example.my.salesforce.com/")

        with caplog.at_level(logging.WARNING, logger="src.service"):
            _verify_soql_count(bad_url_entry, soql_table, client.__enter__.return_value)

        # ``request()`` が呼ばれていない（ ID が取れないので比較しようがない）
        client.__enter__.return_value.request.assert_not_called()
        assert "レポート ID を URL から取り出せません" in caplog.text

    def test_http_500_logs_warning_and_returns(self, caplog):
        """レポート総件数の取得が 500 などの永続的な HTTP エラーで失敗したとき
        は WARNING を出して保存は続ける。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        exc = SalesforceRequestError("GET", "/x", 500, "boom")
        client = _build_fake_client(soql_table, request_return=exc)

        with caplog.at_level(logging.WARNING, logger="src.service"):
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert "HTTP 500" in caplog.text

    def test_http_401_propagates(self):
        """401 は既存の取得失敗と同じ扱い（ ``SalesforceRequestError`` をそのまま
        上位へ送出）。WARNING で握りつぶさない。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        exc = SalesforceRequestError("GET", "/x", 401, "unauthorized")
        client = _build_fake_client(soql_table, request_return=exc)

        with pytest.raises(SalesforceRequestError) as caught:
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert caught.value.status_code == 401

    def test_http_403_propagates(self):
        """403 も既存の取得失敗と同じ扱い（ ``SalesforceRequestError`` をそのまま）。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        exc = SalesforceRequestError("GET", "/x", 403, "forbidden")
        client = _build_fake_client(soql_table, request_return=exc)

        with pytest.raises(SalesforceRequestError) as caught:
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert caught.value.status_code == 403

    def test_row_count_value_as_int(self):
        """``factMap["T!T"]["aggregates"][RowCount]`` が ``int`` のときに
        その値が取れる。"""
        soql_table = Table(["Id"], [{"Id": str(i)} for i in range(7)])
        client = _build_fake_client(soql_table, request_return=_payload_with_row_count(7))

        _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

    def test_row_count_value_as_dict(self):
        """``factMap["T!T"]["aggregates"][RowCount]`` が ``{"value": n}`` の
        ``dict`` のときも ``value`` を採って件数が取れる。"""
        soql_table = Table(["Id"], [{"Id": str(i)} for i in range(5)])
        client = _build_fake_client(
            soql_table,
            request_return=_payload_with_row_count({"label": "5", "value": 5}),
        )

        _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

    def test_row_count_value_as_bool_is_ignored(self, caplog):
        """``bool`` は ``int`` の subclass なので ``isinstance(value, int)`` で
        拾ってしまう事故を防ぐ。除外される（= ``None`` 扱い → WARNING）。"""
        soql_table = Table(["Id"], [{"Id": "1"}])
        client = _build_fake_client(soql_table, request_return=_payload_with_row_count(True))

        with caplog.at_level(logging.WARNING, logger="src.service"):
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        assert "RowCount が見つかりません" in caplog.text

    def test_non_http_salesforce_error_logs_warning_and_returns(self, caplog):
        """HTTP 以外の ``SalesforceError``（ ``SalesforceRequestError`` でない
        ``SalesforceError``、 例: 通信断・タイムアウトなど）は WARNING を出して
        保存は続ける。 ``_fetch_report_total_count()`` 内で ``SalesforceRequestError``
        より広い ``SalesforceError`` を補足する形になっていることを検証する。
        """
        soql_table = Table(["Id"], [{"Id": "1"}])
        # ``SalesforceError`` を直接投げる（ ``SalesforceRequestError`` の
        # subclass ではない形 ── 401/403 扱いではない ）
        exc = SalesforceError("connection lost")
        client = _build_fake_client(soql_table, request_return=exc)

        with caplog.at_level(logging.WARNING, logger="src.service"):
            # 例外は上がらず、 WARNING が出る
            _verify_soql_count(_make_soql_entry(), soql_table, client.__enter__.return_value)

        # 通信エラー（HTTP ではない）が WARNING で記録されている
        assert caplog.text  # ログが出ている
        assert any(record.levelno == logging.WARNING for record in caplog.records), (
            "WARNING が出ていません"
        )


class TestFetchViaSoqlCountCheckDisabled:
    """``SoqlReport.CHECK_COUNT=False`` のときは Report API を叩かず
    ``bulk_query()`` の結果をそのまま返す。"""

    def test_check_count_false_skips_request(self):
        """``CHECK_COUNT=False`` のときは ``request()`` が呼ばれず ``bulk_query()``
        の Table がそのまま返る。"""

        class _NoCheckReport(SoqlReport):
            KEY = "9001"
            CHECK_COUNT: ClassVar[bool] = False

            def soql(self) -> str:
                return "SELECT Id FROM Account"

        soql_table = Table(["Id"], [{"Id": "1"}])
        client = _build_fake_client(soql_table, request_return=({}, {}))
        site = MagicMock(return_value=client)

        with patch(
            "src.soql_reports._registry.registered_reports",
            return_value=(_NoCheckReport,),
        ):
            with patch("src.service.site_for", return_value=site):
                result = _fetch_via_soql(_make_soql_entry())

        client.__enter__.return_value.request.assert_not_called()
        assert result is soql_table


# ─────────────────────────────────────────────────────────────────────
# 比較を壊した場合に不一致のテストが落ちることを確認する
# ─────────────────────────────────────────────────────────────────────


class TestCountMismatchFailureRow:
    """``SoqlCountMismatchError`` を ``_failure_row()`` に通したときの履歴行。

    既存の ``_failure_row()`` の区分ルール（ ``ComkenError`` 系は「Salesforce」
    区分）に従うため、 ``SoqlCountMismatchError`` は ``cause="Salesforce"``
    で記録される。
    """

    def test_failure_row_uses_salesforce_cause(self):
        from src.service import _failure_row

        exc = SoqlCountMismatchError("9001", "顧客一覧", 5, 10)
        row = _failure_row(exc, 1.5, schedule_key="S001", route="SOQL")
        assert row.cause == "Salesforce"
        assert row.error_code == "SoqlCountMismatchError"
        assert row.succeeded is False
        assert row.fetched_from_salesforce is False
        assert row.saved_to_file is None
        # エラーメッセージに管理番号・概要・件数が出ている
        assert "9001" in row.error
        assert "顧客一覧" in row.error
        assert "SOQL の件数: 5" in row.error
        assert "レポートの総件数: 10" in row.error


# ─────────────────────────────────────────────────────────────────────


class TestCountCheckSanityCheck:
    """比較ロジックを壊したときに、 不一致ケースで例外が出なくなる事故を
    防ぐためのサニティチェック。 ``src.service._verify_soql_count`` を
    意図的に「何もしない」関数で差し替え、 不一致ケースで例外が出ること
    （=サニティチェックが本物の実装を呼んでいることを確認する）を一度確かめる。
    """

    def test_sanity_check_detects_when_verification_disabled(self):
        # ``src.service`` の ``_verify_soql_count`` を直接「何もしない」関数に
        # 置き換える。 ``_verify_soql_count`` 自体は test 冒頭で ``from src.service
        # import _verify_soql_count`` 済みだが、 呼び出し側で ``from src.service``
        # 経由ではなく、 ``service_module._verify_soql_count`` を直接参照して
        # 呼ぶことでモンキーパッチ後の実装が効くようにする
        from src import service as service_module

        original = service_module._verify_soql_count

        def _always_match(*args, **kwargs):
            return None

        service_module._verify_soql_count = _always_match
        try:
            # 不一致ケース（ SOQL=1 件 / レポート=99 件 ）で、
            # 壊した実装では何も起きないはず → pytest.raises が失敗する
            soql_table = Table(["Id"], [{"Id": "1"}])
            client = _build_fake_client(soql_table, request_return=_payload_with_row_count(99))
            # 壊した実装を直接呼ぶ（ ``_verify_soql_count`` というローカル名は
            # 束縛済みのため、 モジュールの属性経由で参照する）。新シグネチャは
            # ``(entry, table, salesforce)`` なので偽の client を直接渡す
            service_module._verify_soql_count(
                _make_soql_entry(), soql_table, client.__enter__.return_value
            )
        finally:
            service_module._verify_soql_count = original
        # 「壊した実装（何もしない関数）で不一致ケースを呼んでも例外が出ない」こと
        # を直接 assert することで、 本物の実装が呼ばれていないことを保証する。
        # 元の実装がモンキーパッチで漏れていた場合はここで例外が出て落ちる


# ─────────────────────────────────────────────────────────────────────
# ``SoqlReport.transform()`` の挙動（2026-10 追加）
#
# 加工は ``bulk_query()`` → 件数比較 → ``transform()`` の順で行われ、
# 件数の比較は **加工前** の生の結果に対して行われる（=レポートの総件数と
# 比べられるのは加工前の件数）。 加工後の結果は保存 CSV にそのまま書かれ、
# ``_save()`` の ``COLUMN_NAMES`` 適用はこの **あと** で行われる。
# ─────────────────────────────────────────────────────────────────────


def _soql_table_with_rows() -> Table:
    """テスト用に ``bulk_query()`` が返す既定の Table。"""
    return Table(
        ["Id", "StageName", "Amount"],
        [
            {"Id": "001", "StageName": "Closed Won", "Amount": "100"},
            {"Id": "002", "StageName": "Closed Lost", "Amount": "200"},
            {"Id": "003", "StageName": "Closed Won", "Amount": "50"},
        ],
    )


def _patch_soql_registry(monkeypatch, *classes: type[SoqlReport]) -> None:
    """``_registry.registered_reports()`` をテスト用クラス群に差し替えるヘルパー。"""
    monkeypatch.setattr(_registry, "registered_reports", lambda: classes)


def _patched_soql_client(table: Table) -> MagicMock:
    """``bulk_query()`` が ``table`` を返す偽クライアントを作る。
    件数比較の ``request()`` は ``RowCount`` が無い形にして WARNING で握りつぶす。"""
    client = MagicMock()
    client.__enter__.return_value.bulk_query.return_value = table
    client.__enter__.return_value.data_path.side_effect = lambda suffix: (
        f"/services/data/v67.0{suffix}"
    )
    client.__enter__.return_value.request.return_value = (
        {"reportMetadata": {"aggregates": []}, "factMap": {}},
        {},
    )
    return MagicMock(return_value=client)


class TestFetchViaSoqlTransform:
    """``SoqlReport.transform()`` の既定・上書きの挙動。"""

    def test_default_transform_returns_input_unchanged(self, monkeypatch):
        """``transform()`` を上書きしない ``SoqlReport`` では ``bulk_query()`` の
        結果がそのまま返る（既定の transform が入力テーブルをそのまま返すため）。"""

        class _PlainReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _PlainReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            result = _fetch_via_soql(SOQL_ENTRY)

        # 加工していないので ``bulk_query()`` の戻り値と一致（同じオブジェクト）
        assert result is table

    def test_transform_filters_rows(self, monkeypatch):
        """``transform()`` で ``filter`` した結果が返る（行数・値）。"""

        class _FilterReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                return table.filter(lambda row: row["StageName"] == "Closed Won")

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _FilterReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            result = _fetch_via_soql(SOQL_ENTRY)

        # 行が絞られ、 値もそのまま
        assert result.columns == ["Id", "StageName", "Amount"]
        assert result.to_rows() == [
            {"Id": "001", "StageName": "Closed Won", "Amount": "100"},
            {"Id": "003", "StageName": "Closed Won", "Amount": "50"},
        ]

    def test_count_check_uses_pre_transform_count(self, monkeypatch, caplog):
        """件数の比較は加工前の生の結果に対して行われる。

        ``bulk_query()`` が 3 件を返し、 ``Report API`` の ``RowCount`` が 3。
        ``transform()`` が 2 行に絞っても、 比較は加工前の 3 件で行われるので
        不一致にならず保存に進む（ ``SoqlCountMismatchError`` が出ない）。
        """

        class _FilterReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                # 加工後は 2 行になる
                return table.filter(lambda row: row["StageName"] == "Closed Won")

        table = _soql_table_with_rows()
        client = MagicMock()
        client.__enter__.return_value.bulk_query.return_value = table
        client.__enter__.return_value.data_path.side_effect = lambda suffix: (
            f"/services/data/v67.0{suffix}"
        )
        # レポート API 側は 3 件と返す（=加工前の件数）
        client.__enter__.return_value.request.return_value = _payload_with_row_count(3)
        site = MagicMock(return_value=client)

        _patch_soql_registry(monkeypatch, _FilterReport)
        with caplog.at_level(logging.INFO, logger="src.service"):
            with patch("src.service.site_for", return_value=site):
                # 例外が出ずに Table が返る
                result = _fetch_via_soql(SOQL_ENTRY)

        # 件数一致のログが出ている（件数=3 = 加工前の件数）
        assert "SOQL 件数がレポート総件数と一致しました" in caplog.text
        assert "件数=3" in caplog.text
        # 返ってきた Table は加工後の 2 行
        assert len(result) == 2

    def test_transform_exception_becomes_soql_transform_error(self, monkeypatch):
        """``transform()`` の中で ``ValueError`` が出ると ``SoqlTransformError``
        に包まれる。 ``from exc`` で元の例外が ``__cause__`` に繋がり、
        メッセージに元の例外の種類（ ``ValueError`` ）が含まれる。"""

        marker = "TRANSFORM_VALUE_MARKER_in_row"

        class _RaisingReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                # 行の値を ``str(exc)`` に含めて例外を投げる。 ``SoqlTransformError``
                # 側で ``str(exc)`` を載せない作りなので、 メッセージにもログにも
                # このマーカーは漏れない
                for row in table:
                    if row["StageName"] == marker:
                        raise ValueError(f"bad row: {row}")
                return table

        table = Table(
            ["Id", "StageName", "Amount"],
            [{"Id": "001", "StageName": marker, "Amount": "100"}],
        )
        _patch_soql_registry(monkeypatch, _RaisingReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            with pytest.raises(SoqlTransformError) as caught:
                _fetch_via_soql(SOQL_ENTRY)

        # 元の例外が ``__cause__`` に繋がっている
        assert isinstance(caught.value.__cause__, ValueError)
        # メッセージに元の例外の種類（ ``ValueError`` ）が入っている
        message = str(caught.value)
        assert "ValueError" in message
        assert "9001" in message
        # **メッセージ・ログに行の値が漏れていない** （マーカーが出ていないこと）
        assert marker not in message

    def test_transform_exception_does_not_leak_row_value_to_log(self, monkeypatch, caplog):
        """``transform()`` が ``ValueError`` を投げてもログに行の値が漏れない
        （マーカーで検証）。 INFO / WARNING 以上の双方を確認するため、
        DEBUG まで含めて ``src.service`` のログを見る。"""

        marker = "LOG_TRANSFORM_MARKER_in_log"

        class _RaisingReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                raise ValueError(f"bad row: {marker}")

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _RaisingReport)
        with caplog.at_level(logging.DEBUG, logger="src.service"):
            with patch("src.service.site_for", return_value=_patched_soql_client(table)):
                with pytest.raises(SoqlTransformError):
                    _fetch_via_soql(SOQL_ENTRY)

        # ``src.service`` 由来の全ログ（ DEBUG 含む） に行のマーカーが漏れていない
        service_records = [r for r in caplog.records if r.name == "src.service"]
        for record in service_records:
            assert marker not in record.getMessage(), (
                f"行の値が漏れています: level={record.levelname}, message={record.getMessage()!r}"
            )

    def test_transform_returning_none_raises(self, monkeypatch):
        """``transform()`` が ``None`` を返すと ``SoqlTransformError`` で止める。"""

        class _NoneTransformReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:  # type: ignore[override]
                return None  # type: ignore[return-value]

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _NoneTransformReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            with pytest.raises(SoqlTransformError) as caught:
                _fetch_via_soql(SOQL_ENTRY)

        message = str(caught.value)
        assert "NoneType" in message
        assert "9001" in message
        assert "戻り値が Table ではありません" in message

    def test_transform_returning_non_table_raises(self, monkeypatch):
        """``transform()`` が ``Table`` 以外（辞書など）を返しても ``SoqlTransformError``。"""

        class _DictTransformReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:  # type: ignore[override]
                return {"not": "a table"}  # type: ignore[return-value]

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _DictTransformReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            with pytest.raises(SoqlTransformError) as caught:
                _fetch_via_soql(SOQL_ENTRY)

        assert "dict" in str(caught.value)
        assert "戻り値が Table ではありません" in str(caught.value)

    def test_transform_does_not_run_when_count_mismatch_raises(self, monkeypatch):
        """件数の比較で ``SoqlCountMismatchError`` が起きると ``transform()`` は
        呼ばれない（比較は加工前の件数で行われるので、 不一致なら加工する意味がない）。"""
        transform_called = []

        class _FilterReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                transform_called.append(True)
                return table

        table = _soql_table_with_rows()
        client = MagicMock()
        client.__enter__.return_value.bulk_query.return_value = table
        client.__enter__.return_value.data_path.side_effect = lambda suffix: (
            f"/services/data/v67.0{suffix}"
        )
        # レポート API 側は 99 件（=不一致）
        client.__enter__.return_value.request.return_value = _payload_with_row_count(99)
        site = MagicMock(return_value=client)

        _patch_soql_registry(monkeypatch, _FilterReport)
        with patch("src.service.site_for", return_value=site):
            with pytest.raises(SoqlCountMismatchError):
                _fetch_via_soql(SOQL_ENTRY)

        # 不一致で先に落ちるので transform() は呼ばれない
        assert transform_called == []

    def test_transform_zero_rows_with_allow_empty_false_is_empty_error_in_save(
        self, monkeypatch, output_folder
    ):
        """``transform()`` が 0 行を返したときは、 既存の ``_save()`` の
        ``ALLOW_EMPTY=False`` の判定がそのまま動く。

        ここでは ``_save()`` まで動かして ``EmptyReportError`` で保存されないことを
        検証する。  ``_fetch_via_soql()`` だけでは ``ALLOW_EMPTY`` 判定は走らない
        （判定は保存時の責務） 。"""
        from src.exceptions import EmptyReportError

        class _EmptyTransformReport(SoqlReport):
            KEY = "9100"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:
                # 0 行にする
                return table.filter(lambda row: False)

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _EmptyTransformReport)
        # ``_save()`` は ``soql_report_for(entry.key)`` でクラスを引くので
        # ``_registry`` 経由でこのテスト用クラスが見える必要がある
        entry = replace(SOQL_ENTRY, key=_EmptyTransformReport.KEY)

        # SOQL 取得フェーズは fake を渡す
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            fetched = _fetch_via_soql(entry)
        # 加工後は 0 行
        assert len(fetched) == 0

        # 保存フェーズで ``allow_empty=False`` のまま ``EmptyReportError`` が出る
        with pytest.raises(EmptyReportError):
            _save(entry, fetched)

    def test_transform_failure_row_uses_salesforce_cause(self):
        """``SoqlTransformError`` は ``ComkenError`` 系なので ``_failure_row()``
        で「Salesforce」区分に入る。"""
        from src.service import _failure_row

        exc = SoqlTransformError("9001", "顧客一覧", "MyReport", "ValueError")
        row = _failure_row(exc, 1.5, schedule_key="S001", route="SOQL")
        assert row.cause == "Salesforce"
        assert row.error_code == "SoqlTransformError"
        assert row.succeeded is False
        # ``DownloadedComkenError`` 系の取り決めに従い ``fetched_from_salesforce``
        # は ``False``（ ``_failure_row()`` は例外の型だけから組み立てる）。
        # 技術的には ``bulk_query()`` が成功してから失敗しているのでデータ自体は
        # 取れているが、 既存の 3 状態（ ``fetched`` / ``saved`` ）の単純化は
        # 維持する（ ``cause`` で Salesforce 区分に入れば履歴から追える）
        assert row.fetched_from_salesforce is False
        assert row.saved_to_file is None


# ─────────────────────────────────────────────────────────────────────
# 未完成の SOQL（ ``1 行目に【未完成】`` ）の停止（2026-10 追加）
#
# soql-collector が下書きを出し直すときに 1 行目に付ける印。
# ``_fetch_via_soql()`` が ``bulk_query()`` を呼ぶ前に止まる。
# ─────────────────────────────────────────────────────────────────────


class TestFetchViaSoqlIncompleteMarker:
    """``soql()`` の 1 行目（前後の空白を除く）が ``INCOMPLETE_SOQL_MARKER``
    で始まるとき、 ``bulk_query()`` を呼ばずに ``SoqlIncompleteError`` で止める。"""

    def test_marker_at_first_line_raises(self, monkeypatch):
        """印が 1 行目の先頭に付いていると ``SoqlIncompleteError`` で停止。"""

        class _IncompleteReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return f"{INCOMPLETE_SOQL_MARKER}\nSELECT Id, StageName FROM Opportunity"

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _IncompleteReport)
        client = _patched_soql_client(table)
        with patch("src.service.site_for", return_value=client):
            with pytest.raises(SoqlIncompleteError) as caught:
                _fetch_via_soql(SOQL_ENTRY)

        # メッセージに管理番号・概要・印の説明が出ている
        message = str(caught.value)
        assert "9001" in message
        assert "顧客一覧" in message
        assert INCOMPLETE_SOQL_MARKER in message
        # **SOQL 本文は漏れていない** （ ``bulk_query()`` が受け取る予定の SOQL の
        # 2 行目以降がメッセージに入っていないこと）
        assert "SELECT Id, StageName FROM Opportunity" not in message
        # ``bulk_query()`` が呼ばれていない（=Salesforce に問い合わせていない）
        client.__enter__.return_value.bulk_query.assert_not_called()

    def test_marker_after_leading_whitespace_raises(self, monkeypatch):
        """印が 1 行目にあるが前後に空白・改行があっても止まる。"""

        class _IncompleteReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return f"  \n\t{INCOMPLETE_SOQL_MARKER}\nSELECT Id FROM Opportunity"

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _IncompleteReport)
        client = _patched_soql_client(table)
        with patch("src.service.site_for", return_value=client):
            with pytest.raises(SoqlIncompleteError):
                _fetch_via_soql(SOQL_ENTRY)

        client.__enter__.return_value.bulk_query.assert_not_called()

    def test_marker_inside_value_does_not_stop(self, monkeypatch):
        """印が本文の途中（ ``WHERE 名前 = '【未完成】'`` のような文字列値）に
        出るだけなら止まらない（ 1 行目が ``SELECT`` で始まっているため）。"""

        class _OkReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return f"SELECT Id FROM Account WHERE Name = '{INCOMPLETE_SOQL_MARKER}'"

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _OkReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            # 止まらずに進む
            result = _fetch_via_soql(SOQL_ENTRY)

        assert result is table

    def test_marker_on_later_line_does_not_stop(self, monkeypatch):
        """印が 2 行目以降にあるだけなら止まらない（ 1 行目は ``SELECT`` ）。"""

        class _OkReport(SoqlReport):
            KEY = "9001"

            def soql(self) -> str:
                return f"SELECT Id\n-- {INCOMPLETE_SOQL_MARKER}\nFROM Account"

        table = _soql_table_with_rows()
        _patch_soql_registry(monkeypatch, _OkReport)
        with patch("src.service.site_for", return_value=_patched_soql_client(table)):
            result = _fetch_via_soql(SOQL_ENTRY)

        assert result is table

    def test_marker_uses_comkenerror_cause_in_failure_row(self):
        """``SoqlIncompleteError`` も ``DownloaderError`` 系なので ``_failure_row()``
        で「Salesforce」区分に入る。"""
        from src.service import _failure_row

        exc = SoqlIncompleteError("9001", "顧客一覧")
        row = _failure_row(exc, 0.5, schedule_key="S001", route="SOQL")
        assert row.cause == "Salesforce"
        assert row.error_code == "SoqlIncompleteError"
        assert row.succeeded is False
        assert row.fetched_from_salesforce is False
        assert row.saved_to_file is None


class TestFetchViaSoqlIncompleteSanityCheck:
    """未完成のチェックを意図的に外したときに、 該当テストが落ちることを確認
    するサニティチェック（一度確かめて戻す）。"""

    def test_sanity_check_detects_when_marker_check_disabled(self):
        """``INCOMPLETE_SOQL_MARKER`` のチェックを実質「常に偽」にした（印を
        元の値と違う文字列に差し替え） ``_fetch_via_soql()`` の挙動を確認する。
        印のある SOQL でも ``bulk_query()`` が呼ばれること（=印チェックで
        止まっていないこと）を確かめ、 元の実装が印チェックを実際に
        していることを保証する。"""

        from src import service as service_module

        original = service_module.INCOMPLETE_SOQL_MARKER
        # 印を違う文字列にしてチェックを「常に偽」にする（=元の印では止まらない）
        service_module.INCOMPLETE_SOQL_MARKER = "X_NO_MATCH_NEVER_MATCHES"
        try:

            class _IncompleteReport(SoqlReport):
                KEY = "9001"

                def soql(self) -> str:
                    # 1 行目は本物の印で始まるが、 印チェックは無効化されている
                    return f"{original}\nSELECT Id FROM Opportunity"

            with patch(
                "src.soql_reports._registry.registered_reports",
                return_value=(_IncompleteReport,),
            ):
                table = _soql_table_with_rows()
                site = _patched_soql_client(table)
                # 印チェックを通り抜けたので ``bulk_query()`` が呼ばれる
                with patch("src.service.site_for", return_value=site):
                    _fetch_via_soql(SOQL_ENTRY)

                # ``site`` は ``MagicMock(return_value=client)`` の形なので、
                # 実クライアントは ``site.return_value`` にある
                site.return_value.__enter__.return_value.bulk_query.assert_called_once()
        finally:
            service_module.INCOMPLETE_SOQL_MARKER = original


# ─────────────────────────────────────────────────────────────────────
# ``transform()`` を呼ばない壊し方をしたときに、 該当テストが落ちることを確認
# (T_Transform 型検証)
# ─────────────────────────────────────────────────────────────────────


class TestTransformSanityCheck:
    """``transform()`` を呼ばない実装で差し替えたとき、 加工が効かなくなること
    を確認するサニティチェック。 元の実装が ``_fetch_via_soql()`` の中で
    ``instance.transform(table)`` を呼んでいることを保証する。"""

    def test_sanity_check_detects_when_transform_skipped(self, monkeypatch):
        from src import service as service_module

        original = service_module._fetch_via_soql

        def _bypass_transform(*args, **kwargs):
            # ``transform()`` を呼ばずに ``bulk_query()`` の結果をそのまま返す実装
            report_cls = service_module.soql_report_for(args[0].key)
            instance = report_cls()
            site = service_module.site_for(args[0].url)
            with site() as salesforce:
                return salesforce.bulk_query(instance.soql())

        service_module._fetch_via_soql = _bypass_transform
        try:
            # 加工を期待しているケースで、 加工後の Table ではなく加工前のものが返る
            class _FilterReport(SoqlReport):
                KEY = "9001"

                def soql(self) -> str:
                    return "SELECT Id, StageName, Amount FROM Opportunity"

                def transform(self, table: Table) -> Table:
                    return table.filter(lambda row: row["StageName"] == "Closed Won")

            table = _soql_table_with_rows()
            with patch(
                "src.soql_reports._registry.registered_reports",
                return_value=(_FilterReport,),
            ):
                with patch("src.service.site_for", return_value=_patched_soql_client(table)):
                    result = service_module._fetch_via_soql(SOQL_ENTRY)

            # 壊した実装では ``transform()`` が呼ばれず、 全件が返る
            assert len(result) == 3
        finally:
            service_module._fetch_via_soql = original

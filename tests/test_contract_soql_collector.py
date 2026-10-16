"""soql-collector（隣のプロジェクト）との契約テスト（読み手側）。

comken の ``comken.services.salesforce_downloader.contract.load_sample()``
が返す **正本の見本** を使って、 ``src.soql_reports.column_names.load_column_names_for()``
と ``src.service._save()`` が **soql-collector 側の出力と一致して動くこと**を
固定する。 見本そのものは comken 側に置いてあるので、ここでは **同じバイト
を読み出して** 比較する（手作業で同期しない）。

- ``列名の対応.csv`` の見本 → ``load_sample("列名の対応.contract.csv")``
- ``未完成のSOQL.txt`` の見本 → ``load_sample("未完成のSOQL.contract.txt")``
  （soql-collector が SOQL の下書きを出し直すときに 1 行目に付ける
  ``INCOMPLETE_MARKER_PREFIX`` の印を含む見本）

``src.service._fetch_via_soql()`` がこの印を検知して ``bulk_query()``
の **前** に ``SoqlIncompleteError`` で止めることを確かめる。

書き手側（soql-collector）のテストは別リポジトリで別途やる。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from comken.core.table import Table
from comken.services.salesforce_downloader.contract import (
    INCOMPLETE_MARKER_PREFIX,
    load_sample,
)
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import SoqlIncompleteError
from src.service.save import _save
from src.service.soql import _fetch_via_soql
from src.sheets.master import ReportEntry
from src.soql_reports import _registry
from src.soql_reports import column_names as column_names_module
from src.soql_reports.base import SoqlReport


# comken 側 ``contract_samples/`` の見本を読み込むヘルパー。
# ``load_sample()`` は ``importlib.resources`` 経由で comken パッケージ内の
# 見本を取り出すので、両プロジェクトが同じバイト列を参照する。
def _column_names_csv_sample() -> bytes:
    """``列名の対応.csv`` の見本バイト列を返す。"""
    return load_sample("列名の対応.contract.csv")


def _incomplete_soql_sample() -> bytes:
    """``未完成のSOQL.contract.txt`` の見本バイト列を返す。"""
    return load_sample("未完成のSOQL.contract.txt")


def _write_sample_csv(path: Path) -> None:
    """``列名の対応.csv`` の見本を ``path`` に書き出す（テスト用の一時ファイル）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_column_names_csv_sample())


def _sample_csv_path(tmp_path: Path) -> Path:
    """見本 CSV を ``tmp_path/列名の対応.csv`` に書き出して返す。"""
    path = Path(tmp_path) / "列名の対応.csv"
    _write_sample_csv(path)
    return path


URL = "https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"


# ─────────────────────────────────────────────────────────────────────
# ``load_column_names_for()`` の挙動 — サンプル CSV を直接 ``csv_path=`` で渡す
# ─────────────────────────────────────────────────────────────────────


class TestContractSampleLoad:
    """comken の ``load_sample()`` が見本を返し、
    ``load_column_names_for()`` が soql-collector の出力と一致して動くことを確かめる。
    """

    def test_opportunity_returns_all_three_columns(self, tmp_path: Path):
        """``Opportunity`` の SOQL 列名 ``Account.Name`` / ``Amount`` / ``Name``
        がそれぞれ ``取引先名`` / ``金額`` / ``商談名`` に対応する。"""
        path = _sample_csv_path(tmp_path)
        result = column_names_module.load_column_names_for(
            "Opportunity",
            csv_path=path,
        )
        assert result == {
            "Account.Name": "取引先名",
            "Amount": "金額",
            "Name": "商談名",
        }

    def test_opportunity_lowercase_returns_same(self, tmp_path: Path):
        """``opportunity`` （小文字）でも同じ dict が返る（主オブジェクトは
        大文字小文字を区別しない）。"""
        path = _sample_csv_path(tmp_path)
        result = column_names_module.load_column_names_for(
            "opportunity",
            csv_path=path,
        )
        assert result == {
            "Account.Name": "取引先名",
            "Amount": "金額",
            "Name": "商談名",
        }

    def test_task_returns_subject(self, tmp_path: Path):
        """``Task`` は ``Subject → 件名`` の 1 件だけが返る（ ``Opportunity`` の
        行は混ざらない）。"""
        path = _sample_csv_path(tmp_path)
        result = column_names_module.load_column_names_for(
            "Task",
            csv_path=path,
        )
        assert result == {"Subject": "件名"}

    def test_unknown_main_object_returns_empty_with_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """サンプル CSV に無い主オブジェクト（ ``Contact`` ）を引くと
        空 dict ＋ WARNING が出る（取得は止めない）。"""
        path = _sample_csv_path(tmp_path)
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = column_names_module.load_column_names_for(
                "Contact",
                csv_path=path,
            )

        assert result == {}
        # WARNING に主オブジェクト名と「行がありません」が出ている
        assert "Contact" in caplog.text
        assert "行がありません" in caplog.text


# ─────────────────────────────────────────────────────────────────────
# サービス層 ``_save()`` でサンプル CSV を使い、 保存 CSV の見出しが置き換わるか
# ─────────────────────────────────────────────────────────────────────


class _ContractSampleReport(SoqlReport):
    """``SELECT Id, Name, Amount, Account.Name FROM Opportunity`` のダミー。"""

    KEY = "9301"
    # ``COLUMN_NAMES`` は空にして、 ``列名の対応.csv`` だけから拾う形に統一
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        return "SELECT Id, Name, Amount, Account.Name FROM Opportunity"


@pytest.fixture
def _patched_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_registry.registered_reports()`` がテスト用の1件だけを返すようにする。"""
    monkeypatch.setattr(_registry, "registered_reports", lambda: (_ContractSampleReport,))


@pytest.fixture
def _column_csv_path_from_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``paths.COLUMN_NAMES_CSV_PATH`` と ``column_names.COLUMN_NAMES_CSV_PATH`` を
    comken の見本 ``列名の対応.contract.csv`` から作った実ファイルへ向ける。

    ``column_names.py`` 側はモジュール変数を import 時に取り込んでいるので、
    両方を monkeypatch して ``load_column_names_for()`` が既定パスでなく
    サンプルを読むようにする。
    """
    path = _sample_csv_path(tmp_path)
    monkeypatch.setattr(paths_module, "COLUMN_NAMES_CSV_PATH", path)
    monkeypatch.setattr(column_names_module, "COLUMN_NAMES_CSV_PATH", path)
    return path


def _patched_output_path(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """``paths.output_path()`` を ``folder`` 配下の固定パスに差し替える。"""

    def fake_output_path(entry, schedule_run_time=None, *, now=None):
        return folder / "9301_20240101_0900.csv"

    monkeypatch.setattr(paths_module, "output_path", fake_output_path)


@pytest.fixture
def _output_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``_save()`` の出力先を ``tmp_path`` 配下へ向ける。"""
    folder = tmp_path / "out"
    folder.mkdir()
    _patched_output_path(monkeypatch, folder)
    return folder


class TestContractSampleSave:
    """``_save()`` の保存 CSV の見出しがサンプル CSV の表示名に置き換わる検証。

    既存の ``tests/test_service_soql_fetch.py::TestSaveAppliesFileColumnNames``
    と同じやり方（ ``COLUMN_NAMES_CSV_PATH`` を差し替えて ``_save()`` を呼ぶ）。
    ここでは **comken の契約の見本ファイルそのもの**を使って soql-collector の
    出力と一致しているかを確かめる。
    """

    def test_save_headers_match_contract_sample(
        self,
        _patched_registry: None,
        _output_folder: Path,
        _column_csv_path_from_sample: Path,
    ):
        """サンプルを ``COLUMN_NAMES_CSV_PATH`` に使い、 SOQL の結果 ``Id, Name,
        Amount, Account.Name`` を ``_save()`` に通すと、 保存 CSV の見出しが
        ``Id, 商談名, 金額, 取引先名`` になる（ ``Opportunity`` 行だけが残る）。"""
        entry = ReportEntry(
            key=_ContractSampleReport.KEY,
            summary="契約テスト",
            url=URL,
            group="営業本部",
            assignee="山田",
            enabled=True,
            allow_empty=False,
            use_soql=True,
        )
        table = Table(
            ["Id", "Name", "Amount", "Account.Name"],
            [
                {
                    "Id": "001",
                    "Name": "山田",
                    "Amount": "100",
                    "Account.Name": "取引先A",
                },
            ],
        )

        saved = _save(entry, table)

        with CSV(saved, read_only=True) as csv_file:
            result = csv_file.read()
        # ``Opportunity`` の行だけが残り、 表示名が ``Id`` 以外で見出しになっている
        assert result.columns == ["Id", "商談名", "金額", "取引先名"]
        assert result.to_rows() == [
            {
                "Id": "001",
                "商談名": "山田",
                "金額": "100",
                "取引先名": "取引先A",
            },
        ]


# ─────────────────────────────────────────────────────────────────────
# 未完成の SOQL（ comken の ``未完成のSOQL.contract.txt`` ）
# soql-collector が下書きを出し直すときに 1 行目に付ける
# ``INCOMPLETE_MARKER_PREFIX`` の印を含む見本。 ``_fetch_via_soql()`` が
# ``bulk_query()`` を呼ぶ前に止めることを検証する。
# ─────────────────────────────────────────────────────────────────────


# サンプル TXT は comken の ``load_sample()`` からバイト単位で読む
# （ ``Path(__file__).resolve().parent / "contract" / ...`` ではない）。


class TestContractIncompleteSoql:
    """comken の ``load_sample()`` が返す ``未完成のSOQL.contract.txt`` を使って
    ``_fetch_via_soql()`` が印のある SOQL を実行前に止めることを確かめる。
    """

    def test_first_line_is_incomplete_marker(self):
        """見本の 1 行目が ``INCOMPLETE_MARKER_PREFIX`` で始まる（前後の空白を除く）。"""
        soql_text = _incomplete_soql_sample().decode("utf-8")
        first_line = soql_text.lstrip().splitlines()[0]
        assert first_line.startswith(INCOMPLETE_MARKER_PREFIX)

    def test_incomplete_soql_stops_before_bulk_query(self):
        """``_fetch_via_soql()`` は ``bulk_query()`` を呼ぶ前に ``SoqlIncompleteError``
        で止める。 Salesforce には問い合わせない（= ``bulk_query()`` が呼ばれない）。"""
        soql_text = _incomplete_soql_sample().decode("utf-8")

        class _IncompleteContractReport(SoqlReport):
            KEY = "9301"

            def soql(self) -> str:
                return soql_text

        contract_entry = ReportEntry(
            key=_IncompleteContractReport.KEY,
            summary="契約テスト",
            url=URL,
            group="営業本部",
            assignee="山田",
            enabled=True,
            allow_empty=False,
            use_soql=True,
        )

        with (
            patch(
                "src.soql_reports._registry.registered_reports",
                return_value=(_IncompleteContractReport,),
            ),
            patch("src.service.soql.site_for") as site_for,
        ):
            with pytest.raises(SoqlIncompleteError) as caught:
                _fetch_via_soql(contract_entry)

        # ``site_for()`` 自体が呼ばれていない（=Salesforce に接続していない）
        site_for.assert_not_called()
        # メッセージに管理番号・概要・印の説明が出ている
        message = str(caught.value)
        assert "9301" in message
        assert "契約テスト" in message
        # **SOQL 本文は漏れていない** （ 2 行目以降の SOQL がメッセージに入っていない）
        assert "SELECT" not in message
        assert "Opportunity" not in message

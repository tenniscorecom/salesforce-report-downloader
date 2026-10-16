"""soql-collector（隣のプロジェクト）との契約テスト（読み手側）。

``tests/contract/列名の対応.contract.csv`` （soql-collector が出す
``列名の対応.csv`` と同じ見本）を使って、 ``src.soql_reports.column_names.load_column_names_for()``
と ``src.service._save()`` が **soql-collector 側の出力と一致して動くこと**を
固定する。 soql-collector 側の ``tests/contract/列名の対応.contract.csv`` と
バイト単位で同じファイルをこのリポジトリに置き、 両者が食い違っていないかを
突き合わせる。

変えたくなったら両リポジトリの ``tests/contract/列名の対応.contract.csv`` を
**同時に** 変える（``tests/contract/README.md`` 参照）。 書き手側
（soql-collector）のテストは別リポジトリで別途やる。

``tests/contract/未完成のSOQL.contract.txt`` は soql-collector が SOQL の
下書きを出し直すときに 1 行目に付ける ``【未完成】`` の印を含む見本。
``src.service._fetch_via_soql()`` がこの印を検知して ``bulk_query()``
の **前** に ``SoqlIncompleteError`` で止めることを確かめる。 soql-collector
側にも同じファイルを置き、 両リポジトリで同じ見本を参照する。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from comken.core.table import Table
from comken.toolbox.csv import CSV

from src import paths as paths_module
from src.exceptions import SoqlIncompleteError
from src.service.save import _save
from src.service.soql import _fetch_via_soql
from src.sheets.master import ReportEntry
from src.soql_reports import _registry
from src.soql_reports import column_names as column_names_module
from src.soql_reports.base import SoqlReport

# サンプル CSV の絶対パス（このファイル自体）。 テストは ``csv_path=CONTRACT_CSV_PATH``
# を ``load_column_names_for()`` に渡して、 モジュール既定の
# ``paths.COLUMN_NAMES_CSV_PATH`` を経由せず直接読む。
CONTRACT_CSV_PATH = Path(__file__).resolve().parent / "contract" / "列名の対応.contract.csv"

URL = "https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"


# ─────────────────────────────────────────────────────────────────────
# ``load_column_names_for()`` の挙動 — サンプル CSV を直接 ``csv_path=`` で渡す
# ─────────────────────────────────────────────────────────────────────


class TestContractSampleLoad:
    """``tests/contract/列名の対応.contract.csv`` を使って
    ``load_column_names_for()`` が soql-collector の出力と一致して動くことを確かめる。
    """

    def test_opportunity_returns_all_three_columns(self):
        """``Opportunity`` の SOQL 列名 ``Account.Name`` / ``Amount`` / ``Name``
        がそれぞれ ``取引先名`` / ``金額`` / ``商談名`` に対応する。"""
        result = column_names_module.load_column_names_for(
            "Opportunity",
            csv_path=CONTRACT_CSV_PATH,
        )
        assert result == {
            "Account.Name": "取引先名",
            "Amount": "金額",
            "Name": "商談名",
        }

    def test_opportunity_lowercase_returns_same(self):
        """``opportunity`` （小文字）でも同じ dict が返る（主オブジェクトは
        大文字小文字を区別しない）。"""
        result = column_names_module.load_column_names_for(
            "opportunity",
            csv_path=CONTRACT_CSV_PATH,
        )
        assert result == {
            "Account.Name": "取引先名",
            "Amount": "金額",
            "Name": "商談名",
        }

    def test_task_returns_subject(self):
        """``Task`` は ``Subject → 件名`` の 1 件だけが返る（ ``Opportunity`` の
        行は混ざらない）。"""
        result = column_names_module.load_column_names_for(
            "Task",
            csv_path=CONTRACT_CSV_PATH,
        )
        assert result == {"Subject": "件名"}

    def test_unknown_main_object_returns_empty_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """サンプル CSV に無い主オブジェクト（ ``Contact`` ）を引くと
        空 dict ＋ WARNING が出る（取得は止めない）。"""
        with caplog.at_level(logging.WARNING, logger="src.soql_reports.column_names"):
            result = column_names_module.load_column_names_for(
                "Contact",
                csv_path=CONTRACT_CSV_PATH,
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
def _patched_registry(monkeypatch):
    """``_registry.registered_reports()`` がテスト用の1件だけを返すようにする。"""
    monkeypatch.setattr(_registry, "registered_reports", lambda: (_ContractSampleReport,))


@pytest.fixture
def _column_csv_path_from_sample(monkeypatch) -> Path:
    """``paths.COLUMN_NAMES_CSV_PATH`` と ``column_names.COLUMN_NAMES_CSV_PATH`` を
    ``tests/contract/列名の対応.contract.csv`` の実ファイルへ向ける。

    ``column_names.py`` 側はモジュール変数を import 時に取り込んでいるので、
    両方を monkeypatch して ``load_column_names_for()`` が既定パスでなく
    サンプルを読むようにする。
    """
    monkeypatch.setattr(paths_module, "COLUMN_NAMES_CSV_PATH", CONTRACT_CSV_PATH)
    monkeypatch.setattr(column_names_module, "COLUMN_NAMES_CSV_PATH", CONTRACT_CSV_PATH)
    return CONTRACT_CSV_PATH


def _patched_output_path(monkeypatch, folder: Path) -> None:
    """``paths.output_path()`` を ``folder`` 配下の固定パスに差し替える。"""

    def fake_output_path(entry, schedule_run_time=None, *, now=None):
        return folder / "9301_20240101_0900.csv"

    monkeypatch.setattr(paths_module, "output_path", fake_output_path)


@pytest.fixture
def _output_folder(tmp_path: Path, monkeypatch) -> Path:
    """``_save()`` の出力先を ``tmp_path`` 配下へ向ける。"""
    folder = tmp_path / "out"
    folder.mkdir()
    _patched_output_path(monkeypatch, folder)
    return folder


class TestContractSampleSave:
    """``_save()`` の保存 CSV の見出しがサンプル CSV の表示名に置き換わる検証。

    既存の ``tests/test_service_soql_fetch.py::TestSaveAppliesFileColumnNames``
    と同じやり方（ ``COLUMN_NAMES_CSV_PATH`` を差し替えて ``_save()`` を呼ぶ）。
    ここでは **契約の見本ファイルそのもの**を使って soql-collector の出力と
    一致しているかを確かめる。
    """

    def test_save_headers_match_contract_sample(
        self,
        _patched_registry,
        _output_folder,
        _column_csv_path_from_sample,
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
# 隣の soql-collector リポジトリの ``tests/contract/列名の対応.contract.csv``
# とバイト単位で同じかを突き合わせる
# ─────────────────────────────────────────────────────────────────────


def _soql_collector_contract_path() -> Path | None:
    """soql-collector リポジトリの ``tests/contract/列名の対応.contract.csv`` のパスを返す。

    環境変数 ``SOQL_COLLECTOR_DIR`` があればそれを最優先、無ければこの
    リポジトリの親フォルダの ``soql-collector`` を探す。 どちらにも該当
    ファイルが無ければ ``None``（テストは skip する）。
    """
    env_dir = os.environ.get("SOQL_COLLECTOR_DIR")
    if env_dir:
        candidate = Path(env_dir) / "tests" / "contract" / "列名の対応.contract.csv"
        return candidate if candidate.exists() else None
    # このリポジトリと同じフォルダに置いた ``soql-collector`` を見る
    # （例: ``F:\dev\Salesforceレポートダウンローダー`` と ``F:\dev\soql-collector``）。
    # 他の場所に置いてあるときは環境変数 ``SOQL_COLLECTOR_DIR`` で上書きできる。
    sibling = Path(__file__).resolve().parents[2] / "soql-collector"
    candidate = sibling / "tests" / "contract" / "列名の対応.contract.csv"
    return candidate if candidate.exists() else None


class TestContractSamplesMatchAcrossRepos:
    """このリポジトリと soql-collector 側のサンプル CSV がバイト単位で同じか。"""

    def test_contract_samples_match_byte_for_byte(self):
        """soql-collector 側に ``tests/contract/列名の対応.contract.csv`` が
        有れば、 このリポジトリの見本とバイト単位で一致する。 無ければ skip。"""
        other = _soql_collector_contract_path()
        if other is None:
            pytest.skip(
                "soql-collector 側の tests/contract/列名の対応.contract.csv が見つかりません。"
                "環境変数 SOQL_COLLECTOR_DIR で soql-collector リポジトリの"
                "ルートフォルダを指すと検証できます。",
            )

        mine = CONTRACT_CSV_PATH.read_bytes()
        theirs = other.read_bytes()
        assert mine == theirs, (
            f"このリポジトリのサンプル ({CONTRACT_CSV_PATH}) と "
            f"soql-collector 側のサンプル ({other}) がバイト単位で一致しません。"
            "両方の tests/contract/列名の対応.contract.csv を同じ内容にしてください。"
        )


# ─────────────────────────────────────────────────────────────────────
# 未完成の SOQL（ ``tests/contract/未完成のSOQL.contract.txt`` ）
# soql-collector が下書きを出し直すときに 1 行目に付ける ``【未完成】`` の印
# を含む見本。 ``_fetch_via_soql()`` が ``bulk_query()`` を呼ぶ前に止める
# ことを検証する。
# ─────────────────────────────────────────────────────────────────────


# サンプル TXT の絶対パス。 テストはこのファイルから SOQL テキストを読み、
# ``SoqlReport.soql()`` として直接 ``_fetch_via_soql()`` に渡す。
CONTRACT_INCOMPLETE_SOQL_PATH = (
    Path(__file__).resolve().parent / "contract" / "未完成のSOQL.contract.txt"
)


def _soql_collector_incomplete_path() -> Path | None:
    """soql-collector リポジトリの ``tests/contract/未完成のSOQL.contract.txt`` のパスを返す。

    環境変数 ``SOQL_COLLECTOR_DIR`` があればそれを最優先、無ければこの
    リポジトリの親フォルダの ``soql-collector`` を探す。 どちらにも該当
    ファイルが無ければ ``None``（テストは skip する）。
    """
    env_dir = os.environ.get("SOQL_COLLECTOR_DIR")
    if env_dir:
        candidate = Path(env_dir) / "tests" / "contract" / "未完成のSOQL.contract.txt"
        return candidate if candidate.exists() else None
    sibling = Path(__file__).resolve().parents[2] / "soql-collector"
    candidate = sibling / "tests" / "contract" / "未完成のSOQL.contract.txt"
    return candidate if candidate.exists() else None


class TestContractIncompleteSoql:
    """``tests/contract/未完成のSOQL.contract.txt`` を使って
    ``_fetch_via_soql()`` が印のある SOQL を実行前に止めることを確かめる。
    """

    def test_first_line_is_incomplete_marker(self):
        """見本の 1 行目が ``【未完成】`` で始まる（前後の空白を除く）。"""
        soql_text = CONTRACT_INCOMPLETE_SOQL_PATH.read_text(encoding="utf-8")
        first_line = soql_text.lstrip().splitlines()[0]
        assert first_line.startswith("【未完成】")

    def test_incomplete_soql_stops_before_bulk_query(self):
        """``_fetch_via_soql()`` は ``bulk_query()`` を呼ぶ前に ``SoqlIncompleteError``
        で止める。 Salesforce には問い合わせない（= ``bulk_query()`` が呼ばれない）。"""
        soql_text = CONTRACT_INCOMPLETE_SOQL_PATH.read_text(encoding="utf-8")

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

    def test_contract_samples_match_byte_for_byte(self):
        """soql-collector 側に ``tests/contract/未完成のSOQL.contract.txt`` が
        有れば、 このリポジトリの見本とバイト単位で一致する。 無ければ skip。"""
        other = _soql_collector_incomplete_path()
        if other is None:
            pytest.skip(
                "soql-collector 側の tests/contract/未完成のSOQL.contract.txt が見つかりません。"
                "環境変数 SOQL_COLLECTOR_DIR で soql-collector リポジトリの"
                "ルートフォルダを指すと検証できます。",
            )

        mine = CONTRACT_INCOMPLETE_SOQL_PATH.read_bytes()
        theirs = other.read_bytes()
        assert mine == theirs, (
            f"このリポジトリのサンプル ({CONTRACT_INCOMPLETE_SOQL_PATH}) と "
            f"soql-collector 側のサンプル ({other}) がバイト単位で一致しません。"
            "両方の tests/contract/未完成のSOQL.contract.txt を同じ内容にしてください。"
        )

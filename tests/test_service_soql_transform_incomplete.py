"""``SoqlReport.transform()`` と未完成 SOQL の ``download_scheduled()`` 統合テスト。

``src.service.fetch_via_soql()`` に ``transform()`` の呼び出しと未完成印の
検知を入れた変更が、 2 本以上のレポートを含む 1 実行で

- ``transform()`` の例外が **そのレポートだけの失敗**になり、 次のレポートの
  取得は続いて終わる
- 未完成印のある SOQL は **実行前** に止まり、 ファイルが残らず、 履歴に
  失敗 1 行が残る

ことを確かめるためのもの。 単体テスト（ ``test_service_soql_fetch.py`` ）が
``fetch_via_soql()`` 単体の挙動を見ているのに対し、 ここでは管理表・
スケジュール・履歴・保存先を含めたフル実行を確認する。
"""

import datetime as dt
import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.toolbox.csv import CSV
from comken.toolbox.excel import Excel

from src.exceptions import ScheduledDownloadFailedError, SoqlIncompleteError, SoqlTransformError
from src.service import attempt as attempt_module
from src.sheets.master import load_master
from src.soql_reports import _registry
from src.soql_reports.base import SoqlReport
from tests.test_service import fake_salesforce

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"
URL_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000FGHIJ/view"


def _make_master_with_two_reports(
    tmp_path: Path,
    *,
    base_path: Path,
    row_b: list,
) -> Path:
    """``1001``（常に有効）と ``1002``（ ``row_b`` で指定） の 2 件の管理表を作る。"""
    master_path = tmp_path / "レポート管理表.xlsx"

    HEADERS = [
        "ID",
        "グループ",
        "担当者",
        "概要",
        "Salesforce URL",
        "有効",
        "0件あり",
        "2000件超",
        "SOQL",
        "上書き",
    ]
    GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]
    schedule_headers = [
        "スケジュールキー",
        "レポートキー",
        "取得頻度",
        "取得開始時刻",
        "取得時刻",
        "曜日",
        "日付",
        "祝日対応",
        "有効",
    ]

    row_1001 = [
        "1001",
        "テストグループ",
        "山田",
        "レポートA",
        URL_A,
        "○",
        "",
        "",
        "",
        "",
    ]
    table_rows = [
        dict(zip(HEADERS, row_1001, strict=True)),
        dict(zip(HEADERS, row_b, strict=True)),
    ]
    schedule_rows = [
        dict(
            zip(
                schedule_headers,
                ["S001", "1001", "毎日", "", "", "", "", "取得しない", "○"],
                strict=True,
            ),
        ),
        dict(
            zip(
                schedule_headers,
                ["S002", "1002", "毎日", "", "", "", "", "取得しない", "○"],
                strict=True,
            ),
        ),
    ]
    settings_rows_dict = [
        {"グループ": "テストグループ", "ベースURL": str(base_path)},
    ]

    with Excel(master_path) as book:
        book.create_data_sheet("管理表").create_table("管理表", Table(HEADERS, table_rows))
        book.create_data_sheet("スケジュール").create_table(
            "スケジュール", Table(schedule_headers, schedule_rows)
        )
        book.create_data_sheet("設定").create_table(
            "設定", Table(GROUP_SETTINGS_HEADERS, settings_rows_dict)
        )

    return master_path


@pytest.fixture
def paths_two_reports(tmp_path, monkeypatch):
    """``1001`` と ``1002`` の 2 件が有効な管理表・スケジュール・設定を作る。
    ``1002`` の中身は呼び出し側で ``replace`` する。"""
    import src.paths as _paths_inner

    base_path = tmp_path / "ベース"
    base_path.mkdir()
    history_path = tmp_path / "ダウンロード履歴.csv"
    # 1002 は無効のダミー（テストで replace して有効化する）
    master = _make_master_with_two_reports(
        tmp_path,
        base_path=base_path,
        row_b=[
            "1002",
            "テストグループ",
            "山田",
            "レポートB",
            URL_B,
            "×",
            "",
            "",
            "",
            "",
        ],
    )

    monkeypatch.setattr(_paths_inner, "MASTER_PATH", master)
    monkeypatch.setattr(
        "comken.services.salesforce_downloader.paths.HISTORY_PATH",
        history_path,
    )
    _paths_inner.reset_group_settings_cache()

    return {
        "master_path": master,
        "history_path": history_path,
        "base_path": base_path,
    }


def _read_history(history_path: Path) -> list[dict[str, str]]:
    """履歴 CSV を ``list[dict]`` で読む。"""
    with CSV(history_path) as csv_file:
        return [dict(row) for row in csv_file.read().to_rows()]


def _enable_1002(monkeypatch, *, url: str, use_soql: bool) -> None:
    """``load_master()`` を ``1002`` を有効化した ``dict`` を返す関数で差し替える。"""
    import src.paths as paths_module

    entries = load_master(paths_module.MASTER_PATH)
    entry_1002 = replace(
        entries["1002"],
        enabled=True,
        url=url,
        use_soql=use_soql,
    )
    all_entries = {**entries, "1002": entry_1002}
    monkeypatch.setattr(attempt_module, "load_master", lambda _master_path: all_entries)


def _patch_clock(monkeypatch) -> None:
    """``clock_now()`` を平日 9:00 に固定（スケジュールの ``is_due()`` が True になる）。"""
    import src.paths as _paths_inner

    fixed_now = dt.datetime(2026, 9, 18, 9, 0)  # noqa: DTZ001
    # ``clock_now()`` の実体は ``attempt._download_scheduled_locked()`` から
    # ``comken.core.dates.now as clock_now`` 経由で束縛されているので、
    # ``attempt`` 側を差し替える（``src.service`` モジュールには無い）
    monkeypatch.setattr(attempt_module, "clock_now", lambda: fixed_now)
    monkeypatch.setattr(_paths_inner, "clock_now", lambda: fixed_now)


# ─────────────────────────────────────────────────────────────────────
# 加工（ ``transform()`` ） の例外で 1 件失敗しても、 次レポートの取得が続く
# ─────────────────────────────────────────────────────────────────────


class _TransformRaises(SoqlReport):
    """``transform()`` が ``ValueError`` を投げるテスト用 ``SoqlReport``。"""

    KEY = "1002"

    def soql(self) -> str:
        return "SELECT Id, StageName, Amount FROM Opportunity"

    def transform(self, table: Table) -> Table:  # type: ignore[override]
        raise ValueError("bad row in transform")


def _fake_salesforce_bulk_query_returning_table(table: Table) -> MagicMock:
    """``bulk_query()`` が ``table`` を返し、 件数比較の ``request()`` が
    ``RowCount`` 無し（ WARNING で握りつぶされて続行する）のを返す偽 Salesforce。"""
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


class TestDownloadScheduledTransformFailure:
    """``transform()`` の例外で 1 件失敗しても次のレポートの取得が続く。"""

    def test_transform_failure_does_not_stop_next_report(self, monkeypatch, paths_two_reports):
        """``1002`` の ``transform()`` が例外を投げても、 ``1001`` の取得と保存は
        影響を受けず、 ``1002`` は履歴に失敗 1 行が残り、 最終的に
        ``ScheduledDownloadFailedError`` で ``fetch_via_soql()`` から見た ``1002``
        の例外を ``__cause__`` に繋いで送出される。

        ``1002`` は SOQL 経由（ ``use_soql=True`` ）にして ``fetch_via_soql()`` を
        経由させ、 ``transform()`` の例外を ``SoqlTransformError`` に包ませる。"""
        _enable_1002(monkeypatch, url=URL_B, use_soql=True)
        _patch_clock(monkeypatch)

        # ``_registry.registered_reports()`` が ``_TransformRaises`` だけを返す
        monkeypatch.setattr(_registry, "registered_reports", lambda: (_TransformRaises,))

        soql_table = Table(["Id"], [{"Id": "001"}])

        # ``fake_salesforce`` （Report API の戻り値） と
        # ``_fake_salesforce_bulk_query_returning_table`` （SOQL の戻り値） を
        # 切り替える
        def _site_for(url):
            # ``use_soql=True`` のときは ``bulk_query`` を生やす偽クライアント、
            # それ以外は ``fake_salesforce`` の Report API 経路
            if "1002" in url or "FGHIJ" in url:
                return _fake_salesforce_bulk_query_returning_table(soql_table)
            return fake_salesforce()

        with (
            patch("src.service.soql.site_for", side_effect=_site_for),
            patch("src.service.api.site_for", side_effect=_site_for),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            attempt_module.download_scheduled()

        # ``__cause__`` に ``SoqlTransformError`` が繋がっている
        assert isinstance(caught.value.__cause__, SoqlTransformError)
        # 履歴は 2 行（ ``1001`` 成功 / ``1002`` 失敗）
        history_rows = _read_history(paths_two_reports["history_path"])
        assert len(history_rows) == 2

        # ``1001`` は成功
        row_1001 = history_rows[0]
        assert row_1001["管理番号"] == "1001"
        assert row_1001["成否"] == "成功"
        assert row_1001["保存結果"] == "成功"

        # ``1002`` は失敗（ SoqlTransformError ）
        row_1002 = history_rows[1]
        assert row_1002["管理番号"] == "1002"
        assert row_1002["成否"] == "失敗"
        assert row_1002["エラーコード"] == "SoqlTransformError"
        assert row_1002["原因区分"] == "Salesforce"

        # ``1001`` のファイルは残っている
        summary_dir = paths_two_reports["base_path"] / "レポートA"
        assert summary_dir.is_dir()
        assert any(summary_dir.glob("*.csv"))

        # ``1002`` のフォルダは作られない（ ``save_report()`` に到達していないため）
        summary_dir_b = paths_two_reports["base_path"] / "レポートB"
        assert not summary_dir_b.exists()

    def test_transform_failure_message_does_not_leak_row_value(
        self, monkeypatch, paths_two_reports, caplog
    ):
        """``transform()`` が ``ValueError`` を ``f"bad row: {marker}"`` で投げても
        履歴の「エラー内容」列と ``ScheduledDownloadFailedError`` のメッセージに
        行の値が漏れない（マーカーで検証）。"""

        marker = "HISTORY_TRANSFORM_MARKER_in_error"

        class _TransformRaisesMarker(SoqlReport):
            KEY = "1002"

            def soql(self) -> str:
                return "SELECT Id, StageName, Amount FROM Opportunity"

            def transform(self, table: Table) -> Table:  # type: ignore[override]
                raise ValueError(f"bad row: {marker}")

        _enable_1002(monkeypatch, url=URL_B, use_soql=True)
        _patch_clock(monkeypatch)
        monkeypatch.setattr(_registry, "registered_reports", lambda: (_TransformRaisesMarker,))

        soql_table = Table(["Id"], [{"Id": "001"}])

        def _site_for(url):
            if "FGHIJ" in url:
                return _fake_salesforce_bulk_query_returning_table(soql_table)
            return fake_salesforce()

        with (
            caplog.at_level(logging.DEBUG, logger="src.service"),
            patch("src.service.soql.site_for", side_effect=_site_for),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            attempt_module.download_scheduled()

        # 送出された ``ScheduledDownloadFailedError`` のメッセージにマーカーが漏れていない
        assert marker not in str(caught.value)
        # ``__cause__`` （ ``SoqlTransformError`` ） のメッセージにも漏れていない
        assert marker not in str(caught.value.__cause__)

        # 履歴の「エラー内容」列にもマーカーが漏れていない
        history_rows = _read_history(paths_two_reports["history_path"])
        error_row = next(r for r in history_rows if r["管理番号"] == "1002")
        assert marker not in error_row["エラー内容"]

        # ``src.service`` 由来の全ログ（ DEBUG 含む） にもマーカーが漏れていない
        service_records = [r for r in caplog.records if r.name == "src.service"]
        for record in service_records:
            assert marker not in record.getMessage(), (
                f"行の値が漏れています: level={record.levelname}, message={record.getMessage()!r}"
            )


# ─────────────────────────────────────────────────────────────────────
# 未完成印のある SOQL は実行前に止まり、 ファイルが残らない
# ─────────────────────────────────────────────────────────────────────


class _IncompleteSoqlReport(SoqlReport):
    """1 行目が ``【未完成】`` で始まる SOQL を返すテスト用 ``SoqlReport``。"""

    KEY = "1002"

    def soql(self) -> str:
        from src.service.soql import INCOMPLETE_SOQL_MARKER

        return f"{INCOMPLETE_SOQL_MARKER}\nSELECT Id, StageName FROM Opportunity"


class TestDownloadScheduledIncompleteSoql:
    """未完成印のある SOQL は ``bulk_query()`` 呼び出し前に止まり、
    ファイルが残らない（= 1002 のフォルダが作られない）。"""

    def test_incomplete_soql_stops_before_save(self, monkeypatch, paths_two_reports):
        """``1002`` の SOQL が未完成印で始まっていれば ``SoqlIncompleteError`` で
        失敗扱い。 ``bulk_query()`` は呼ばれず、 ファイルは作られない。
        ``1001`` は普通に取れる。"""
        _enable_1002(monkeypatch, url=URL_B, use_soql=True)
        _patch_clock(monkeypatch)
        monkeypatch.setattr(_registry, "registered_reports", lambda: (_IncompleteSoqlReport,))

        soql_table = Table(["Id"], [{"Id": "001"}])

        def _site_for(url):
            # 未完成印で止まるので、 ``bulk_query()`` は呼ばれない想定
            if "FGHIJ" in url:
                client = MagicMock()
                client.__enter__.return_value.bulk_query.return_value = soql_table
                return MagicMock(return_value=client)
            return fake_salesforce()

        with (
            patch("src.service.soql.site_for", side_effect=_site_for),
            patch("src.service.api.site_for", side_effect=_site_for),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            attempt_module.download_scheduled()

        # ``__cause__`` が ``SoqlIncompleteError``
        assert isinstance(caught.value.__cause__, SoqlIncompleteError)
        # 履歴 2 行（ 1001 成功 / 1002 失敗 ）
        history_rows = _read_history(paths_two_reports["history_path"])
        assert len(history_rows) == 2

        row_1002 = history_rows[1]
        assert row_1002["管理番号"] == "1002"
        assert row_1002["成否"] == "失敗"
        assert row_1002["エラーコード"] == "SoqlIncompleteError"
        assert row_1002["原因区分"] == "Salesforce"

        # ``1001`` のファイルは残っている
        summary_dir = paths_two_reports["base_path"] / "レポートA"
        assert any(summary_dir.glob("*.csv"))

        # ``1002`` のフォルダは作られない（ save_report() に到達していない）
        summary_dir_b = paths_two_reports["base_path"] / "レポートB"
        assert not summary_dir_b.exists()

    def test_incomplete_soql_message_does_not_leak_soql_body(
        self, monkeypatch, paths_two_reports, caplog
    ):
        """未完成印の ``SoqlIncompleteError`` のメッセージに SOQL 本文
        （ 2 行目以降 ）が漏れない（マーカーで確認）。"""

        marker = "INCOMPLETE_SOQL_BODY_MARKER_in_message"

        class _IncompleteWithMarker(SoqlReport):
            KEY = "1002"

            def soql(self) -> str:
                from src.service.soql import INCOMPLETE_SOQL_MARKER

                # 1 行目は印、 2 行目に目印の文字列を入れる
                return f"{INCOMPLETE_SOQL_MARKER}\nSELECT {marker} FROM Opportunity"

        _enable_1002(monkeypatch, url=URL_B, use_soql=True)
        _patch_clock(monkeypatch)
        monkeypatch.setattr(_registry, "registered_reports", lambda: (_IncompleteWithMarker,))

        soql_table = Table(["Id"], [{"Id": "001"}])

        def _site_for(url):
            if "FGHIJ" in url:
                client = MagicMock()
                client.__enter__.return_value.bulk_query.return_value = soql_table
                return MagicMock(return_value=client)
            return fake_salesforce()

        with (
            caplog.at_level(logging.DEBUG, logger="src.service"),
            patch("src.service.soql.site_for", side_effect=_site_for),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            attempt_module.download_scheduled()

        # メッセージに SOQL 本文のマーカーが漏れていない
        cause = caught.value.__cause__
        assert marker not in str(cause)
        assert marker not in str(caught.value)

        # 履歴の「エラー内容」列にもマーカーが漏れていない
        history_rows = _read_history(paths_two_reports["history_path"])
        error_row = next(r for r in history_rows if r["管理番号"] == "1002")
        assert marker not in error_row["エラー内容"]

        # ``src.service`` 由来の WARNING 以上のログにマーカーが漏れていない
        # DEBUG の ``"SOQL: ..."`` は運用者が見る想定で本文が出るので除外する
        # （既存の ``test_save_warning_does_not_leak_soql_body`` と同じポリシー）
        service_records = [r for r in caplog.records if r.name == "src.service"]
        for record in service_records:
            if record.levelno >= logging.WARNING:
                assert marker not in record.getMessage(), (
                    f"SOQL 本文が漏れています: level={record.levelname}, "
                    f"message={record.getMessage()!r}"
                )

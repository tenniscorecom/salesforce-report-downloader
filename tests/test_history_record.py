"""``src.history.record()`` が履歴に書き込む 1 行のバイト列が、
comken 側に移す前と同じ形式・順序・空欄の扱いになることを確かめる。

履歴の列構成・順序・空欄の扱いは ``comken.services.salesforce_downloader.history``
の ``COLUMNS`` が正。``record()`` は ``COLUMNS`` の各列をキーにした ``Mapping``
を組み立てて ``append_history()`` に渡す薄いラッパーなので、
**このテストが落ちたときは出力に互換性のない変更が混じっている**（例:
列順の入れ替え、空欄を空文字列以外の値に丸める、UTF-8 BOM 以外の
エンコーディングで書く、など）。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from comken.services.salesforce_downloader.history import COLUMNS, FAILURE, SUCCESS, HistoryRow

from src.history import record
from src.sheets.master import ReportEntry


def _stage(value: bool | None) -> str:
    """旧実装の ``_stage()`` と同じ変換。"""
    if value is None:
        return ""
    return SUCCESS if value else FAILURE


def _expected_row_bytes(
    *,
    entry: ReportEntry,
    row_data: Mapping[str, Any],
) -> bytes:
    """``record()`` と同じ出力をする「切り替え前の実装」を手作業で再現したバイト列。

    旧実装の ``_append()`` は ``COLUMNS`` 順のリストを ``csv.writer`` で書いて
    いた。同じ入力を同じ手順で書き、 ``record()`` の出力とバイト単位で一致する
    ことを確かめる。**ファイルが空である前提**で見出し + データ行の 2 行を
    出力する。
    """
    values = [
        row_data["executed_at"].strftime("%Y-%m-%d %H:%M:%S"),
        entry.key,
        row_data["schedule_key"],
        entry.summary,
        entry.report_id,
        entry.url,
        "案件集計",
        SUCCESS if row_data["succeeded"] else FAILURE,
        _stage(row_data["fetched_from_salesforce"]),
        _stage(row_data["saved_to_file"]),
        row_data["folder_text"],
        row_data["file_name"],
        "" if row_data["row_count"] is None else row_data["row_count"],
        f"{row_data['seconds']:.2f}",
        row_data["cause"],
        row_data["error_code"],
        row_data["error"].replace("\n", " "),
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(COLUMNS))
    writer.writerow(values)
    # ``record()`` 側の実装は UTF-8 BOM 付きで書く（``CSV`` クラスの既定）ので、
    # 期待側もそれを再現する
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _row_bytes_from_record(
    history_path: Path,
    *,
    entry: ReportEntry,
    row_data: Mapping[str, Any],
) -> bytes:
    """``record()`` を呼んだあとの履歴ファイルの生バイトを返す。"""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    record(
        history_path,
        entry=entry,
        project="案件集計",
        row=HistoryRow(
            succeeded=row_data["succeeded"],
            fetched_from_salesforce=row_data["fetched_from_salesforce"],
            saved_to_file=row_data["saved_to_file"],
            file_name=row_data["file_name"],
            row_count=row_data["row_count"],
            seconds=row_data["seconds"],
            cause=row_data["cause"],
            error_code=row_data["error_code"],
            error=row_data["error"],
            schedule_key=row_data["schedule_key"],
        ),
        executed_at=row_data["executed_at"],
    )
    return history_path.read_bytes()


@pytest.fixture
def entry() -> ReportEntry:
    """テスト用の管理表1行。「グループ」を存在しない値にして、
    ``_resolved_folder()`` が ``GroupNotRegisteredError`` を ``output_path()``
    内で発生させ、文字列化される経路をテストする。
    """
    return ReportEntry(
        key="1001",
        summary="顧客一覧",
        url="https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view",
        group="登録されていないグループ",
        assignee="山田",
        enabled=True,
        allow_empty=False,
    )


def test_record_writes_byte_identical_row_to_history_csv(
    tmp_path: Path, entry: ReportEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record()`` が書く 1 行のバイト列が、切り替え前の実装と同じであることを確かめる。

    成功時の 1 行: 全フィールドを埋めて ``record()`` を呼び、戻り値のバイト列が
    期待値と一致することを見る。期待値はテスト本体で「同じ入力・同じ手順」を
    手作業で書き起こしたもの — ``record()`` の実装が変わったら期待値の
    組み立て方も変える必要があるため、両者が食い違うと検出できる。
    """
    # ``_resolved_folder()`` が ``output_path().parent`` を経由するため、
    # 管理表 + 設定シート（``load_group_settings`` が読む）を作っておく。
    # 「グループ」を登録しないでおき、``output_path()`` が
    # ``GroupNotRegisteredError`` を上げる経路を踏ませる。
    import src.paths as paths_module

    master_path = tmp_path / "管理表.xlsx"
    from comken.core.table import Table
    from comken.toolbox.excel import Excel

    with Excel(master_path) as excel:
        excel.create_data_sheet("設定").create_table("設定", Table(["グループ", "ベースURL"], []))
    monkeypatch.setattr(paths_module, "MASTER_PATH", master_path)
    monkeypatch.setattr(
        "comken.services.salesforce_downloader.paths.HISTORY_PATH", tmp_path / "履歴.csv"
    )

    history_path = tmp_path / "履歴.csv"
    # ``_resolved_folder()`` が生成する文字列を、``output_path()`` を呼んで
    # 実際の ``GroupNotRegisteredError`` メッセージから組み立てる。
    # 期待値はその文字列を含めてから組み立てる（先に作らないと、
    # ``_expected_row_bytes`` を呼んだあとに ``record()`` が
    # 履歴ファイルを作る関係で順序が崩れる）。
    from src.exceptions import GroupNotRegisteredError
    from src.paths import output_path

    row_data: dict[str, Any] = {
        "succeeded": True,
        "fetched_from_salesforce": True,
        "saved_to_file": True,
        "file_name": "1001_20260926_1000.csv",
        "row_count": 2,
        "seconds": 0.20,
        "cause": "",
        "error_code": "",
        "error": "",
        "schedule_key": "S001",
        "executed_at": dt.datetime(2026, 9, 26, 10, 0, 0),  # noqa: DTZ001
        "folder_text": None,  # 下の try/except で上書き
    }
    try:
        output_path(entry)
    except GroupNotRegisteredError as exc:
        row_data["folder_text"] = f"(保存先を組み立てられません: {exc})"
    else:  # pragma: no cover — ここには来ない
        row_data["folder_text"] = "(保存先を組み立てられません: 想定外)"

    # 期待値を見出し + データ行のバイト列として組み立てる
    expected = _expected_row_bytes(entry=entry, row_data=row_data)
    # ``record()`` を呼んで実際の出力を得る
    actual = _row_bytes_from_record(history_path, entry=entry, row_data=row_data)

    assert actual == expected


def test_record_writes_failure_row_with_unchanged_column_layout(
    tmp_path: Path, entry: ReportEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗時の 1 行も、列構成・順序・空欄の扱いが切り替わり前と同一であること。

    失敗時は ``Salesforce取得結果`` / ``保存結果`` の 3 状態（成功／失敗／未到達）
    と「原因区分」「エラーコード」「エラー内容」を埋める。すべての段階が埋まる
    ケース・途中で止まるケースの両方で列順と空欄の扱いを確かめる。
    """
    import src.paths as paths_module

    master_path = tmp_path / "管理表.xlsx"
    from comken.core.table import Table
    from comken.toolbox.excel import Excel

    with Excel(master_path) as excel:
        excel.create_data_sheet("設定").create_table("設定", Table(["グループ", "ベースURL"], []))
    monkeypatch.setattr(paths_module, "MASTER_PATH", master_path)
    monkeypatch.setattr(
        "comken.services.salesforce_downloader.paths.HISTORY_PATH", tmp_path / "履歴.csv"
    )
    # キャッシュを破棄（前テストで作られたキャッシュが影響しないように）
    paths_module._reset_cached_master()

    history_path = tmp_path / "履歴.csv"
    # 失敗（保存先フォルダ無し）のケース: Salesforce取得結果=空、保存結果=空、
    # 原因区分=「設定」、エラーコード=ReportFolderNotFoundError
    record(
        history_path,
        entry=entry,
        project="案件集計",
        row=HistoryRow(
            succeeded=False,
            fetched_from_salesforce=None,
            saved_to_file=None,
            cause="設定",
            error_code="ReportFolderNotFoundError",
            error="保存先のフォルダがありません: 1001",
            schedule_key="",
        ),
        executed_at=dt.datetime(2026, 9, 26, 11, 0, 0),  # noqa: DTZ001
    )

    # 書き出された CSV を ``csv.DictReader`` で読み、列順が ``COLUMNS`` と一致し、
    # 各列の値が期待どおりであることを確認する
    with history_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    assert header == list(COLUMNS)
    assert len(rows) == 1
    row = dict(zip(header, rows[0], strict=True))
    assert row["実行日時"] == "2026-09-26 11:00:00"
    assert row["管理番号"] == "1001"
    assert row["スケジュールキー"] == ""
    assert row["概要"] == "顧客一覧"
    assert row["レポートID"] == "00O5g00000ABCDE"
    assert row["Salesforce取得結果"] == ""
    assert row["保存結果"] == ""
    assert row["原因区分"] == "設定"
    assert row["エラーコード"] == "ReportFolderNotFoundError"
    assert row["エラー内容"] == "保存先のフォルダがありません: 1001"
    # 保存先フォルダは ``_resolved_folder()`` が例外を握りつぶして文字列化した値
    assert row["保存先"].startswith("(保存先を組み立てられません")

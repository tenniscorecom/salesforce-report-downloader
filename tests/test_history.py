"""ダウンロード履歴の別実行単位からの同時追記を検証する。"""

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest
from comken.toolbox.csv import CSV

from comken_salesforce_downloader.exceptions import HistoryHeaderMismatchError
from comken_salesforce_downloader.history import (
    HistoryRow,
    record,
    successful_files_today,
)
from comken_salesforce_downloader.master import ReportEntry


def test_concurrent_appends_keep_one_header_and_complete_rows(tmp_path) -> None:
    """同時に見出し作成と追記が走っても、欠損・混在した行を作らない。"""
    history_path = tmp_path / "履歴.csv"
    entry = ReportEntry(
        key="1001",
        summary="顧客一覧",
        url="https://example.com/Report/00O5g00000ABCDE/view",
        schedule="定期",
        folder=tmp_path,
        enabled=True,
        allow_empty=False,
        note="",
    )

    def append(index: int) -> None:
        record(
            history_path,
            entry=entry,
            project=str(index),
            trigger="定期",
            row=HistoryRow(
                succeeded=True,
                fetched_from_salesforce=True,
                saved_to_file=True,
                file_name=f"{index}.csv",
                row_count=index,
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(40)))

    with CSV(history_path) as csv_file:
        rows = csv_file.read()
    assert len(rows) == 40
    assert {row["プロジェクト"] for row in rows} == {str(index) for index in range(40)}


def test_process_appends_keep_one_header_and_complete_rows(tmp_path) -> None:
    """別プロセスから同時追記しても、見出しと各行を壊さない。"""
    history_path = tmp_path / "履歴.csv"
    arguments = [(str(history_path), str(tmp_path), index) for index in range(12)]
    with get_context("spawn").Pool(processes=4) as pool:
        pool.map(_append_from_process, arguments)

    with CSV(history_path) as csv_file:
        rows = csv_file.read()
    assert len(rows) == 12
    assert {row["プロジェクト"] for row in rows} == {str(index) for index in range(12)}


def test_rejects_history_with_different_header(tmp_path) -> None:
    """既存履歴の列が違う場合、値をずらして追記せず明示的に止める。"""
    history_path = tmp_path / "履歴.csv"
    history_path.write_text("管理番号,成否\n1000,成功\n", encoding="utf-8-sig")

    with pytest.raises(HistoryHeaderMismatchError):
        record(
            history_path,
            entry=_entry(tmp_path),
            project="追記",
            trigger="定期",
            row=HistoryRow(True, True, True, file_name="new.csv"),
        )

    assert history_path.read_text(encoding="utf-8-sig").splitlines()[0] == "管理番号,成否"


def test_successful_file_requires_both_overall_and_save_success(tmp_path) -> None:
    """成否だけが成功でも、保存成功が無い履歴を取得済みにしない。"""
    history_path = tmp_path / "履歴.csv"
    entry = _entry(tmp_path)
    record(
        history_path,
        entry=entry,
        project="異常な履歴",
        trigger="定期",
        row=HistoryRow(True, True, False, file_name="not-saved.csv"),
    )

    assert successful_files_today(history_path, entry.key) == []


def _append_from_process(arguments: tuple[str, str, int]) -> None:
    """spawnした子プロセスから履歴を1行追記する。"""
    history_path, folder, index = arguments
    record(
        history_path,
        entry=_entry(Path(folder)),
        project=str(index),
        trigger="定期",
        row=HistoryRow(True, True, True, file_name=f"{index}.csv", row_count=index),
    )


def _entry(folder: Path) -> ReportEntry:
    """各テストで同じ管理表1行を使う。"""
    return ReportEntry(
        key="1001",
        summary="顧客一覧",
        url="https://example.com/Report/00O5g00000ABCDE/view",
        schedule="定期",
        folder=folder,
        enabled=True,
        allow_empty=False,
        note="",
    )


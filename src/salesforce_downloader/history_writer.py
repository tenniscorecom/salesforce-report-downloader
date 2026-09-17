"""src/salesforce_downloader/history_writer.py — ダウンロード履歴への書き込み。

**2026-09 に comken から切り出した。** 履歴CSVの列定義・1行の形
（`HistoryRow` / `COLUMNS`）・排他ロック（`HistoryFileLock`）・読み取り関数
（`downloaded_today()` など）は comken 側（`comken.services.salesforce_downloader.history`）
に残っている共有契約で、ここではそれに従って**書く側だけ**を実装する。
書き込みを実行するのは、このプロジェクトが Salesforce へ取りに行く唯一の消費者
だから（詳しくは comken の `salesforce_downloader/__init__.py` の履歴メモを参照）。

読み取り側と同じ形式・同じロックを使うことが必須。ここで `COLUMNS` の並びを
変えたり、`HistoryFileLock` を経由せずに書いたりすると、comken 側の読み取り関数
（`downloaded_today()` 等）や `write_latest_status()` が壊れる。
"""

import csv
import logging
from pathlib import Path

from comken.core.clock import now
from comken.exceptions import HistoryHeaderMismatchError, HistoryWriteError
from comken.services.salesforce_downloader.history import (
    COLUMNS,
    FAILURE,
    SUCCESS,
    HistoryRow,
)
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock
from comken.services.salesforce_downloader.master import ReportEntry

logger = logging.getLogger(__name__)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def record(
    path: str | Path,
    *,
    entry: ReportEntry,
    project: str,
    row: HistoryRow,
) -> None:
    """履歴を1行追記する。ファイルが無ければ見出し行から作る。

    履歴は取得結果の根拠になる必須データなので、記録できなければ処理を失敗させる。

    Args:
        path: 履歴 CSV のパス。
        entry: 管理表1行。管理番号・概要・レポートID・URL・保存先はこの中身を履歴に出す。
        project: 呼び出したプロジェクト名。
        row: 履歴1行の本体（成否・各段階の結果・件数・エラー）。
    """
    path = Path(path)
    logger.debug(
        "履歴追記開始: path=%s, 管理番号=%s, schedule_key=%s, project=%s",
        path,
        entry.key,
        row.schedule_key,
        project,
    )
    values = [
        now().strftime(_TIMESTAMP_FORMAT),
        entry.key,
        row.schedule_key,
        entry.summary,
        entry.report_id,
        entry.url,
        project,
        SUCCESS if row.succeeded else FAILURE,
        _stage(row.fetched_from_salesforce),
        _stage(row.saved_to_file),
        str(entry.folder),
        row.file_name,
        "" if row.row_count is None else row.row_count,
        f"{row.seconds:.2f}",
        row.cause,
        row.error_code,
        row.error.replace("\n", " "),  # 1行1レコードを保つ
    ]
    try:
        with HistoryFileLock(path):
            _append(path, values)
    except HistoryWriteError:
        raise
    except OSError as exc:
        raise HistoryWriteError(path, str(exc)) from exc
    logger.debug("履歴追記完了: path=%s", path)


def _stage(value: bool | None) -> str:
    """3状態（成功／失敗／未到達）を履歴の文字列に変換する。"""
    if value is None:
        return ""
    return SUCCESS if value else FAILURE


def _append(path: Path, values: list) -> None:
    """1行を追記する。見出し行はファイルを作るときだけ書く。

    Excel が読めるよう UTF-8 BOM 付きにする。newline="" は csv モジュールの作法
    （Windows で空行が入るのを防ぐ）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    if not is_new:
        _validate_existing_header(path)
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COLUMNS)
        writer.writerow(values)


def _validate_existing_header(path: Path) -> None:
    """追記前に見出しを確認し、違う列へ値をずらして書く事故を防ぐ。

    comken 側の `history._require_expected_header()` と同じ判定だが、あちらは
    パッケージ内部専用（アンダースコア付き）のため、ここでは同じ判定をこの
    ファイル内で直接行う（`COLUMNS` は comken 側の共有契約からそのまま import
    しているので、判定基準そのものが2箇所で食い違うことはない）。
    """
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        actual = tuple(next(csv.reader(f), None) or ())
    if actual != COLUMNS:
        raise HistoryHeaderMismatchError(path, actual, COLUMNS)

"""src/history_writer.py — ダウンロード履歴への書き込み。

**2026-09 に comken から切り出した。** 履歴CSVの列定義・1行の形
（`HistoryRow` / `COLUMNS`）・排他ロック（`HistoryFileLock`）・読み取り関数
（`downloaded_today()` など）は comken 側（`comken.services.salesforce_downloader.sheets.history`）
に残っている共有契約で、ここではそれに従って**書く側だけ**を実装する。
書き込みを実行するのは、このプロジェクトが Salesforce へ取りに行く唯一の消費者
だから（詳しくは comken の `salesforce_downloader/__init__.py` の履歴メモを参照）。

読み取り側と同じ形式・同じロックを使うことが必須。ここで `COLUMNS` の並びを
変えたり、`HistoryFileLock` を経由せずに書いたりすると、comken 側の読み取り関数
（`downloaded_today()` 等）が壊れる。
"""

import csv
import io
import logging
from pathlib import Path

from comken.constants import Encoding
from comken.core.clock import now
from comken.core.files import atomic_write
from comken.exceptions import GroupNotRegisteredError, HistoryWriteError
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock
from comken.services.salesforce_downloader.provider import output_path
from comken.services.salesforce_downloader.sheets.history import (
    COLUMNS,
    FAILURE,
    SUCCESS,
    HistoryRow,
    migrate_row,
)
from comken.services.salesforce_downloader.sheets.master import ReportEntry
from comken.toolbox.csv import read_text

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
        entry: 管理表1行。管理番号・概要・レポートID・URLはこの中身を履歴に出す。
            保存先は `output_path()` で組み立て直した値を出す（`_resolved_folder()`）。
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
        _resolved_folder(entry),
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


def _resolved_folder(entry: ReportEntry) -> str:
    """保存先フォルダを文字列にする。

    ``output_path()`` は管理表の「グループ」列が設定シートに無いと
    ``GroupNotRegisteredError`` を送出する。**失敗の履歴記録中にこのエラーで
    さらに失敗すると、本来の失敗原因（Salesforce側のエラー等）ごと履歴に残せず、
    誰も追跡できなくなる**ため、ここでは握りつぶして理由を文字列として残す。
    """
    try:
        return str(output_path(entry).parent)
    except GroupNotRegisteredError as exc:
        return f"(保存先を組み立てられません: {exc})"


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
        _migrate_if_needed(path)
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(COLUMNS)
        writer.writerow(values)


def _migrate_if_needed(path: Path) -> None:
    """既存見出しが古い構成なら、ファイル全体を新しい ``COLUMNS`` に書き換える。

    既に ``COLUMNS`` と一致しているときは何もしない（毎回ファイル全体を
    書き換えるのは無駄なので、変更が必要なときだけ動かす）。``COLUMNS`` の
    増減・並び替えがあっても、運用中の履歴は ``migrate_row()`` で吸収して
    新しい ``COLUMNS`` の見出しに揃え直す。

    文字コードは ``read_text()`` の自動判定（UTF-8 BOM / CP932）で吸収する。
    人が Excel で開いて保存し直すと CP932 へ化けるため、プログラムが書く
    UTF-8 BOM 以外のエンコーディングでも読み込める必要がある。書き換え後は
    次の ``_append()`` と同じく UTF-8 BOM 付きへ正規化する（同じ ``_append()``
    ループからの追記と整合させるため）。

    この書き換えは ``HistoryFileLock`` の中で呼び出される前提になっている
    （``_append()`` の呼び出し経路が ``with HistoryFileLock(path):`` 内）。
    """
    text = read_text(path, encoding=Encoding.AUTO)
    actual = tuple(next(csv.reader(io.StringIO(text)), None) or ())
    if actual == COLUMNS:
        return  # 既に最新構成
    if not actual:
        # 見出しすら無い壊れたファイルは触らない。``_append()`` がそのまま
        # 追記を進めて、二重見出しなどの更なる事故を防ぐ
        logger.debug(
            "履歴マイグレーションをスキップ（見出しなし）: path=%s", path
        )
        return
    # 古い見出し → 全行を新しい COLUMNS に揃え直してアトミックに書き換え
    reader = csv.DictReader(io.StringIO(text), fieldnames=actual)
    next(reader, None)  # ヘッダー行を捨てる
    migrated_rows = [migrate_row(row) for row in reader]
    logger.debug(
        "履歴マイグレーション開始: path=%s, 古い列=%s → 新しい列=%s, 行数=%d",
        path,
        actual,
        COLUMNS,
        len(migrated_rows),
    )
    with (
        atomic_write(path) as temporary_path,
        temporary_path.open("w", encoding="utf-8-sig", newline="") as f,
    ):
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for row in migrated_rows:
            writer.writerow([row.get(column, "") for column in COLUMNS])
    logger.debug("履歴マイグレーション完了: path=%s", path)

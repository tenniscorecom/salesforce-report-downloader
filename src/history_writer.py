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

import datetime as dt
import logging
from pathlib import Path

from comken.core.dates import now
from comken.core.table import Table
from comken.exceptions import GroupNotRegisteredError, HistoryWriteError, InvalidTableInputError
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
from comken.toolbox.csv import CSV

logger = logging.getLogger(__name__)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def record(
    path: str | Path,
    *,
    entry: ReportEntry,
    project: str,
    row: HistoryRow,
    executed_at: dt.datetime | None = None,
) -> None:
    """履歴を1行追記する。ファイルが無ければ見出し行から作る。

    履歴は取得結果の根拠になる必須データなので、記録できなければ処理を失敗させる。

    Args:
        path: 履歴 CSV のパス。
        entry: 管理表1行。管理番号・概要・レポートID・URLはこの中身を履歴に出す。
            保存先は `output_path()` で組み立て直した値を出す（`_resolved_folder()`）。
        project: 呼び出したプロジェクト名。
        row: 履歴1行の本体（成否・各段階の結果・件数・エラー）。
        executed_at: 「実行日時」列に書く値。**その実行の開始時刻**を 1 実行で
            固定して渡すことで、23:59 に始まった実行が日付をまたいでも、判定・
            ファイル名・履歴のすべてが開始日で揃う。``None`` のときは従来どおり
            ``now()``（呼び出した瞬間の時刻）で書く（テストなど、時刻固定が
            不要な呼び出し側の後方互換）。
    """
    path = Path(path)
    logger.debug(
        "履歴追記開始: path=%s, 管理番号=%s, schedule_key=%s, project=%s",
        path,
        entry.key,
        row.schedule_key,
        project,
    )
    timestamp = now() if executed_at is None else executed_at
    values = [
        timestamp.strftime(_TIMESTAMP_FORMAT),
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
    except (OSError, InvalidTableInputError) as exc:
        # ``InvalidTableInputError`` は、既存の履歴が CP932 のとき、その文字コードで表せない文字
        # （絵文字など）を書こうとした場合。履歴は必須データなので、記録失敗として扱う
        # （文字コードは変えない。元のファイルは無傷で残る）
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

    既存見出しが古い構成のときは、全行を ``migrate_row()`` で今の ``COLUMNS``
    に揃え直したうえで、新しい1行を足して **1回の保存で** 書き込む
    （マイグレーションと追記を別々に2回書き直さない）。既存見出しが
    重複・空など致命的に壊れていると、 ``CSV`` クラスがそのまま例外を
    上げてファイルは何も書き換えない。

    書き込みは ``comken.toolbox.csv.CSV`` クラスに委譲する。文字コード自動判定
    （UTF-8 BOM / CP932）・見出しの検証（空・重複）・列数不一致の検出は
    ``CSV`` クラスが担当する。書き換え後は次の追記と同じく UTF-8 BOM 付きへ
    正規化される（同じ ``with`` からの追記と整合させるため）。

    パフォーマンス面の注意: 1 回あたりにファイル全体を原子的に書き直すため、
    行数が増えるほど 1 回の書き込みコストは増える。履歴の規模
    （1 日数十件程度）では許容できる。

    この関数は ``HistoryFileLock`` の中で呼び出される前提になっている
    （``record()`` の呼び出し経路が ``with HistoryFileLock(path):`` 内）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    record_dict = dict(zip(COLUMNS, values, strict=True))
    # ``is_new=True`` のときだけ ``columns=`` を渡す。既存ファイルに ``columns=``
    # を渡すと「見出しなしのファイル」扱いとなり、最初の1行をデータとして読んで
    # しまうため、渡すのは新規ファイル限定。
    with CSV(path, columns=list(COLUMNS) if is_new else None) as csv_file:
        if is_new:
            csv_file.append(record_dict)
            return
        # 既存ファイル: ``CSV.read()`` が見出し検証（重複・空）を行う。
        # 検証失敗時の ``CSVError`` はそのまま呼出側へ伝播する
        table = csv_file.read()
        if tuple(table.columns) == COLUMNS:
            # 既に最新構成 → 既存 Table に 1 行足すだけ
            table.append(record_dict)
            csv_file.replace(table)
            return
        # 見出しが古い構成 → ``migrate_row`` で今の ``COLUMNS`` に揃え、
        # そこに今回の 1 行を足して 1 回で書き直す
        migrated_rows = [migrate_row(row) for row in table.to_rows()]
        new_table = Table(list(COLUMNS), migrated_rows)
        new_table.append(record_dict)
        csv_file.replace(new_table)

"""src/history.py — 履歴CSVへの書き込み（``record()``）。

**履歴の形式・読み取り・ロック・置き場所は comken 側にある。** ダウンローダーは
「管理表の行から履歴に書く値を組み立てる」部分だけを持ち、書き込み自体は
``comken.services.salesforce_downloader.history.append_history()`` に委譲する。

履歴の形式が変わるたびに書く側と読む側（comken を import して履歴を読む別プロジェクト）が
食い違わないよう、両者が `comken.services.salesforce_downloader.history` の ``COLUMNS`` /
``HistoryRow`` / ``append_history()`` / ``HistoryFileLock`` を共通で使う。
"""

import datetime as dt
import logging
from pathlib import Path

from comken.services.salesforce_downloader.history import (
    COLUMNS,
    FAILURE,
    ROUTE_API,
    ROUTE_BROWSER,
    ROUTE_SOQL,
    SUCCESS,
    HistoryRow,
    append_history,
)

from src.exceptions import GroupNotRegisteredError
from src.paths import output_path
from src.sheets.master import ReportEntry

logger = logging.getLogger(__name__)


def record(
    path: str | Path,
    *,
    entry: ReportEntry,
    project: str,
    row: HistoryRow,
    executed_at: dt.datetime,
    folder: str | Path,
) -> None:
    """履歴を1行追記する。ファイルが無ければ見出し行から作る。

    履歴の本体（ロック・列検証・書き換え）は comken 側の ``append_history()`` に委譲する。
    「取得経路」列は、呼び出し側が ``row.route`` に明示していればそれを、無ければ
    ``_route_of(entry)`` で「SOQL」「2000件超」列から決める。

    Args:
        path: 履歴 CSV のパス。
        entry: 管理表1行。管理番号・概要・レポートID・URLはこの中身を履歴に出す。
        project: 呼び出したプロジェクト名。
        row: 履歴1行の本体（成否・各段階の結果・件数・エラー）。型は comken 側の
            ``HistoryRow``（``src.history.HistoryRow`` は同じ型を再エクスポート）。
        executed_at: 「実行日時」列に書く値。**その実行の開始時刻**を渡して、日付をまたぐ
            実行でも判定・ファイル名・履歴を開始日で揃える。
        folder: 履歴の「保存先」列に書く値。**実際にファイルを置いたフォルダ**
            （``ベース/概要``）を渡す。ファイル未保存のケースでは ``resolved_folder()`` が
            作った ``(保存先を組み立てられません: ...)`` 文字列を渡す。
    """
    path = Path(path)
    logger.debug(
        "履歴追記開始: path=%s, 管理番号=%s, schedule_key=%s, project=%s",
        path,
        entry.key,
        row.schedule_key,
        project,
    )
    values: dict[str, object] = {
        "管理番号": entry.key,
        "スケジュールキー": row.schedule_key,
        "概要": entry.summary,
        "レポートID": entry.report_id,
        "URL": entry.url,
        "プロジェクト": project,
        "成否": SUCCESS if row.succeeded else FAILURE,
        "Salesforce取得結果": _stage(row.fetched_from_salesforce),
        "保存結果": _stage(row.saved_to_file),
        "保存先": str(folder),
        "ファイル名": row.file_name,
        "取得件数": "" if row.row_count is None else row.row_count,
        "処理秒数": f"{row.seconds:.2f}",
        "原因区分": row.cause,
        "エラーコード": row.error_code,
        "エラー内容": row.error.replace("\n", " "),
        # 「取得経路」列: ``row.route`` が空でなければそれを優先し、空なら
        # ``_route_of(entry)``（SOQL → 2000件超 → API の優先順位）
        "取得経路": row.route or _route_of(entry),
    }
    # ``append_history()`` は ``COLUMNS`` に無いキーを止めるので ``COLUMNS`` 順に並べ直す
    # （``実行日時`` は ``append_history`` 側で組み立てる）
    values = {column: values.get(column, "") for column in COLUMNS if column != "実行日時"}
    append_history(path, values, executed_at=executed_at)
    logger.debug("履歴追記完了: path=%s", path)


def resolved_folder(entry: ReportEntry) -> str:
    """失敗履歴用に保存先フォルダ文字列を組み立てる。

    ``output_path()`` は管理表の「グループ」列が設定シートに無いと
    ``GroupNotRegisteredError`` を送出する。失敗の履歴記録中にこのエラーで
    さらに失敗すると本来の失敗原因ごと残せなくなるため、握りつぶして理由を文字列で残す。
    成功時は ``output_path(entry).parent`` を文字列化して返す。
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


def _route_of(entry: ReportEntry) -> str:
    """``entry``（管理表の1行）から「取得経路」列に書く文字列を決める。

    優先順位は ``src.service._fetch()`` と同じ（**SOQL → 2000件超 → API**）:

    1. ``entry.use_soql`` が真 → ``ROUTE_SOQL``
    2. ``entry.exceeds_row_limit`` が真 → ``ROUTE_BROWSER``
    3. どちらも偽 → ``ROUTE_API``

    自動切替2種（``ROUTE_BROWSER_FALLBACK_*``）はこの関数では返さず、呼び出し側が
    ``row.route`` に入れる。
    """
    if entry.use_soql:
        return ROUTE_SOQL
    if entry.exceeds_row_limit:
        return ROUTE_BROWSER
    return ROUTE_API


__all__ = ["record", "resolved_folder", "HistoryRow"]

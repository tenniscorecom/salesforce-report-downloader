"""src/history.py — 履歴 SQLite への書き込み（``record()``）。

**履歴の形式・読み取り・ロック・置き場所は comken 側にある。** ダウンローダーは
「管理表の行から履歴に書く値を組み立てる」部分だけを持ち、書き込み自体は
``comken.services.salesforce_downloader.history.append_history()`` に委譲する。
履歴の正本は SQLite で、人が見る CSV は ``download_scheduled()`` が
``export_history()`` で書き出す（ このファイルでは扱わない ）。

境界を履歴にした理由: 履歴の形式が変わるたびに、書く側（ダウンローダー）と
読む側（comken を import して履歴を読む別プロジェクト）が食い違う事故を防ぐため、
両者が `comken.services.salesforce_downloader.history` の ``HistoryColumns`` /
``HistoryRow`` / ``append_history()`` / ``HistoryFileLock`` を共通で使う。
"""

import datetime as dt
import logging
from pathlib import Path

from comken.core.dates import now
from comken.services.salesforce_downloader.history import (
    FAILURE,
    SUCCESS,
    HistoryColumns,
    HistoryRow,
)

from src.exceptions import GroupNotRegisteredError
from src.sheets.master import ReportEntry

logger = logging.getLogger(__name__)


def record(
    path: str | Path,
    *,
    entry: ReportEntry,
    project: str,
    row: HistoryRow,
    executed_at: dt.datetime | None = None,
    folder: str | Path | None = None,
) -> None:
    """履歴 SQLite に1行追記する。

    履歴の本体（ロック・列検証・書き換え）は comken 側の ``append_history()`` に
    委譲する。**書き込まれる値（各列の中身・順番・空欄の扱い）は comken 側に
    移す前の ``record()`` と 1 文字も変えない**（管理番号・概要・レポートID・URL
    ・保存先・ファイル名・件数・秒数・原因区分・エラーコード・エラー内容の各列と
    その順序）。人が見る CSV への書き出しは ``download_scheduled()`` 側で
    ``export_history()`` を呼んで行うので、 ``record()`` は DB に書くだけ。

    Args:
        path: 履歴 SQLite のパス。親フォルダは ``append_history()`` が無ければ作成する。
        entry: 管理表1行。管理番号・概要・レポートID・URLはこの中身を履歴に出す。
            保存先は ``folder`` が指定されていればその値を、指定が無ければ
            ``output_path()`` で組み立て直した値を出す（`_resolved_folder()`）。
        project: 呼び出したプロジェクト名。
        row: 履歴1行の本体（成否・各段階の結果・件数・エラー）。型は comken 側の
            ``HistoryRow``（``src.history.HistoryRow`` は同じ型を再エクスポート）。
        executed_at: 「実行日時」列に書く値。**その実行の開始時刻**を 1 実行で
            固定して渡すことで、23:59 に始まった実行が日付をまたいでも、判定・
            ファイル名・履歴のすべてが開始日で揃う。``None`` のときは従来どおり
            ``now()``（呼び出した瞬間の時刻）で書く（テストなど、時刻固定が
            不要な呼び出し側の後方互換）。
        folder: 履歴の「保存先」列に書く値。**実際にファイルを置いたフォルダ**
            （``ベース/概要``）を文字列で渡す。``None`` のときは ``output_path()``
            で組み立て直す（失敗の履歴など、ファイル未保存のケースの後方互換用）。
            概要のフォルダは出力パス組み立て時に動的に決まるので、 ``_save()`` から
            実保存パス（``path.parent``）を渡してもらう形にすると「概要の決定
            ロジック」が履歴側に漏れ出さない。
    """
    from comken.services.salesforce_downloader.history import append_history

    path = Path(path)
    logger.debug(
        "履歴追記開始: path=%s, 管理番号=%s, schedule_key=%s, project=%s",
        path,
        entry.key,
        row.schedule_key,
        project,
    )
    timestamp = now() if executed_at is None else executed_at
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
        "保存先": str(folder) if folder is not None else _resolved_folder(entry),
        "ファイル名": row.file_name,
        "取得件数": "" if row.row_count is None else str(row.row_count),
        "処理秒数": f"{row.seconds:.2f}",
        "原因区分": row.cause,
        "エラーコード": row.error_code,
        "エラー内容": row.error.replace("\n", " "),
        # 取得経路（API / SOQL / ブラウザ / 自動切替2種）。``HistoryRow.route`` の
        # 既定値は空文字なので、``record()`` 経由で書かれる経路が無い古い呼び出しも
        # そのまま通る（``append_history()`` が ``HistoryColumns.names()`` に無い
        # キーで ``InvalidTableInputError`` を出す関係で、後段の
        # ``HistoryColumns.names()`` 並べ直しで空文字に落ちる）
        "取得経路": row.route,
    }
    # ``append_history()`` が ``HistoryColumns.names()`` に無いキーを
    # ``InvalidTableInputError`` で止めるので、 ``HistoryColumns.names()`` 順の dict
    # へ並べ直す（``実行日時`` は ``append_history`` 側で ``timestamp`` から組み立てる
    # ため、ここでは含めない）
    values = {
        column: values.get(column, "") for column in HistoryColumns.names() if column != "実行日時"
    }
    append_history(path, values, executed_at=timestamp)
    logger.debug("履歴追記完了: path=%s", path)


def _resolved_folder(entry: ReportEntry) -> str:
    """保存先フォルダを文字列にする。

    ``output_path()`` は管理表の「グループ」列が設定シートに無いと
    ``GroupNotRegisteredError`` を送出する。**失敗の履歴記録中にこのエラーで
    さらに失敗すると、本来の失敗原因（Salesforce側のエラー等）ごと履歴に残せず、
    誰も追跡できなくなる**ため、ここでは握りつぶして理由を文字列として残す。
    """
    from src.paths import output_path

    try:
        return str(output_path(entry).parent)
    except GroupNotRegisteredError as exc:
        return f"(保存先を組み立てられません: {exc})"


def _stage(value: bool | None) -> str:
    """3状態（成功／失敗／未到達）を履歴の文字列に変換する。"""
    if value is None:
        return ""
    return SUCCESS if value else FAILURE


__all__ = ["record", "HistoryRow"]

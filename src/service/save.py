r"""src/service/save.py — 保存とフォルダ検査。

`download_scheduled()` の 1 件単位で「フォルダ検査 → 取得 → 保存」を担当する
`save_report()` / `_write_csv()` / `require_folder()` を置く。下位モジュール
（`api.py` / `soql.py`）には依存しない（`src.paths` と `src.exceptions` だけ）。

    from src.service.save import save_report, _write_csv, require_folder

**列名の置き換えはここでは行わない。** SOQL 経由のレポートの列名
（``COLUMN_NAMES`` / ``列名の対応.csv``）は ``src.service.soql`` の
``fetch_via_soql()`` が取得時に置き換える（``transform()`` の前）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from comken.core.files import atomic_write
from comken.core.table.model import Table
from comken.runtime import is_dry_run
from comken.toolbox.csv import CSV

from src import paths
from src.exceptions import (
    EmptyReportError,
    ReportFolderNotFoundError,
    SavedFileEmptyError,
)
from src.sheets.master import ReportEntry

logger = logging.getLogger("src.service")


def require_folder(entry: ReportEntry) -> None:
    """保存先フォルダが無ければ `ReportFolderNotFoundError`。**勝手に作らない。**

    無いのは書き間違いのことが多く、勝手に作ると誰も読まない場所へ置き続けることになる。

    検査するのは**ベースフォルダ**のみ。出力パスは「ベース / 概要」の 2 階層だが、
    概要のフォルダは ``save_report()`` が ``mkdir(exist_ok=True)`` で自動作成する
    （ここで検査すると、人が概要フォルダを掘る手間が増える）。

    保存先フォルダは ``paths.base_folder()`` が組み立てる（管理表の「グループ」＋
    設定シートの「ベースURL」）。`entry.group` が設定シートに無い場合はここより先に
    `GroupNotRegisteredError` が上がる（出力先そのものを決められないほうが
    根本的なエラーだから）。
    """
    base = paths.base_folder(entry)
    if not base.is_dir():
        raise ReportFolderNotFoundError(entry.key, base)


def save_report(
    entry: ReportEntry,
    table: Table,
    *,
    schedule_run_time: dt.time | None = None,
    current: dt.datetime | None = None,
) -> Path:
    """行数と `allow_empty` に応じて保存先へ書き込み、書き終わったパスを返す。

    0 行・`allow_empty` × → `EmptyReportError`（=失敗）／○ → 空 CSV を置く。
    ``report.get()`` が返す ``Table.columns`` を使うため、0 行でも Salesforce の
    メタデータから得た見出しを保存する。

    **列の無い表を保存するときは WARNING を 1 行出す**（見出しの無い 0 バイトの
    ファイルになる）。列名を得る手段が無いので動きは変えない
    （``USE_BULK=False`` の SOQL が 0 件のとき）。

    **保存の流れ**（本番の名前の空ファイルを**一瞬も作らない**ため）:

    1. 同じフォルダに**一時ファイル**（``~`` プレフィックス + UUID）へ CSV を全部書く
    2. 通常モード（``entry.overwrite`` が ``False``）は ``os.rename`` で最終パス
       （または連番 ``_1`` / ``_2`` …）へ移す。``os.rename`` は Windows で移し先が
       存在すると ``FileExistsError`` になるので、それを「次の連番候補へ」の判定に
       使い、**上書きしない**
    3. 上書きモード（``entry.overwrite`` が ``True``）は ``os.replace`` で最終パスへ
       **毎回上書き**する（Windows でも移し先が存在してよい）。連番は付けない
       （``{管理番号}_{HHMM}.csv``。同じ ``HHMM`` なら同じ名前になり、翌日の同時刻に
       上書きされる）
    4. 失敗時は一時ファイルと（移せていれば）最終ファイルを ``unlink`` で片付ける

    連番上限 ``RESERVE_PATH_LIMIT`` まで ``FileExistsError`` が続いたら
    ``DownloaderError``。詳細は ``paths.move_into_place()`` の docstring 参照。

    保存先は ``paths.output_path()`` が返す単一のパス:

    - 通常モード: ``ベース/概要/{管理番号}_{YYYYMMDD}_{HHMM}.csv``
    - 上書きモード: ``ベース/概要/{管理番号}_{HHMM}.csv``

    通常モードは連番（``_1`` / ``_2`` …）を付けて上書きしない。取得済みキャッシュを
    読む側は同じフォルダの連番を全部候補に拾えるので、何度実行しても当日の最新状態は
    崩れない。上書きモードは ``os.replace`` で ``HHMM`` 単位に毎回置き換える
    （古いファイルの内容は消える）。

    **概要のフォルダは保存前にここで ``mkdir`` する。** ベースフォルダは
    ``require_folder()`` で先に検査済みなので、``parents=False`` で「無ければ失敗する」
    をそのまま流用できる。**dry-run 中は ``mkdir`` もしない**（保存もしない契約に
    沿って、フォルダだけ作ると「書き込まないのにフォルダだけが残る」事故を防ぐ）。

    ``current`` は ``_download_scheduled_locked()`` で固定した ``clock_now()`` の値。
    23:59 に始まった実行が日付をまたいで終わっても、出力ファイル名が開始日で揃う。

    **列名の置き換えはここでは行わない。** SOQL 経由のレポートの列名は
    ``src.service.soql`` の ``fetch_via_soql()`` が取得時に置き換える
    （``transform()`` の前）。ここは受け取った ``Table`` をそのまま CSV に書く。
    """
    base = paths.output_path(entry, schedule_run_time, now=current)
    # 障害調査時に「どのパスに書こうとして何が起きなかったか」を追えるよう、
    # 保存先を決定した時点で DEBUG を 1 行出す（上書きモードかどうかも一緒に出す）
    logger.debug("保存先を決定しました: パス=%s, 上書き=%s", base, entry.overwrite)
    _prepare_output_directory(base)

    # 一時ファイル（~プレフィックス）へ全部書いてから移す
    tmp_path = paths.make_temp_csv_path(base)
    final_path: Path | None = None
    try:
        # 問い合わせは成功したが明細が無い。**0件あり × なら失敗扱い、○ なら正常終了**
        if not table and not entry.allow_empty:
            raise EmptyReportError(entry.key, entry.summary, entry.url)
        # **dry-run は副作用を出さない契約。** ファイルも一時ファイルも作らない。
        # 戻り値は「保存しようとしたパス」と同じ ``base``（連番探索は行わない。
        # dry-run は競合判定まで含めて副作用を出さない運用）
        if is_dry_run():
            return base
        _warn_columns_are_empty(entry, table)
        _write_csv(tmp_path, table)
        final_path = _finalize_save(tmp_path, base, entry)
        _verify_saved_file_not_empty(final_path, table, entry)
    except Exception:
        # **失敗時の後始末。** ``final_path`` が決まっていれば削除する。
        # ``tmp_path`` は move_into_place に成功していれば既に存在しない
        # （rename で消えた）ので ``missing_ok=True`` で安全
        _cleanup_failed_save(tmp_path, final_path)
        raise
    return final_path


def _prepare_output_directory(base: Path) -> None:
    """概要フォルダを保存前に ``mkdir`` する。dry-run 中は何もしない。

    ベースフォルダは ``require_folder()`` で先に検査済みなので ``parents=False``
    で「無ければ失敗する」をそのまま流用できる（ベースが既に無い場合はここで
    ``FileNotFoundError`` が出る。先に ``require_folder()`` で
    ``ReportFolderNotFoundError`` に変換する流れが壊れないよう、 ``save_report()`` 単体で
    動くテスト経路のために ``OSError`` のまま伝播させる）。
    """
    if not is_dry_run():
        base.parent.mkdir(exist_ok=True)


def _warn_columns_are_empty(entry: ReportEntry, table: Table) -> None:
    """列の無い表を保存するとき、WARNING を 1 行出す。

    **列が無いと見出しも無いので、0 バイトのファイルになる。** 0 バイト検査は
    「列が無いときは検査しない」ので通ってしまう。列名を得る手段が無いので
    動きは変えない（``USE_BULK=False`` の SOQL が 0 件のときに起こる）ため、
    気づけるように警告だけ残す。
    """
    if table.columns:
        return
    logger.warning(
        "見出しの無い空のファイルを保存しました: 管理番号=%s, 概要=%s"
        "（USE_BULK=False の SOQL は0件だと列名が取れません）",
        entry.key,
        entry.summary,
    )


def _finalize_save(tmp_path: Path, base: Path, entry: ReportEntry) -> Path:
    """一時ファイルを最終パスへ移し、移し終わったパスを返す。

    **上書きモード:** ``{管理番号}_{HHMM}.csv`` の名前で毎回置き換える。 同じ
    ``HHMM`` なら同じパスになり、 翌日の同時刻に上書きされる。 ``os.replace`` は
    Windows でも移し先が存在するとそのまま上書きする（``os.rename`` と違い
    ``FileExistsError`` にならない）。 書き込みは既に ``_write_csv()`` で終わって
    いるので、 ここで失敗したら既存ファイルはそのまま残る契約（``except`` 経路
    で ``tmp_path`` だけ片付ける）。 連番探索は行わない。

    **通常モード:** 一時ファイルを最終パスへ ``os.rename``。 移し先が存在すれば
    次の連番候補を試す。 上書きはしない（``os.rename`` の Windows 仕様で
    ``FileExistsError``）。
    """
    if entry.overwrite:
        os.replace(tmp_path, base)
        return base
    return paths.move_into_place(tmp_path, base, entry.key)


def _verify_saved_file_not_empty(final_path: Path, table: Table, entry: ReportEntry) -> None:
    """保存先が 0 バイトなら ``SavedFileEmptyError`` で落とす。

    保存先（ファイルサーバー）のクォータが足りないと、書き込みはエラーにならずに
    中身だけが捨てられ、 0 バイトのファイルが残る。 空のファイルを
    成功扱いにしないための検査。 ``table.columns`` が空のときは見出しも無いので
    検査しない（書きようが無い）。 dry-run はこの手前で ``return`` 済み。
    """
    if not table.columns:
        return
    size = final_path.stat().st_size
    if size == 0:
        raise SavedFileEmptyError(entry.key, entry.summary, final_path, len(table))
    logger.info("保存しました: %s（%d バイト）", final_path, size)


def _cleanup_failed_save(tmp_path: Path, final_path: Path | None) -> None:
    """失敗時に一時ファイルと（移せていれば）最終ファイルを ``unlink`` で片付ける。

    ``final_path`` が決まっていれば（``move_into_place`` が成功したあとに 0 バイト
    検査で落とすケース）削除する。 ``tmp_path`` は ``move_into_place`` に成功して
    いれば既に存在しない（rename で消えた）ので ``missing_ok=True`` で安全。
    """
    if final_path is not None:
        final_path.unlink(missing_ok=True)
    tmp_path.unlink(missing_ok=True)


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ CSV を書く。

    複数のプロジェクトが同時に呼ぶので、直接書くと**読んでいる最中のファイルが
    半端な状態**になりうる。同じフォルダ内の置き換えは一度に入れ替わる。

    **注意:** ``save_report()`` はこの関数を **一時ファイル**
    （``paths.make_temp_csv_path()`` が返した ``~`` プレフィックスのパス）に対して
    呼び、最終パスへの ``os.replace`` は行わない（``paths.move_into_place()``
    が ``os.rename`` で移す。上書きしない）。``_write_csv()`` の引数は
    単に「**ここに書く**」パスで、本関数の動作は変えていない。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)

r"""src/service/save.py — 保存とフォルダ検査。

`download_scheduled()` の 1 件単位で「フォルダ検査 → 取得 → 保存」を担当する
`_save()` / `_write_csv()` / `_require_folder()` を置く。下位モジュール
（`api.py` / `browser.py` / `soql.py`）には依存しない（`src.paths`
と `src.exceptions` だけ）。

    from src.service.save import _save, _write_csv, _require_folder

`_save()` の 2026-10 改定（``~`` 一時ファイル → ``os.rename`` /
``os.replace``）や ``_write_csv()`` の ``atomic_write`` 使いはもとのまま。
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
from src.soql_reports import soql_report_for
from src.soql_reports.column_names import extract_main_object, load_column_names_for
from src.soql_reports.runner import _apply_column_names

logger = logging.getLogger("src.service")


def _require_folder(entry: ReportEntry) -> None:
    """保存先フォルダが無ければ `ReportFolderNotFoundError`。**勝手に作らない。**

    作らずに失敗させる。無いのは書き間違いのことが多く、勝手に作ると
    誰も読まない場所へ置き続けることになる。

    検査するのは「**ベースフォルダ**」のみ。出力パスは「ベース / 概要」の
    2 階層になったが、ここで作るかを分けるのはベースの有無だけで十分
    （概要のフォルダは ``_save()`` が ``mkdir(exist_ok=True)`` で自動作成する。
    ここで概要の有無まで検査すると、保存のたびに人が概要フォルダを掘る必要が
    出て、運用上の意味がない）。

    保存先フォルダは ``paths.base_folder()`` が組み立てる
    （管理表の「グループ」＋設定シートの「ベースURL」）。
    `entry.group` が設定シートに無い場合はここより先に
    `GroupNotRegisteredError` が上がる（フォルダの有無より先に、
    そもそも出力先を決められないという、より根本的なエラーとして扱う）。
    """
    base = paths.base_folder(entry)
    if not base.is_dir():
        raise ReportFolderNotFoundError(entry.key, base)


def _save(
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

    **保存の流れ（2026-10 改定）:**
    旧版は ``output_path`` が指す本番の名前の空ファイルを ``open("x")`` で
    予約し、CSV を書いた一時ファイルで ``os.replace`` する 2 段構えだった。
    2026-10 に会社のファイルサーバー（UNC）で「取得しました」とログが出たのに
    ファイルが 0 バイトだった（後に原因は保存先のクォータ不足と判明）。調査の
    途中で、予約の空ファイルが書き込みの間ずっと本番の名前で見えている作りも
    避けることにした。

    改定後は、

    1. 同じフォルダに **一時ファイル**（ ``~`` プレフィックス + UUID）に CSV を全部書く
    2. 通常モード（``entry.overwrite`` が ``False``）は ``os.rename`` で最終パス
       （または連番 ``_1`` / ``_2`` …）へ移す — ``os.rename`` は Windows で
       移し先が存在すると ``FileExistsError`` になるので、それを「次の連番候補
       へ」の判定に使い、**上書きしない**
    3. 上書きモード（``entry.overwrite`` が ``True``）は ``os.replace`` で最終
       パスへ **毎回上書き** する（Windows でも移し先が存在してよい）。 連番は
       付けない（``{管理番号}_{HHMM}.csv``。同じ ``HHMM`` なら同じ名前になり、
       翌日の同時刻に上書きされる）
    4. 失敗時は一時ファイルと（移せていれば）最終ファイルを ``unlink`` で片付ける

    という流れになり、**本番の名前の空ファイルを一瞬も作らない**。
    連番上限 ``RESERVE_PATH_LIMIT`` まで ``FileExistsError`` が続いたら
    ``ReportReservePathLimitError``。詳細は ``paths.move_into_place()`` の
    docstring 参照。

    保存先は ``paths.output_path()`` が返す単一のパス:

    - 通常モード: ``ベース/概要/{管理番号}_{YYYYMMDD}_{HHMM}.csv``
    - 上書きモード: ``ベース/概要/{管理番号}_{HHMM}.csv``

    通常モードの衝突回避は ``paths.move_into_place()`` で常に（連番 ``_1`` /
    ``_2`` … を付けて）。 同じパスへの上書きは想定しない — 取得済みキャッシュを
    読む側（`pathlib.Path.glob()` で `paths.output_path()` が組み立てたパスを
    探す運用）は、同じフォルダ内の連番を全部候補に含めて新しい順で拾えるので、
    何度実行しても履歴と当日の最新状態は崩れない。 上書きモードは ``os.replace``
    で ``HHMM`` 単位で毎回置き換える（古いファイルの内容は消える）。

    **概要のフォルダは保存前にここで ``mkdir`` する。** ベースフォルダは
    ``_download()`` 側の ``_require_folder()`` で先に検査済みなので、
    ``parents=False`` で「無ければ失敗する」をそのまま流用できる（ベースが既に
    無い場合はここで ``FileNotFoundError`` が出る。先に ``_require_folder()`` で
    ``ReportFolderNotFoundError`` に変換する流れが壊れないよう、 ``_save()`` 単体で
    動くテスト経路のために ``OSError`` のまま伝播させる）。**dry-run 中は
    ``mkdir`` もしない**（``comken.runtime.dry_run`` のブロック内では保存もしない設計に
    沿って、フォルダだけ作ると「書き込まないのにフォルダだけが残る」事故を防ぐ）。

    ``current`` は ``_download_scheduled_locked()`` で固定した ``clock_now()`` の値。
    ``paths.output_path(now=current)`` に渡すことで、23:59 に始まった実行が日付をまたいで
    終わっても、出力ファイル名が開始日で揃う。``None`` のときは ``paths.output_path()``
    側で ``clock_now()`` にフォールバックする（既存の単体テスト経路を残すため）。
    上書きモードは ``HHMM`` 単位なので ``current`` は ``schedule_run_time`` と
    合成する**日付**にしか影響しない（テストの出力パス固定のために
    ``schedule_run_time`` / ``now`` を渡しても安全）。

    **失敗時の後始末について:** 書き込みが例外の原因になった場合は、既存の
    ``_download()`` の ``except Exception`` 経路で ``_Attempt.record_failure()``
    を経由して ``ScheduledDownloadFailedError`` に変換される。一時ファイル
    （``move_into_place()`` がまだ移せていない）と最終ファイル（移せたが 0 バイト
    検査で落とすケース）は ``unlink(missing_ok=True)`` で後始末する。

    **複雑度の整理（2026-10 改定）:** 本体は「 ``base`` を決める → 一時ファイルを
    用意 → ``EmptyReportError`` / dry-run を判定 → SOQL 用の列名適用 → CSV を書く
    → 最終パスへ移す → 0 バイト検査」の手順に絞り、 各手順を ``_prepare_*`` /
    ``_finalize_*`` / ``_verify_*`` / ``_cleanup_*`` 系のヘルパーに委譲して
    本体の複雑度を下げる。
    """
    base = paths.output_path(entry, schedule_run_time, now=current)
    # 保存先を決定した時点で DEBUG を 1 行出す（上書きモードかどうかも一緒に出す）。
    # 障害調査時に「どのパスに書こうとして何が起きなかったか」を追えるようにする
    logger.debug("保存先を決定しました: パス=%s, 上書き=%s", base, entry.overwrite)
    _prepare_output_directory(base)

    # **一時ファイル（~プレフィックス）を作ってそこへ書く。** 本番の名前の
    # 空ファイルを一瞬も作らない。
    tmp_path = paths.make_temp_csv_path(base)
    final_path: Path | None = None
    try:
        # 問い合わせは成功したが明細が無い。**0件あり × なら失敗扱い、○ なら正常終了**
        if not table and not entry.allow_empty:
            raise EmptyReportError(entry.key, entry.summary, entry.url)
        # **dry-run は副作用を出さない契約。** ファイルも一時ファイルも作らない。
        # ``_save()`` の戻り値パスは「保存しようとしたパス」と同じ ``base`` を返す
        # （連番探索は行わない。dry-run は競合判定まで含めて副作用を出さない運用）
        if is_dry_run():
            return base
        table = _apply_soql_columns_if_needed(entry, table)
        _write_csv(tmp_path, table)
        final_path = _finalize_save(tmp_path, base, entry)
        _verify_saved_file_not_empty(final_path, table, entry)
    except Exception:
        # **失敗時の後始末。** ``final_path`` が決まっていれば（move_into_place が
        # 成功したあとに 0 バイト検査で落とすケース）削除。``tmp_path`` は
        # move_into_place に成功していれば既に存在しない（rename で消えた）ので
        # ``missing_ok=True`` で安全
        _cleanup_failed_save(tmp_path, final_path)
        raise
    return final_path


def _prepare_output_directory(base: Path) -> None:
    """概要フォルダを保存前に ``mkdir`` する。dry-run 中は何もしない。

    ベースフォルダは ``_require_folder()`` で先に検査済みなので ``parents=False``
    で「無ければ失敗する」をそのまま流用できる（ベースが既に無い場合はここで
    ``FileNotFoundError`` が出る。先に ``_require_folder()`` で
    ``ReportFolderNotFoundError`` に変換する流れが壊れないよう、 ``_save()`` 単体で
    動くテスト経路のために ``OSError`` のまま伝播させる）。
    """
    if not is_dry_run():
        base.parent.mkdir(exist_ok=True)


def _apply_soql_columns_if_needed(entry: ReportEntry, table: Table) -> Table:
    """SOQL 経由のときだけ ``COLUMN_NAMES`` を適用した ``Table`` を返す。

    **管理表の「SOQL」列が「○」の行は ``reports/`` 配下の ``SoqlReport`` 経由で
    取りに来ていて、 ``SoqlReport.COLUMN_NAMES`` が人が読む列名への対応表を
    持っている。 2026-10 から ``soql-collector`` が出力する ``列名の対応.csv``
    からも自動で拾う（手書きが無い列や新しいレポートでも、 soql-collector を
    一度走らせれば反映される）。 ``COLUMN_NAMES`` の **手書きは優先**
    （同じキーは手書きが勝つ） — 「直したい列だけ書けばよい」を崩さないため。

    見出しが衝突すると ``SoqlColumnNameConflictError`` が送出されるが、
    その場合は ``_save()`` の ``except`` へ流れるので CSV は書かれない。
    Report API / ブラウザ経由は ``SoqlReport`` を見ないので、 そのまま
    ``table`` を返す。
    """
    if not entry.use_soql:
        return table

    report_cls = soql_report_for(entry.key)
    # ``table.columns`` は ``Table`` が所有する ``list`` で、 後続の
    # ``_apply_column_names()`` は新しい Table を返すので元の list は
    # 書き変わらない。 コピーは不要
    original_columns = table.columns
    soql_text = report_cls().soql()
    extra = _load_soql_extra_column_names(entry, report_cls, soql_text)
    updated = _apply_column_names(report_cls, table, extra=extra)
    _log_column_name_replacements(entry, original_columns, report_cls, extra)
    return updated


def _load_soql_extra_column_names(
    entry: ReportEntry, report_cls: type, soql_text: str
) -> dict[str, str]:
    """``列名の対応.csv`` からこのレポートの主オブジェクトに該当する行を引く。

    主オブジェクトが取れない（ SOQL に ``FROM`` が無い等）ときは空 ``dict`` を
    返し、 API 名のまま保存させる（取得は失敗させない）。 WARNING には SOQL 本文
    （ WHERE の値等）を出さず、 DEBUG にだけ本文を出す（既存の
    ``logger.debug("SOQL: %s", ...)`` と同ルール）。
    """
    main_object = extract_main_object(soql_text)
    if main_object is None:
        logger.warning(
            "SOQL から主オブジェクトを取り出せませんでした。"
            "API 名のまま保存します: 管理番号=%s, クラス=%s",
            entry.key,
            report_cls.__name__,
        )
        logger.debug("SOQL: %s", soql_text)
        return {}
    return load_column_names_for(main_object)


def _log_column_name_replacements(
    entry: ReportEntry,
    original_columns: list[str],
    report_cls: type,
    extra: dict[str, str],
) -> None:
    """``COLUMN_NAMES`` で置き換わった列数を出所別に数えて INFO を出す。

    手書きが ``column_names`` に勝ち、かつ wins が元と変わった列だけが
    「手書きで置換」扱い。 0 件 = 全部元の見出しのままなら出さない（ログを
    常に汚さないように）。 列名そのものは見出しに過ぎず、認証情報にもレポート
    の値にも触れないので INFO で出してよい。
    """
    file_replaced = 0
    manual_replaced = 0
    for col in original_columns:
        if col in report_cls.COLUMN_NAMES:
            if report_cls.COLUMN_NAMES[col] != col:
                manual_replaced += 1
        elif col in extra:
            if extra[col] != col:
                file_replaced += 1
    replaced_count = file_replaced + manual_replaced
    if replaced_count > 0:
        logger.info(
            "COLUMN_NAMES で %d 列を置き換えました（ファイル=%d, 手書き=%d）: 管理番号=%s",
            replaced_count,
            file_replaced,
            manual_replaced,
            entry.key,
        )


def _finalize_save(tmp_path: Path, base: Path, entry: ReportEntry) -> Path:
    """一時ファイルを最終パスへ移し、移し終わったパスを返す。

    **上書きモード:** ``{管理番号}_{HHMM}.csv`` の名前で毎回置き換える。 同じ
    ``HHMM`` なら同じパスになり、 翌日の同時刻に上書きされる。 ``os.replace`` は
    Windows でも移し先が存在するとそのまま上書きする（``os.rename`` と違い
    ``FileExistsError`` にならない）。 書き込みは既に ``_write_csv()`` で終わって
    いるので、 ここで失敗したら既存ファイルはそのまま残る契約（ ``except`` 経路
    で ``tmp_path`` だけ片付ける）。 連番探索は行わない。

    **通常モード:** 一時ファイルを最終パスへ ``os.rename``。 移し先が存在すれば
    次の連番候補を試す。 上書きはしない（``os.rename`` の Windows 仕様で
    ``FileExistsError``）。
    """
    if entry.overwrite:
        os.replace(tmp_path, base)
        return base
    return paths.move_into_place(tmp_path, base, entry.key)


def _verify_saved_file_not_empty(
    final_path: Path, table: Table, entry: ReportEntry
) -> None:
    """保存先が 0 バイトなら ``SavedFileEmptyError`` で落とす。

    保存先（ファイルサーバー）のクォータが足りないと、書き込みはエラーにならずに
    中身だけが捨てられ、0 バイトのファイルが残る（2026-10 の実例）。 空のファイル
    を成功扱いにしないための検査。 ``table.columns`` が空のときは見出しも無いので
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

    ``final_path`` が決まっていれば（ ``move_into_place`` が成功したあとに 0 バイト
    検査で落とすケース）削除する。 ``tmp_path`` は ``move_into_place`` に成功して
    いれば既に存在しない（ rename で消えた）ので ``missing_ok=True`` で安全。
    """
    if final_path is not None:
        final_path.unlink(missing_ok=True)
    tmp_path.unlink(missing_ok=True)


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ書いてから置き換える。

    複数のプロジェクトが同時に呼ぶので、直接書くと**読んでいる最中のファイルが
    半端な状態**になりうる。同じフォルダ内の置き換えは一度に入れ替わる。

    **注意:** ``_save()`` は 2026-10 からこの関数を **一時ファイル**
    （``paths.make_temp_csv_path()`` が返した ``~`` プレフィックスのパス）に対して
    呼び、最終パスへの ``os.replace`` は行わない（``paths.move_into_place()``
    が ``os.rename`` で移す。上書きしない）。``_write_csv()`` の引数は
    単に「**ここに書く**」パスで、本関数の動作は変えていない。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)

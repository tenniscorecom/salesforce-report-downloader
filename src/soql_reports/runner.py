r"""src/soql_reports/runner.py — SOQLレポートの取得実行。

    from src.soql_reports import (
        registered_reports,
        download_soql_reports,
    )

    saved = download_soql_reports()             # registered_reports() を全部
    saved = download_soql_reports([Large, ...])  # テスト用に取り違え

``download_scheduled()`` と同じく **1件失敗しても残りは続ける**。
戻り値は ``list[Path]``。定期取得（履歴 CSV 前提）の失敗用の例外は
``DownloaderError`` が SOQL 経路向けに担う（履歴前提のメッセージは
合わないため、SOQL 経路は専用文言にする）。

履歴（history.csv）への記録は **今回対象外**。``ReportEntry`` 前提の
``history.record()`` を無理に流用せず、まずは「取得して保存する」ところまで
を作る。履歴記録は、実際に使う場面が見えてから別途検討する。

**2026-10 に保存方式を ``src.service._save()`` と統一した。** 共通ヘルパー
``src.paths.move_into_place()`` が ``os.rename`` で一時ファイル（``~``
プレフィックス）を最終パスへ移し、既存ファイルがあるときは連番（``_1`` /
``_2`` …）で別名を確保する。本番の名前の空ファイルを一瞬も作らない。
詳細は ``src.paths.move_into_place()`` 参照。

このファイルが持つもの:
- SOQL レポートの取得・保存
- 1件失敗しても残りを続ける運用

ここに書かないもの:
- スケジュール判定 → 呼び出し側（プロジェクトの定期実行）
- 履歴への記録 → 後で別途
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from comken.core.dates import now
from comken.core.files import atomic_write
from comken.core.table.model import Table
from comken.exceptions import (
    ComkenError,
    DownloaderError,
)
from comken.runtime import is_dry_run
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.sites import site_for

from src.exceptions import (
    EmptyReportError,
    ReportFolderNotFoundError,
    SoqlColumnNameConflictError,
)
from src.paths import make_temp_csv_path, move_into_place, summary_folder_name
from src.soql_reports import _registry
from src.soql_reports.base import SoqlReport

logger = logging.getLogger(__name__)


def _soql_download_failed_error(failed_keys: list[str]) -> DownloaderError:
    """``DownloaderError`` の「SOQL レポートの取得で1件以上が失敗した」文言。

    発生箇所: src.soql_reports の download_soql_reports()
    """
    keys = "、".join(str(key) for key in failed_keys)
    return DownloaderError(
        f"SOQL レポートの取得で {len(failed_keys)} 件が失敗しました: {keys}\n"
        "失敗した管理番号について、SOQL クエリ・組織の認証情報・保存先フォルダの"
        "権限・ネットワークの状態を確認してください。"
        "\n対処: 表示された管理番号について、SOQL クエリ・組織の認証情報・保存先フォルダの"
        "権限・ネットワークの状態を確認してください。"
        "急いで必要なものは download_soql_reports() を直接実行してもよいです。"
    )


# ``move_into_place()`` が連番を足して空きファイル名を探索する回数の上限は
# ``src.paths.RESERVE_PATH_LIMIT`` に集約した（service / runner の両方から
# 同じ上限を使うため）。テストでは ``monkeypatch.setattr(paths_module,
# "RESERVE_PATH_LIMIT", ...)`` で差し替える。

# 保存名の書式。「管理番号_日付_時刻_マイクロ秒.csv」になる
_DATETIME_FORMAT = "%Y%m%d_%H%M%S_%f"


def download_soql_reports(
    reports: Sequence[type[SoqlReport]] | None = None,
) -> list[Path]:
    """登録された SOQL レポートを全て取得し、保存先のパスを返す。

    ``reports`` を省略すると ``registered_reports()`` を使う（テストでは差し替え可能）。
    **1件失敗しても残りは続ける**（``download_scheduled()`` と同じ方針）。

    想定した失敗（``ComkenError`` / ``OSError``）はログに残して次のレポートへ進む。
    想定外（``TypeError`` などのプログラムバグ）はそのまま伝播させ、気づける
    ようにする。1件でも失敗したら最後に ``DownloaderError`` を
    ``__cause__`` 付きで送出する。

    Args:
        reports: 取得対象の ``SoqlReport`` サブクラスのシーケンス。
            ``None`` のときは ``registered_reports()`` を使う。

    Returns:
        保存したファイルのパス一覧（**成功したぶんだけ**）。
    """
    targets = _registry.registered_reports() if reports is None else tuple(reports)
    logger.info("SOQL 取得の対象: %d 件", len(targets))

    saved: list[Path] = []
    failed: list[str] = []
    last_exception: BaseException | None = None
    for report_cls in targets:
        try:
            saved.append(_download(report_cls))
        except (ComkenError, OSError) as e:
            # **想定した失敗は続ける。想定していない失敗は止める。**
            # ``download_scheduled()`` と同じ判断:
            # - ``ComkenError`` は ``docs/ERRORS.md`` に対処法が載っている想定内の失敗なので続行
            # - ``OSError`` は共有サーバー断・権限・パスなど運用上の失敗
            # - それ以外（``TypeError`` など）は ``DownloaderError``
            #   （=「1件取れませんでした」）の顔で出てくると非エンジニアが
            #   「もう一度実行してみる」を繰り返すだけなので、捕捉せずその場で落とす
            logger.error("SOQL 取得に失敗しました: %s（%s）", report_cls.KEY, e)
            failed.append(report_cls.KEY)
            last_exception = e

    logger.info("SOQL 取得: %d 件中 %d 件を取得しました。", len(targets), len(saved))
    if failed:
        # 続けたぶん、最後に必ず知らせる（終了コードで落ちたことが分かるように）。
        # 直近の失敗を ``__cause__`` に乗せて送出する
        raise _soql_download_failed_error(failed) from last_exception
    return saved


def _download(report_cls: type[SoqlReport]) -> Path:
    """1件を取得して保存する。"""
    _require_folder(report_cls)
    table = _fetch(report_cls)
    return _save(report_cls, table)


def _require_folder(report_cls: type[SoqlReport]) -> None:
    """保存先フォルダが無ければ ``ReportFolderNotFoundError``。**勝手に作らない。**

    作らずに失敗させるのは ``service._require_folder()`` と同じ理由:
    無いのは書き間違いのことが多く、勝手に作ると誰も読まない場所へ
    置き続けることになる。
    """
    folder = Path(report_cls.FOLDER)
    if not folder.is_dir():
        raise ReportFolderNotFoundError(report_cls.KEY, folder)


def _fetch(report_cls: type[SoqlReport]) -> Table:
    """Salesforce へ問い合わせて明細表を返す。

    ``download_scheduled()`` と同じく、つなぐ組織は URL のドメインで決まる
    （``site_for()``）。サブクラス側で URL を間違えれば ``SalesforceError``
    で即座に気付ける。

    取得は ``SalesforceBase.bulk_query()`` で行う。SOQL 経由のレポートは
    件数が大きいものが多く、Bulk API 2.0 のサーバ側ジョブで往復回数を
    抑えられる。集計関数・``GROUP BY`` 等 Bulk API 2.0 が受け付けない
    SOQL はこの経路では使えない。
    """
    instance = report_cls()
    site = site_for(report_cls.URL)
    with site() as salesforce:
        return salesforce.bulk_query(instance.soql())


def _save(report_cls: type[SoqlReport], table: Table) -> Path:
    """行数と ``ALLOW_EMPTY`` に応じて保存先へ書き込み、書き終わったパスを返す。

    ``service._save()`` と同じ方針:
    0 行・``ALLOW_EMPTY`` × → ``EmptyReportError``（=失敗）／○ → 空 CSV を置く。
    0 行でも ``SalesforceBase.bulk_query()`` が ``Table.columns`` を持って返すので、
    見出し行だけ書いた空 CSV を保存する。

    **保存の流れ（2026-10 改定）:** ``service._save()`` と同じく、本番の名前の
    空ファイルを一瞬も作らない ``os.rename`` 方式 (``src.paths.move_into_place()``
    経由) に統一した。先に一時ファイル（``~`` プレフィックス）へ CSV を全部
    書いてから、 ``os.rename`` で最終パス（または連番 ``_1`` / ``_2`` …）へ移す。
    詳細は ``src.paths.move_into_place()`` の docstring 参照。

    **概要のフォルダは保存前に ``mkdir`` する。** ベース（``FOLDER``）は
    ``_require_folder()`` で先に検査済みなので ``parents=False`` で安全。
    dry-run 中は ``mkdir`` もしない（``service._save()`` と同じ防御）。
    dry-run はファイルも一時ファイルも作らない（``base`` をそのまま返す）。
    """
    base = _file_path_of(report_cls)
    # **概要のフォルダは保存前に ``mkdir`` する。**
    if not is_dry_run():
        base.parent.mkdir(exist_ok=True)
    tmp_path = make_temp_csv_path(base)
    final_path: Path | None = None
    try:
        if not table and not report_cls.ALLOW_EMPTY:
            raise EmptyReportError(report_cls.KEY, report_cls.SUMMARY, report_cls.URL)
        if is_dry_run():
            # dry-run は副作用を出さない契約
            return base
        # 保存の直前に ``COLUMN_NAMES`` を適用する。空なら何もしない。
        # 0 行でも ``table.columns`` はあるので見出しだけ置き換わる（``_write_csv``
        # 側で空 CSV として書かれる）。 ``ALLOW_EMPTY`` 判定は元の ``table`` で
        # 済ませているので、ここで ``table`` を差し替えても判定は壊れない。
        table = _apply_column_names(report_cls, table)
        _write_csv(tmp_path, table)
        final_path = move_into_place(tmp_path, base, report_cls.KEY)
    except Exception:
        # 失敗時: 移せていれば最終ファイル、後始末
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)
        raise
    return final_path


def _file_path_of(report_cls: type[SoqlReport]) -> Path:
    """そのレポートを保存するパス。

    フォルダは ``FOLDER / 概要`` の 2 階層、ファイル名は「管理番号_日付_時刻_マイクロ秒」。
    **管理番号を先頭に置く**のは、概要や参照先のレポートが変わっても番号は
    変わらないため。拡張子は ``.csv``。概要はフォルダ名にだけ使い、ファイル名には
    混ぜない（``src.paths.summary_folder_name()`` で安全化し、定期取得側と
    同じ規則でフォルダ名を作る）。
    """
    return (
        Path(report_cls.FOLDER)
        / summary_folder_name(report_cls.SUMMARY)
        / f"{report_cls.KEY}_{now():{_DATETIME_FORMAT}}.csv"
    )


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ書いてから置き換える。

    ``service._write_csv()`` と同じ組み立て方。複数のプロジェクトが同時に呼ぶので、
    直接書くと**読んでいる最中のファイルが半端な状態**になりうる。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)


def _apply_column_names(report_cls: type[SoqlReport], table: Table) -> Table:
    """``SoqlReport.COLUMN_NAMES`` に従って SOQL の結果列を人が読む見出しへ置き換える。

    ``COLUMN_NAMES`` が空なら ``table`` をそのまま返す（従来どおりの出力）。
    結果の列に無いキーは無視する（SOQL を後から変えても対応表を貼り直さずに
    済むように）。列の並び順は元のまま。置き換え後の見出しが 2 つ以上
    重なる場合は ``SoqlColumnNameConflictError`` を送出する（CSV は書かれ
    ない）。
    """
    column_names = report_cls.COLUMN_NAMES
    if not column_names:
        return table
    # 元の列順を保ったまま、``COLUMN_NAMES`` にある列だけ表示名へ差し替える。
    new_columns = [column_names.get(col, col) for col in table.columns]
    # 衝突の見出しを先に集める（``table.select(...)`` に進む前に弾く）。
    # 重複の判定は ``new_columns`` 全体に対して行う。``COLUMN_NAMES``
    # に登録されていない列も、置き換え後の見出しが他と重なれば衝突になる
    # （CSV に同じ列名が 2 回現れて何が何だかにするため）。 ``sources``
    # には元の列名をそのまま入れる（登録しなかった列も「元はどの列だったか」
    # 衝突の解消で困らないよう含める）。
    conflicts: dict[str, list[str]] = {}
    for original, new in zip(table.columns, new_columns, strict=True):
        conflicts.setdefault(new, []).append(original)
    duplicated = {header: sources for header, sources in conflicts.items() if len(sources) >= 2}
    if duplicated:
        raise SoqlColumnNameConflictError(report_key=report_cls.KEY, conflicts=duplicated)
    # ``table.select(*new_columns, aliases=aliases)`` は new の列名で並べ、
    # ``aliases`` にある new だけ既存の old 名の列から値を拾う。
    # 結果に無いキーは無視したいので、 ``table.columns`` に含まれる old だけ aliases に入れる。
    aliases = {new: original for original, new in column_names.items() if original in table.columns}
    return table.select(*new_columns, aliases=aliases)

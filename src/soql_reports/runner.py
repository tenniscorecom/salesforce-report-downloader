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

from comken.core.files import DateNameBuilder, atomic_write
from comken.core.table.model import Table
from comken.exceptions import (
    ComkenError,
)
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.sites import site_for

from src.exceptions import (
    DownloaderError,
    EmptyReportError,
    ReportFolderNotFoundError,
    ReportReservePathLimitError,
)
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


# ``_reserve_path`` が連番を足して空きファイル名を探索する回数の上限。
# Salesforceレポートダウンローダー の ``service.RESERVE_PATH_LIMIT`` と同じ
# 理由: 共有サーバーの同期・権限異常で ``FileExistsError`` が返り続けると
# 無限ループになるため、必ず上限を切る
RESERVE_PATH_LIMIT = 1000

# ファイル名に使えない文字。概要をファイル名に混ぜるので、ここで落とす
_FORBIDDEN_IN_NAME = '\\/:*?"<>|'
# 概要が長いとパスが伸びすぎるので、ファイル名に使うのはこの長さまで
_SUMMARY_LIMIT = 30
# ``DateNameBuilder.suffix()`` に渡す書式。「管理番号_概要_日付_時刻_マイクロ秒.csv」になる
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
    """
    instance = report_cls()
    site = site_for(report_cls.URL)
    with site() as salesforce:
        return salesforce.query(instance.soql())


def _save(report_cls: type[SoqlReport], table: Table) -> Path:
    """行数と ``ALLOW_EMPTY`` に応じて保存先へ書き込み、書き終わったパスを返す。

    ``service._save()`` と同じ方針:
    0 行・``ALLOW_EMPTY`` × → ``EmptyReportError``（=失敗）／○ → 空 CSV を置く。
    0 行でも ``SalesforceBase.query()`` が ``Table.columns`` を持って返るので、
    見出し行だけ書いた空 CSV を保存する。
    """
    path = _reserve_path(report_cls)
    try:
        if not table and not report_cls.ALLOW_EMPTY:
            raise EmptyReportError(report_cls.KEY, report_cls.SUMMARY, report_cls.URL)
        _write_csv(path, table)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _reserve_path(report_cls: type[SoqlReport]) -> Path:
    """排他的な新規作成で保存名を予約し、既存ファイルを上書きしない。

    同じフォルダに既存ファイルがあると連番（ ``_1`` / ``_2`` …）を足して別の
    ファイル名を探す。 ``RESERVE_PATH_LIMIT`` を超えると ``ReportReservePathLimitError``
    を送出する（権限・同期の異常で ``FileExistsError`` が返り続ける無限ループを
    避けるため）。``service._reserve_unique_path()`` と同じアルゴリズム。
    """
    base_path = _file_path_of(report_cls)
    candidate = base_path
    sequence = 0
    for _ in range(RESERVE_PATH_LIMIT):
        try:
            candidate.open("x").close()
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = base_path.with_stem(f"{base_path.stem}_{sequence}")
    raise ReportReservePathLimitError(report_cls.KEY, base_path, RESERVE_PATH_LIMIT)


def _file_path_of(report_cls: type[SoqlReport]) -> Path:
    """そのレポートを保存するパス。

    ファイル名は「管理番号_概要_日付_時刻_マイクロ秒」。**管理番号を先頭に置く**のは、
    概要や参照先のレポートが変わっても番号は変わらないため。拡張子は ``.csv``。
    """
    name = f"{report_cls.KEY}_{_safe_summary(report_cls.SUMMARY)}.csv"
    return Path(report_cls.FOLDER) / DateNameBuilder(name).suffix(_DATETIME_FORMAT)


def _safe_summary(summary: str) -> str:
    """概要をファイル名に使える形にする。

    ファイル名に使えない文字を除き、``_SUMMARY_LIMIT`` 文字までに切る
    （空になったら「レポート」）。
    """
    cleaned = "".join(char for char in summary if char not in _FORBIDDEN_IN_NAME).strip()
    return cleaned[:_SUMMARY_LIMIT] or "レポート"


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ書いてから置き換える。

    ``service._write_csv()`` と同じ組み立て方。複数のプロジェクトが同時に呼ぶので、
    直接書くと**読んでいる最中のファイルが半端な状態**になりうる。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)

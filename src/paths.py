"""src/paths.py — 管理表の置き場所と、保存先パスの組み立て。

`download_scheduled()` が落とした CSV を置く**唯一の保存先パス**を組み立てる関数と、
グループ設定のプロセス内キャッシュを置く。

**import 時に `requests` を読まない**（BO 環境で動かす前提）。

出力パスは ``ベースパス / 概要 /{管理番号}_{スケジュール時刻:%Y%m%d_%H%M}.csv``。
``summary`` はフォルダ名にだけ使い、ファイル名には混ぜない。``assignee`` は使わない。

保存は「一時ファイル（``~`` プレフィックス）に書いてから ``os.rename`` で移す」方式。
移し先が既にあれば ``_1`` / ``_2`` … と連番を足す（``move_into_place()``）。
本番の名前の空ファイルを一瞬も作らない。

このファイルが持つもの:
- `output_path` / `report_folder` / `move_into_place`
- グループ設定のプロセス内キャッシュ
- 置き場所の定数（`MASTER_PATH`、`COLUMN_NAMES_CSV_PATH`）

ここに書かないもの:
- Salesforce への問い合わせ → src.service
- 履歴の記録・形式・読み取り・ロック・置き場所 → src.history / comken.services.salesforce_downloader
- 管理表・設定シートの読み込み（列定義・雛形・検査）→ src.sheets.* / src.report_master
- ``列名の対応.csv`` の読み込み → src.soql_reports.column_names
"""

import datetime as dt
import logging
import os
import uuid
from collections import OrderedDict
from pathlib import Path

from comken.core.dates import now as clock_now
from comken.exceptions import DownloaderError
from comken.services.salesforce_downloader.paths import SALESFORCE_DOWNLOADER_FOLDER

from src.exceptions import GroupNotRegisteredError
from src.sheets.master import ReportEntry

logger = logging.getLogger(__name__)


# ── 管理表の置き場所 ───────────────────────────────────────────
# config ファイルへ外出ししない: 置き場所は配置時にエンジニアが決める値で、
# config.ini には非エンジニアが変える値だけを置く方針。
#
# **共有フォルダ（`SALESFORCE_DOWNLOADER_FOLDER`）は comken 側の定数を使う。**
# 履歴の `HISTORY_PATH` も同じ定数から作られており、ここに同じ定数を持つと
# 履歴と管理表が別のフォルダを指す事故になる。
MASTER_FILENAME = "レポート管理表.xlsx"

# レポート管理表の絶対パス。テストでは `monkeypatch.setattr` で tmp_path に差し替える。
MASTER_PATH = SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME

# ``soql-collector``（別プロジェクト）が出す列名の対応 CSV の置き場所。
# 全レポート分が 1 ファイルに積み上がる形式で、SOQL の ``FROM <名前>`` の主オブジェクトで引く。
# config.ini には出さない（``MASTER_PATH`` と同じ理由）。テストでは差し替える。
COLUMN_NAMES_CSV_PATH = SALESFORCE_DOWNLOADER_FOLDER / "soql-collector" / "列名の対応.csv"


# ── グループ設定のプロセス内キャッシュ ─────────────────────────────────────
# `report_folder()` は管理表の1件ごとに呼ばれる。毎回 `load_group_settings()` を
# 走らせると設定シートを N 回開くので、解決済み絶対パスをキーに保持する。
# 同じパスのファイルを書き換えても反映されない（`stat()` による更新確認はしない）。
_GROUP_SETTINGS_CACHE_MAXSIZE = 16
_group_settings_cache: OrderedDict[str, dict[str, Path]] = OrderedDict()


# ── 概要からフォルダ名を作る ───────────────────────────────────────
# 定期取得と SOQL レポートの両方がこの関数を使う。ファイル名には概要を混ぜない。
_FORBIDDEN_IN_FOLDER_NAME = r'\/:*?"<>|'
_SUMMARY_FOLDER_LIMIT = 30


def summary_folder_name(summary: str) -> str:
    """概要から安全なフォルダ名を作る。

    ``output_path()`` と SOQL レポートの両方から呼ばれる共通ヘルパー。

    ルール: ``\\ / : * ? " < > |`` を取り除く → 30 文字で切る →
    末尾の ``.`` と空白を取り除く → 空なら「レポート」。
    """
    cleaned = "".join(char for char in summary if char not in _FORBIDDEN_IN_FOLDER_NAME)
    truncated = cleaned[:_SUMMARY_FOLDER_LIMIT].rstrip(" .")
    return truncated or "レポート"


# ── 保存ファイル名の連番上限 ──────────────────────────────────────
# ``move_into_place()`` が連番を足して空きを探す回数の上限。共有サーバーの異常で
# ``FileExistsError`` が返り続けても無限ループにならないようにする。
RESERVE_PATH_LIMIT = 1000


def make_temp_csv_path(target: Path) -> Path:
    """``target`` と同じフォルダに置く一時ファイル名を作る。``~`` で始めて
    ``1001_*.csv`` のような glob で拾われないようにし、UUID で衝突を避ける。

    ファイルは作らない（パスだけを返す）。
    """
    return target.with_name(f"~{target.stem}.{uuid.uuid4().hex}{target.suffix}")


def move_into_place(tmp_path: Path, base_path: Path, report_key: str) -> Path:
    """一時ファイルを最終パスへ ``os.rename`` で移す（**上書きしない**）。

    同じフォルダに既存ファイルがあると連番（``_1`` / ``_2`` …）を足して別のファイル名を
    探す。``RESERVE_PATH_LIMIT`` を超えると ``DownloaderError``。先に一時ファイルへ全部
    書いてから移すので、本番の名前の空ファイルは一瞬も作らない。

    Args:
        tmp_path: 既に内容物が書かれている一時ファイルのパス（呼び出し側が
            作った ``~`` プレフィックスのファイル）。
        base_path: 本番の保存先候補パス。既に同名ファイルがある可能性がある。
        report_key: エラーメッセージに含める管理番号。

    Returns:
        実際に移った先のパス。``base_path`` か ``base_path_1.csv`` /
        ``base_path_2.csv`` … のいずれか。

    Raises:
        DownloaderError: ``RESERVE_PATH_LIMIT`` 回試しても空きが
            見つからなかった場合。
    """
    candidate = base_path
    sequence = 0
    for _ in range(RESERVE_PATH_LIMIT):
        try:
            os.rename(tmp_path, candidate)
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = base_path.with_stem(f"{base_path.stem}_{sequence}")
    raise DownloaderError(
        f"保存ファイル名の連番が上限に達しました: {report_key}\n"
        f"{base_path}\n"
        f"{RESERVE_PATH_LIMIT} 回試しても空きのファイル名が見つかりませんでした。\n"
        "保存先フォルダの権限・排他制御と、 古い取得ファイルの数を確認してください。"
    )


def output_path(
    entry: ReportEntry,
    schedule_run_time: dt.time | None = None,
    *,
    now: dt.datetime | None = None,
) -> Path:
    """レポートの保存先パス（唯一の出力先）を返す。

    フォルダは ``report_folder()``（ベースパス）/ ``summary_folder_name(entry.summary)``
    の 2 階層。ファイル名は次のどちらか:

    - ``entry.overwrite`` が ``False``（既定）: ``{管理番号}_{YYYYMMDD}_{HHMM}.csv``
      （衝突時は ``service.save_report()`` が連番で別名にする）
    - ``entry.overwrite`` が ``True``: ``{管理番号}_{HHMM}.csv``
      （同じ ``HHMM`` なら翌日に上書きされる。日付は入らない）

    時刻は ``schedule_run_time``（スケジュール行の希望時刻）を今日の日付と合成して使い、
    無ければ ``now``（省略時は現在時刻）を使う。スケジュール行が無いレポートは取得しない
    ので、上書きモードでは ``schedule_run_time`` が必ず渡る。

    フォルダは作らないし検査もしない（保存時に呼び出し側が ``mkdir``、検査は
    ``service.require_folder``）。

    Args:
        entry: レポート管理表の1行。
        schedule_run_time: 今回の取得の根拠になったスケジュール行の「取得時刻」
            （``ScheduleRule.desired_time``）。判定には使われない記録用の希望時刻で、
            ファイル名の ``HHMM`` 部（``overwrite=True`` のとき）または
            ``YYYYMMDD_HHMM`` 部（``overwrite=False`` のとき）に使う。無ければ
            ``now`` にフォールバックする。
        now: ``schedule_run_time`` が無いときに使う時刻。省略時は現在時刻
            （``comken.core.dates.now()`` を使う）。

    Raises:
        GroupNotRegisteredError: 設定シートにないグループ名の場合（``report_folder()`` 経由）。
    """
    folder = base_folder(entry)
    current = now if now is not None else clock_now()
    if schedule_run_time is not None:
        base_dt = dt.datetime.combine(current.date(), schedule_run_time)
    else:
        base_dt = current
    if entry.overwrite:
        # 上書きモード: 同じ ``HHMM`` なら同じパスになる（連番なし・日付なし）
        return (
            folder
            / summary_folder_name(entry.summary)
            / f"{entry.key}_{base_dt.strftime('%H%M')}.csv"
        )
    return (
        folder
        / summary_folder_name(entry.summary)
        / f"{entry.key}_{base_dt.strftime('%Y%m%d_%H%M')}.csv"
    )


def base_folder(entry: ReportEntry) -> Path:
    """管理表の1行から、保存先のベースフォルダを返す。

    設定シート（管理表と同じブック内）の「グループ→ベースパス」を引いて
    ``report_folder()`` に委譲する。フォルダは作らないし検査もしない。
    ``MASTER_PATH`` は呼び出し時点のモジュール変数を参照する。

    Args:
        entry: レポート管理表の1行。

    Returns:
        保存先のベースフォルダの ``Path``。

    Raises:
        GroupNotRegisteredError: ``entry.group`` が設定シートに無い場合。
    """
    return report_folder(entry, _load_group_settings_cached(MASTER_PATH))


def report_folder(entry: ReportEntry, group_settings: dict[str, Path]) -> Path:
    """管理表の1行と設定シートから、保存先フォルダを組み立てる。

    組み立てルールは **設定シートのベースパス** のみ。管理表の `assignee` / `summary` は
    出力パスには使わない。

    Args:
        entry: レポート管理表の1行。
        group_settings: ``{グループ名: ベースパス}`` の辞書
            （``load_group_settings()`` の戻り値）。

    Returns:
        保存先フォルダの ``Path``。

    Raises:
        GroupNotRegisteredError: ``entry.group`` が ``group_settings`` に無い場合。
    """
    base_path = group_settings.get(entry.group)
    if base_path is None:
        raise GroupNotRegisteredError(entry.group, sorted(group_settings), MASTER_PATH)
    return base_path


def _load_group_settings_cached(master_path: Path) -> dict[str, Path]:
    """``load_group_settings()`` を ``Path.resolve()`` 後の絶対パスでキャッシュする。

    ``_GROUP_SETTINGS_CACHE_MAXSIZE`` を超えたら古いものから捨てる。
    ``stat()`` による更新確認は行わない。
    """
    from src.sheets.group_settings import load_group_settings

    resolved_key = str(master_path) if master_path.is_absolute() else str(master_path.resolve())
    cache = _group_settings_cache
    if resolved_key in cache:
        cache.move_to_end(resolved_key)
        return cache[resolved_key]
    settings = load_group_settings(Path(master_path))
    cache[resolved_key] = settings
    cache.move_to_end(resolved_key)
    if len(cache) > _GROUP_SETTINGS_CACHE_MAXSIZE:
        cache.popitem(last=False)
    return settings


def reset_group_settings_cache() -> None:
    """グループ設定のプロセス内キャッシュを破棄する（テスト用）。

    ``tmp_path`` ごとに管理表を差し替えるテストで、前のテストの設定が漏れないようにする。
    """
    _group_settings_cache.clear()

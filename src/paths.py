"""src/paths.py — 管理表の置き場所と、保存先パスの組み立て。

`download_scheduled()` が Salesforce から落とした CSV を置く**唯一の保存先パス**を
組み立てる関数と、グループ設定のプロセス内キャッシュを置く。

**import 時に `requests` を読まない**（BO 環境で動かす前提）。
`comken.toolbox.salesforce` に依存しないことで、`requests` の入っていない環境でも
このモジュールだけを使えるようにする。

**出力パスの組み立ては 1 本化した。** フォルダは ``ベースパス / 概要 /`` の
2 階層、ファイル名は ``{管理番号}_{スケジュール時刻:%Y%m%d_%H%M}.csv``。
``assignee`` は管理表に残してあるが、出力パスには使わない。``summary``
はフォルダ階層の 1 段目にだけ使う（人が業務ごとに探しやすくするため。
ファイル名には混ぜない）。

**保存は「一時ファイル（``~`` プレフィックス）に書いてから ``os.rename``
で移し先へ移す」方式。** 移し先が既に存在するときは ``_1`` / ``_2`` … と
連番を足して別名を確保する。``move_into_place()`` が共通ヘルパー。
これにより **本番の名前の空ファイルを一瞬も作らない** 保存になる。
詳細は ``move_into_place()`` の docstring 参照。

**履歴CSVの置き場所は `comken.services.salesforce_downloader.paths.HISTORY_PATH`
に移した（境界は履歴）— このファイルは管理表だけを管理する。** 履歴の形式や
ロック・読み取り関数も同じく comken 側にあるので、ダウンローダーは自分で持たず
comken の `append_history()` へ委譲する。

このファイルが持つもの:
- レポートの唯一の保存先パスを組み立てる `output_path`
- 設定シートから保存先フォルダを返す `report_folder`
- 一時ファイル→最終パスのリネーム（連番フォールバック付き）`move_into_place`
- グループ設定のプロセス内キャッシュ
- 管理表の置き場所の定数（`MASTER_PATH`）
- ``soql-collector`` の出力（列名対応 CSV）の置き場所 ``COLUMN_NAMES_CSV_PATH``

ここに書かないもの:
- Salesforce への問い合わせ → src.service
- 履歴への記録 → src.history（comken の `append_history()` を呼ぶだけ）
- 履歴の形式・読み取り・ロック・置き場所 → comken.services.salesforce_downloader
- 管理表の読み込み（列定義・雛形・検査）→ src.sheets.master / src.report_master
- 設定シート（グループ→ベースパス）の列定義 → src.sheets.group_settings
- ``列名の対応.csv`` の読み込み・キャッシュ → src.soql_reports.column_names
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
# レポート管理表（Excel）。非エンジニアが編集する。検査は `python -m src.cli check`。
# 雛形は `src.template_writer.create_combined_workbook()` などで生成する。
# config ファイルへ外出ししない: 非エンジニアが変えるのは管理表の中身で、置き場所は
# 配置時にエンジニアが決める値なので、コードに置く（config.ini には非エンジニアが
# 変える値だけを置く方針）。
#
# **共有フォルダ（`SALESFORCE_DOWNLOADER_FOLDER`）は comken 側の定数を使う。**
# 履歴（CSV）の置き場 `HISTORY_PATH` も同じ定数から作られているので、配置時に
# 書き換えるのは comken の `services/salesforce_downloader/paths.py` の 1 か所だけ
# （ここに同じ定数を持つと、履歴と管理表が別のフォルダを指す事故になる）。
#
# ファイル名だけ変えたいときのため、ファイル名定数を分けて、下のパス定数で組み立てる。
MASTER_FILENAME = "レポート管理表.xlsx"

# レポート管理表の絶対パス（モジュール変数）。テストでは `paths.MASTER_PATH`
# を `monkeypatch.setattr` で tmp_path に差し替えて運用する。
MASTER_PATH = SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME

# ``soql-collector``（別プロジェクト）が出す列名の対応 CSV の置き場所。
# 1 ファイルに全レポート分が積み上がる形式なので、管理番号ごとのファイルは
# 持たない（``src.soql_reports.column_names`` が SOQL の ``FROM <名前>`` で
# 取り出した **主オブジェクト** で引く）。
# config.ini には出さない方針（``MASTER_PATH`` と同じ。配置時にエンジニアが
# 決める値で、頻繁に動かすものではないため）。
# テストでは ``monkeypatch.setattr(paths_module, "COLUMN_NAMES_CSV_PATH", ...)``
# で ``tmp_path`` 配下に差し替える運用。
COLUMN_NAMES_CSV_PATH = SALESFORCE_DOWNLOADER_FOLDER / "soql-collector" / "列名の対応.csv"


# ── グループ設定のプロセス内キャッシュ ─────────────────────────────────────
# `report_folder()` は管理表の1件ごとに呼ばれる。毎回 `load_group_settings()` を
# 走らせると設定シートを N 回開くので、解決済み絶対パスをキーに保持する。
# 同じパスのファイルを書き換えても反映されない（`stat()` による更新確認はしない）。
# 上限を超えたら古いものから捨てる（テストで大量の ``tmp_path`` を読んでも膨らまない）。
_GROUP_SETTINGS_CACHE_MAXSIZE = 16
_group_settings_cache: OrderedDict[str, dict[str, Path]] = OrderedDict()


# ── 概要からフォルダ名を作る ───────────────────────────────────────
# 定期取得と SOQL レポートの両方がこの関数を使う（``src.soql_reports.runner`` 側に
# 同じ目的の関数を 2 つ持たないため）。ファイル名には概要を混ぜず、フォルダ名として
# のみ使うので、ここで Windows のファイル名規則に従って安全化する。
#
# - 使えない文字（``\ / : * ? " < > |``）は取り除く
# - 長さは ``_SUMMARY_FOLDER_LIMIT`` 文字まで
# - 空になったら「レポート」
# - **Windows のフォルダ名として使えない「末尾の ``.`` と空白」も取り除く**
#   （取り除いて空になったら「レポート」）
_FORBIDDEN_IN_FOLDER_NAME = r'\/:*?"<>|'
_SUMMARY_FOLDER_LIMIT = 30


def summary_folder_name(summary: str) -> str:
    """概要から安全なフォルダ名を作る。

    ``output_path()`` と SOQL レポートの ``_file_path_of()`` の両方から呼ばれる
    共通ヘルパー。Windows のファイル名規則に従って安全化し、パスが伸びすぎない
    ように ``_SUMMARY_FOLDER_LIMIT`` 文字で切る。

    ルール:
    1. ``\\ / : * ? " < > |`` を取り除く
    2. 30 文字で切る
    3. 末尾の ``.`` と空白を取り除く
    4. 空になったら「レポート」
    """
    cleaned = "".join(char for char in summary if char not in _FORBIDDEN_IN_FOLDER_NAME)
    truncated = cleaned[:_SUMMARY_FOLDER_LIMIT].rstrip(" .")
    return truncated or "レポート"


# ── 保存ファイル名の連番上限 ──────────────────────────────────────
# ``move_into_place()`` が連番を足して空きファイル名を探索する回数の上限。
# ``comken.core.dates.WORKDAY_SEARCH_LIMIT`` と同じ理由で、共有サーバーの
# 同期・権限異常などで ``FileExistsError`` が返り続けると無限ループになる
# ため、必ず上限を切る
RESERVE_PATH_LIMIT = 1000


def make_temp_csv_path(target: Path) -> Path:
    """``target`` と同じフォルダに置く一時ファイル名を作る。``~`` で始めて
    ``1001_*.csv`` のような glob で拾われないようにし、衝突回避のため UUID
    を入れる（``comken.core.files._new_temp_name`` と同じ命名規則）。

    ファイルは作らない（パスだけを返す）。呼び出し側が ``CSV(tmp)`` などで
    開いたときに初めて作られる。
    """
    return target.with_name(f"~{target.stem}.{uuid.uuid4().hex}{target.suffix}")


def move_into_place(tmp_path: Path, base_path: Path, report_key: str) -> Path:
    """一時ファイルを最終パスへ ``os.rename`` で移す（**上書きしない**）。

    同じフォルダに既存ファイルがあると連番（ ``_1`` / ``_2`` …）を足して別の
    ファイル名を探す。 ``RESERVE_PATH_LIMIT`` を超えると ``DownloaderError``
    を送出する（権限・同期の異常で ``FileExistsError`` が返り続ける無限ループを
    避けるため）。

    なぜこの方式か:
        **先に一時ファイルへ全部書いてから** ``os.rename``
        （Windows では既存があると ``FileExistsError``）で移す。失敗時は次の
        連番候補を試す。最終的に移ったパスを返す（呼び出し側で 0 バイト検査を
        行う）。本番の名前の空ファイルを **一瞬も作らない** ので、予約ファイル
        方式の問題（書き込み中も本番の名前で見えてしまう／クォータ不足で
        0 バイトファイルが残る）を避けられる。

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
    （概要のフォルダ名）の 2 階層。ファイル名は次のどちらか:

    - ``entry.overwrite`` が ``False``（既定）: ``{管理番号}_{YYYYMMDD}_{HHMM}.csv``
      （毎回新しいファイル名。衝突時は呼び出し側 ``service.save_report()`` が連番で別名にする）
    - ``entry.overwrite`` が ``True``: ``{管理番号}_{HHMM}.csv``（同じ ``HHMM`` なら
      翌日に同じファイル名で上書きされる。時刻だけ。日付はファイル名に入らない）

    どちらも時刻の組み立て方は同じ。``schedule_run_time``（今回の取得の根拠になった
    スケジュール行の「取得時刻」、``ScheduleRule.desired_time`` の値。記録用の希望時刻）
    があれば今日の日付と合成、無ければ ``now``（省略時は現在時刻）を使う。
    上書きモードはこの値から ``HHMM`` だけ取り出してファイル名にする。
    **スケジュール行が無いレポートは取得しない**（``service.download_scheduled()``）
    ので、上書きモードのときは ``schedule_run_time`` が必ず渡る。

    **概要のフォルダは保存時に呼び出し側が ``mkdir`` で作る**（この関数は作らない）。
    ベースのフォルダは作らないし検査もしない（検査は ``service.require_folder`` の責務）。

    ``overwrite`` が ``False`` のときは常に新規ファイルとして扱う（同じパスへの上書きは
    想定しない。衝突回避は呼び出し側 ``service.save_report()`` の ``move_into_place`` の責務）。
    ``move_into_place()`` は書き終えた一時ファイルを ``os.rename`` で移すので、
    本番の名前の空ファイルは作らない。
    ``overwrite`` が ``True`` のときは ``HHMM`` 単位で上書きする（``service.save_report()``
    が ``os.replace`` で同名ファイルを上書きする。連番は付かない）。

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
        # **上書きモード:** ``{管理番号}_{HHMM}.csv`` の名前で ``os.replace`` を
        # かける（=同じ ``HHMM`` なら同じパスになり、翌日の同時刻に上書きされる）。
        # 連番は付けない。呼び出し側 ``service.save_report()`` が ``os.replace`` で
        # 同名ファイルを上書きする。日付はファイル名に入らない
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
    ``report_folder()`` に委譲する。フォルダは作らないし検査もしない
    （存在検査は ``service.require_folder`` の責務）。

    ``MASTER_PATH`` は呼び出し時点のモジュール変数を参照する（テストでは
    ``monkeypatch.setattr(paths, "MASTER_PATH", ...)`` で差し替える）。

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

    組み立てルールは **「設定シートのベースパス」** のみ。 管理表の `assignee`
    / `summary` は**出力パスには使わない**（管理表には残してあっても、フォルダ
    階層には影響しない）。 Excel の数式で組み立てる案は openpyxl が数式セルを
    信頼できないため採用せず、Python 側で連結する。

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

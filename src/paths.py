"""src/paths.py — 管理表の置き場所と、保存先パスの組み立て。

`download_scheduled()` が Salesforce から落とした CSV を置く**唯一の保存先パス**を
組み立てる関数と、管理表・グループ設定のプロセス内キャッシュを置く。

**import 時に `requests` を読まない**（BO 環境で動かす前提）。
`comken.toolbox.salesforce` に依存しないことで、`requests` の入っていない環境でも
このモジュールだけを使えるようにする。

**2026-09 に出力パスの組み立てを 1 本化した。** フォルダは設定シートのベースパスのみ、
ファイル名は ``{管理番号}_{スケジュール時刻:%Y%m%d_%H%M}.csv``。``assignee`` /
``summary`` は管理表に残してあるが、出力パスには使わない。

**履歴CSVの置き場所は `comken.services.salesforce_downloader.paths.HISTORY_PATH`
に移した（境界は履歴）— このファイルは管理表だけを管理する。** 履歴の形式や
ロック・読み取り関数も同じく comken 側にあるので、ダウンローダーは自分で持たず
comken の `append_history()` へ委譲する。

このファイルが持つもの:
- レポートの唯一の保存先パスを組み立てる `output_path`
- 設定シートから保存先フォルダを返す `report_folder`
- 管理表・グループ設定のプロセス内キャッシュ
- 管理表の置き場所の定数（`MASTER_PATH`）

ここに書かないもの:
- Salesforce への問い合わせ → src.service
- 履歴への記録 → src.history（comken の `append_history()` を呼ぶだけ）
- 履歴の形式・読み取り・ロック・置き場所 → comken.services.salesforce_downloader
- 管理表の読み込み（列定義・雛形・検査）→ src.sheets.master / src.report_master
- 設定シート（グループ→ベースパス）の列定義 → src.sheets.group_settings
"""

import datetime as dt
import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from comken.core.dates import now as clock_now

from src.exceptions import (
    DownloaderError,
    GroupNotRegisteredError,
    ReportNotRegisteredError,
)

if TYPE_CHECKING:
    from src.sheets.master import ReportEntry

logger = logging.getLogger(__name__)


# ── 管理表の置き場所 ───────────────────────────────────────────
# レポート管理表（Excel）。非エンジニアが編集する。検査は `python -m src.cli check`。
# 雛形は `src.template_writer.create_combined_workbook()` などで生成する。
# config ファイルへ外出ししない: 非エンジニアが変えるのは管理表の中身で、置き場所は
# 配置時にエンジニアが決める値なので、コードに置く（config.ini には非エンジニアが
# 変える値だけを置く方針）。
#
# **履歴（CSV）の置き場はここには無い。** 境界は履歴にしたので、
# comken 側の `comken.services.salesforce_downloader.paths.HISTORY_PATH` を
# 使う（書き換えるのは comken 側 1 か所だけ）。
#
# フォルダを変えたい／ファイル名だけ変えたいときのため、フォルダ定数とファイル名
# 定数を分けて、下のパス定数で組み立てる。
SALESFORCE_DOWNLOADER_FOLDER = Path(r"\\server\share\tools\salesforce")

MASTER_FILENAME = "レポート管理表.xlsx"

# レポート管理表の絶対パス（モジュール変数）。テストでは `paths.MASTER_PATH`
# を `monkeypatch.setattr` で tmp_path に差し替えて運用する。
MASTER_PATH = SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME


def _report_disabled_error(report_key: str, summary: str, master_path: Path) -> DownloaderError:
    """``DownloaderError`` の「管理表で『無効』になっているレポートを取ろうとした」文言。

    発生箇所: src.paths の `_find()`
    """
    return DownloaderError(
        f"このレポートは無効になっています: {report_key}（{summary}）\n"
        f"管理表: {master_path}\n"
        "また使うなら「有効」列を有効に戻してください。"
        "\n対処: また使うなら管理表の「有効」を「有効」に戻してください。"
        "使わないなら、呼び出し側のコードから消してください。"
    )


# ── 管理表のプロセス内キャッシュ ──────────────────────────────────────
# `_find()` が `output_path()` などの公開 API から呼ばれるたびに
# `load_master()` を実行すると、**ループの中でレポートを N 件取ると
# 管理表 Excel を N 回開く**ことになる。 管理表は 1 回の実行中に変わらないので、
# **解決済み絶対パス**をキーにプロセス内キャッシュを持つ（``Config(path)``
# と同じ考え方: 相対パスと絶対パスを同一視し、``Path.resolve()`` の Windows UNC /
# 8.3 形式の揺れも吸収する）。
#
# キャッシュの無効化条件:
# - **同じパスのファイルを書き換えても反映されない。** ``stat()`` による更新
#   確認は意図的に行わない（``Config`` と同じ理由:  Windows で 1 回 25
#   マイクロ秒、共有サーバーではネットワーク往復。 業務ツールは実行中の
#   管理表の書き換えを想定しない）。
# - 反映が必要なときは ``_reset_cached_master()`` を呼んで再アクセスする。
# - ファイルが存在しないパスはキャッシュせず ``ComkenFileNotFoundError`` がそのまま上がる
#   （メッセージにパスが含まれるので、業務担当者が共有サーバーの障害 /
#   パス変更 / 権限喪失に気づける。プロバイダ側で業務例外への変換はしない）。
# - ``_MASTER_CACHE_MAXSIZE``（既定 16）で **FIFO 退避** する:  業務利用では
#   管理表は 1〜数種類しかなく到達はまず無いが、テストで大量の ``tmp_path`` を
#   読むケースでエントリが膨らまないための安全弁。
_MASTER_CACHE_MAXSIZE = 16
_master_cache: OrderedDict[str, dict[str, ReportEntry]] = OrderedDict()

# ── グループ設定のプロセス内キャッシュ ─────────────────────────────────────
# `report_folder()` は管理表の1件ごとに呼ばれる。毎回 `load_group_settings()` を
# 走らせると、設定シートを N 回開くことになるので、 管理表キャッシュと同じ
# 設計で ``_master_cache`` とは別口の ``OrderedDict`` にキャッシュする
# （無効化条件・退避ポリシーは ``_MASTER_CACHE_MAXSIZE`` と揃える）。
#
# **管理表キャッシュとはキーを分ける。** 同じ ``MASTER_PATH`` でも、将来
# 「管理表と設定シートを別ファイルに置く」拡張に備えるため。``_reset_cached_master()``
# からこのキャッシュも一緒に破棄するので、テストで管理表を差し替えたときに
# 古いグループ設定が漏れない。
_GROUP_SETTINGS_CACHE_MAXSIZE = 16
_group_settings_cache: OrderedDict[str, dict[str, Path]] = OrderedDict()


def output_path(
    entry: ReportEntry,
    schedule_run_time: dt.time | None = None,
    *,
    now: dt.datetime | None = None,
) -> Path:
    """レポートの保存先パス（唯一の出力先）を返す。

    フォルダは ``report_folder()``（設定シートのベースパスをそのまま返す）。
    ファイル名は ``{管理番号}_{時刻:%Y%m%d_%H%M}.csv``。時刻は ``schedule_run_time``
    （今回の取得の根拠になったスケジュール行の「取得時刻」、``ScheduleRule.desired_time``
    の値。記録用の希望時刻）を優先し、 ``None`` （スケジュール行が無いレポート、
    後方互換）のときは ``now``（省略時は現在時刻）をそのまま使う。

    常に新規ファイルとして扱う（同じパスへの上書きは想定しない。衝突回避は呼び出し側
    ``Salesforceレポートダウンローダー`` の ``_reserve_unique_path`` の責務）。

    Args:
        entry: レポート管理表の1行。
        schedule_run_time: 今回の取得の根拠になったスケジュール行の「取得時刻」
            （``ScheduleRule.desired_time``）。判定には使われない記録用の希望時刻で、
            ファイル名に ``%H%M`` として埋め込む。無ければ ``now`` にフォールバックする。
        now: ``schedule_run_time`` が無いときに使う時刻。省略時は現在時刻
            （``comken.core.dates.now()`` を使う）。

    Raises:
        GroupNotRegisteredError: 設定シートにないグループ名の場合（``report_folder()`` 経由）。
    """
    # ``MASTER_PATH`` は呼び出し時点のモジュール変数を参照する（テストでは
    # ``monkeypatch.setattr(paths, "MASTER_PATH", ...)`` で差し替える）。
    from src.paths import MASTER_PATH as _MASTER_PATH

    folder = report_folder(entry, _load_group_settings_cached(_MASTER_PATH))
    current = now if now is not None else clock_now()
    if schedule_run_time is not None:
        base_dt = dt.datetime.combine(current.date(), schedule_run_time)
    else:
        base_dt = current
    return folder / f"{entry.key}_{base_dt.strftime('%Y%m%d_%H%M')}.csv"


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
    # ``MASTER_PATH`` は呼び出し時点のモジュール変数を参照する。
    from src.paths import MASTER_PATH as _MASTER_PATH

    base_path = group_settings.get(entry.group)
    if base_path is None:
        raise GroupNotRegisteredError(entry.group, sorted(group_settings), _MASTER_PATH)
    return base_path


def _find(report_key: str, master_path: Path) -> ReportEntry:
    """管理表から1行を引く。無効なものはここで止める。

    管理表 Excel を読む ``load_master()`` はキャッシュ経由で呼ばれる
    （プロセス内で同じ解決済みパスなら 1 度しか開かない）。 詳細は
    モジュール冒頭のキャッシュ設計コメント参照。
    """
    entries = _load_master_cached(master_path)
    entry = entries.get(report_key)
    if entry is None:
        raise ReportNotRegisteredError(report_key, sorted(entries), master_path)
    if not entry.enabled:
        raise _report_disabled_error(entry.key, entry.summary, master_path)
    return entry


def _load_master_cached(master_path: Path) -> dict[str, ReportEntry]:
    """``load_master()`` を ``Path.resolve()`` 後の絶対パスでキャッシュする。

    公開 API の ``load_master()`` は Excel を毎回読むシグネチャを残す
    （呼び出し側がテストで生のアクセスをすることがあるため）。 その
    公開シグネチャを持ちながら、 ``_find()`` 経由での呼び出しでは
    Excel を開かないように、この内部関数で ``OrderedDict`` ベースの
    1段キャッシュに流す。
    """
    from src.sheets.master import load_master

    resolved_key = str(master_path) if master_path.is_absolute() else str(master_path.resolve())
    cache = _master_cache
    if resolved_key in cache:
        cache.move_to_end(resolved_key)
        return cache[resolved_key]
    entries = load_master(Path(master_path))
    cache[resolved_key] = entries
    cache.move_to_end(resolved_key)
    if len(cache) > _MASTER_CACHE_MAXSIZE:
        cache.popitem(last=False)
    return entries


def _load_group_settings_cached(master_path: Path) -> dict[str, Path]:
    """``load_group_settings()`` を ``Path.resolve()`` 後の絶対パスでキャッシュする。

    管理表キャッシュと同じポリシー（``_GROUP_SETTINGS_CACHE_MAXSIZE`` で FIFO 退避、
    ``stat()`` による更新確認は行わない）で、設定シートの読み取りを 1 プロセス内で
    1パスにつき 1 回に抑える。`_load_master_cached()` と同じ ``Path.resolve()`` 後の
    キーを共有するので、両キャッシュのキーが食い違うことはない。
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


def _reset_cached_master() -> None:
    """**管理表・グループ設定の両方のキャッシュを破棄する**（テスト用）。

    テストでは ``tmp_path`` がテストごとに違う管理表 Excel を指すため、
    前のテストのキャッシュが残ると別テストの設定が漏れる。 各テストの
    冒頭で呼ぶ想定（``tests/test_paths.py`` の
    autouse fixture から）。 利用者向けの公開 API ではない。

    管理表を差し替えると、設定シート（同じブック内）も差し替わるので、
    **両方のキャッシュを一緒に破棄する**（片方だけ破棄すると、古いグループ
    設定が残って別テストの設定が漏れる）。
    """
    _master_cache.clear()
    _group_settings_cache.clear()

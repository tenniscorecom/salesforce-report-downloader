"""src/notification.py — Box Drive への通知ファイル書き出し。

**Teams へ通知したいが、Outlook は使えず、Power Automate のクラウドフローは UNC を
監視できない。** そこで、Box Drive が同期するローカルフォルダ
（``src.paths.NOTIFICATION_FOLDER``、既定では ``~/Box/Salesforceレポートダウンローダー通知``）へ
1件1ファイルの JSON を書き出し、Power Automate の Box コネクタ（ファイル作成時トリガー）が
これを受けて Teams チャンネルへ投稿する、という間接連携を取る。

**書くタイミングは 2 種類だけ**:

- 取得が失敗したとき（``種類 = 失敗``）
- Report API で失敗 → ブラウザに自動で切り替わって成功したとき（``種類 = 自動切替``。
  SOQL 化や管理表修正を促すため）

通常の成功（API / SOQL / 最初からブラウザ）では書かない（毎回書くと Power Automate
の転送上限に抵触する）。

**通知の失敗で取得を止めない**: ``NOTIFICATION_FOLDER`` の親（``~/Box``）が無い、
書き込めない等は ``logger.warning`` を出して戻るだけ。例外を外へ出さない。
ただしバグは握りつぶさず ``OSError`` 以外の例外はそのまま伝播させる
（``TypeError`` などを黙って通過させないため）。

**dry-run 中は書かない**: ``comken.runtime.is_dry_run()`` が真のときは JSON を
組み立ててログだけ出す。Box に書き出して Power Automate を発火させないため。

**ファイル名は ``{%Y%m%d_%H%M%S}_{管理番号}_{連番か短いランダム}.json``**: 同時刻の
複数件が衝突して上書きしないよう、排他的新規作成（``open("x")``）で空き名前を
探す。万一 ``OSError`` のリトライで連番上限を超えるようなことが起きても分かる短さに
留める。

**書き出しは一時ファイルへ書いてから ``rename`` で置き換える**: Box Drive が
「書きかけの JSON」を同期して、Power Automate 側が「壊れた JSON」を読む事故を
防ぐため。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import string
import tempfile
from pathlib import Path

from comken.runtime import is_dry_run

from src import paths as _paths

logger = logging.getLogger(__name__)

# ファイル名衝突回避の連番上限。``_reserve_unique_path`` と同じ思想で、
# 共有サーバーや Box Drive の同期異常で ``FileExistsError`` が返り続けると
# 無限ループになるため、必ず上限を切る
_NOTIFY_SEQUENCE_LIMIT = 1000

# ファイル名末尾のランダム文字列の長さ。短いランダムで衝突確率を下げる
_RANDOM_SUFFIX_LENGTH = 4


def write_notification(
    *,
    report_key: str,
    summary: str,
    url: str,
    kind: str,  # "失敗" / "自動切替"
    route: str,
    cause: str,
    error_code: str,
    error_message: str,
    row_count: int | None,
    executed_at: dt.datetime,
) -> Path | None:
    """1 件の取得について、Box 通知ファイルを書き出す。

    戻り値は実際に書いたファイルのパス。**書かなかったとき**（dry-run 中、
    親フォルダ ``~/Box`` が無い、書き込めない等）は ``None`` を返す。例外は
    ``OSError`` だけ飲み込み（取得を止めないため）、それ以外のバグはそのまま
    外へ伝える。

    Args:
        report_key: 管理番号（ファイル名に使う）。
        summary: 概要（JSON の中身にも入れる）。
        url: Salesforce レポートの URL。
        kind: ``"失敗"`` または ``"自動切替"``。**呼び出し側で値の整合性を担保する前提**
            （このモジュールでは検査しない）。
        route: 履歴の「取得経路」列と同じ文字列（``API / SOQL / ブラウザ / 自動切替2種``）。
        cause: 履歴の「原因区分」列の値（失敗時のみ入る想定）。
        error_code: 履歴の「エラーコード」列の値（例外クラス名）。
        error_message: 履歴の「エラー内容」列の値。
        row_count: 取得できた行数（失敗時は ``None`` でも可）。
        executed_at: 実行日時。**ファイル名のタイムスタンプ**にもなる。
    """
    payload = _build_payload(
        report_key=report_key,
        summary=summary,
        url=url,
        kind=kind,
        route=route,
        cause=cause,
        error_code=error_code,
        error_message=error_message,
        row_count=row_count,
        executed_at=executed_at,
    )
    if is_dry_run():
        logger.info(
            "dry-run のため Box 通知ファイルを書きません: %s",
            json.dumps(payload, ensure_ascii=False),
        )
        return None

    folder = _paths.NOTIFICATION_FOLDER
    try:
        parent = folder.parent
        if not parent.is_dir():
            logger.warning(
                "Box 通知フォルダの親 %s が無いため、通知ファイルを書きません"
                "（取得は成功扱いで進めます）",
                parent,
            )
            return None
    except OSError as exc:
        logger.warning(
            "Box 通知フォルダの親 %s の有無を確認できなかったため、通知ファイルを書きません: %s",
            folder.parent,
            exc,
        )
        return None

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Box 通知フォルダ %s を作れないため、通知ファイルを書きません: %s",
            folder,
            exc,
        )
        return None

    path = _reserve_unique_path(folder, report_key, executed_at)
    if path is None:
        return None
    try:
        return _atomic_write_json(path, payload)
    except OSError as exc:
        logger.warning(
            "Box 通知ファイル %s への書き出しに失敗したため、"
            "取得処理はそのまま成功扱いで進めます: %s",
            path,
            exc,
        )
        return None


def _build_payload(
    *,
    report_key: str,
    summary: str,
    url: str,
    kind: str,
    route: str,
    cause: str,
    error_code: str,
    error_message: str,
    row_count: int | None,
    executed_at: dt.datetime,
) -> dict[str, object]:
    """Teams 通知用に流す JSON ペイロードを組み立てる。

    Power Automate 側で「ファイルの内容を取得 → JSON 解析 → Teams 投稿」を
    行うため、**キーは Power Automate から参照しやすい日本語に揃える**。
    呼び出し側は ``kind`` が ``"失敗"`` か ``"自動切替"`` かを間違えないこと
    （このモジュールでは検査しない）。
    """
    return {
        "実行日時": executed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "管理番号": report_key,
        "概要": summary,
        "URL": url,
        "種類": kind,
        "取得経路": route,
        "原因区分": cause,
        "エラーコード": error_code,
        "エラー内容": error_message.replace("\n", " "),
        "件数": "" if row_count is None else row_count,
    }


def _random_suffix() -> str:
    """短いランダム文字列を返す（大文字小文字の英数字）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=_RANDOM_SUFFIX_LENGTH))


def _reserve_unique_path(folder: Path, report_key: str, executed_at: dt.datetime) -> Path | None:
    """排他的新規作成でファイル名を予約する。

    ベース名は ``{実行日時_%Y%m%d_%H%M%S}_{管理番号}`` で、同時刻の複数件が
    衝突しないよう末尾に短いランダム文字列と連番（``_1`` / ``_2`` …）を
    付ける。``_NOTIFY_SEQUENCE_LIMIT`` を超える ``FileExistsError`` が返り続ける
    異常時は ``None`` を返す（呼び出し側で警告ログに留めて取得は止めない）。
    """
    timestamp = executed_at.strftime("%Y%m%d_%H%M%S")
    base_name = f"{timestamp}_{report_key}_{_random_suffix()}.json"
    candidate = folder / base_name
    sequence = 0
    for _ in range(_NOTIFY_SEQUENCE_LIMIT):
        try:
            candidate.open("x").close()
            # 排他作成でファイルが残るので、書き込みは呼び出し側に任せるために
            # 一旦閉じたものをそのまま使う（書き換えは _atomic_write_json 側で
            # 「一時ファイル → rename」を行うため、ここで空ファイルが残っても
            # 上書きされる）
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = folder / f"{timestamp}_{report_key}_{_random_suffix()}_{sequence}.json"
    logger.warning(
        "Box 通知ファイルの連番上限 (%d) に達したため書き出しを諦めます: %s",
        _NOTIFY_SEQUENCE_LIMIT,
        folder,
    )
    return None


def _atomic_write_json(path: Path, payload: dict[str, object]) -> Path:
    """一時ファイルへ JSON を書いてから ``rename`` で置き換える。

    Box Drive が「書きかけの JSON」を同期して、Power Automate 側が
    「壊れた JSON」を読む事故を防ぐ。``tempfile.NamedTemporaryFile`` を使い、
    同じフォルダ内（``folder``）に作ってから ``os.replace`` でアトミックに置き換える
    （別フォルダ間で ``rename`` すると Windows で失敗するため）。
    """
    fd, tmp_path_str = tempfile.mkstemp(
        suffix=".json",
        prefix=".notify_",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        # 一時ファイルが残らないようにする。``os.replace`` 失敗時は呼び出し側の
        # ``except OSError`` が通知ファイル全体の失敗として扱うので、ここで
        # 残った一時ファイルはそのままにせず消す
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    logger.debug("Box 通知ファイル書き出し: %s", path)
    return path


__all__ = ["write_notification"]

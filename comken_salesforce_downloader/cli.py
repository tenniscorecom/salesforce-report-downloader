r"""comken_salesforce_downloader/cli.py — 管理表まわりの保守コマンド。

    python -m comken_salesforce_downloader check                       管理表を検査する

**これは保守用のコマンドで、業務の定期実行ではない。** 毎日の取得は個別プロジェクトから
`download_scheduled()` を呼ぶ（ライブラリには**実行される単位を置かない**）。

`check` は、管理表を編集したあとに「プログラムから正しく読めるか」を確かめるためのもの。
書き方の誤り（管理番号の重複、URL からレポート ID を取り出せない等）は取得のときにも
止まるが、**編集した直後にその場で分かる**ほうが直すのが早い。

このファイルが持つもの:
- 管理表の検査（`check`）

ここに書かないもの:
- 業務の定期実行 → 利用プロジェクトから `download_scheduled()` を呼ぶ
  （ライブラリには実行される単位を置かない）
- 取得そのものを行うサブコマンド
"""

# CLI は print を使う（logging より読みやすい）

import argparse
import sys
from pathlib import Path

from comken.exceptions import ComkenError

from comken_salesforce_downloader._paths import MASTER_PATH
from comken_salesforce_downloader.master import load_master, shared_report_ids


def main(argv: list[str] | None = None) -> int:
    """コマンドを実行して終了コードを返す（0=成功 / 1=失敗）。"""
    args = _build_parser().parse_args(argv)
    try:
        args.run(args)
    except ComkenError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m comken_salesforce_downloader",
        description="Salesforce レポート管理表の検査",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="管理表を読んで、書き方の誤りを調べる")
    check.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=f"管理表のパス（省略時: {MASTER_PATH}）",
    )
    check.set_defaults(run=_run_check)

    return parser


def _run_check(args: argparse.Namespace) -> None:
    """管理表を読んで、件数と気になる点を出す。"""
    path = args.path or MASTER_PATH
    entries = load_master(path)  # 書き方の誤りはここで例外になる

    scheduled = [entry for entry in entries.values() if entry.is_scheduled and entry.enabled]
    disabled = [entry for entry in entries.values() if not entry.enabled]
    print(f"読めました: {path}")
    print(f"  登録 {len(entries)} 件（定期 {len(scheduled)} 件 / 無効 {len(disabled)} 件）")

    shared = shared_report_ids(entries)
    if not shared:
        return
    # エラーにはしない。意図している場合もあるので、気づけるようにするだけ
    print()
    print("同じ Salesforce レポートを指している管理番号があります:")
    for report_id, keys in shared.items():
        names = "、".join(f"{key}（{entries[key].summary}）" for key in keys)
        print(f"  {report_id}: {names}")

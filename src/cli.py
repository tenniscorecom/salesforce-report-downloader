r"""src/cli.py — 管理表まわりの保守コマンド。

    python -m src.cli check                       管理表を検査する

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

from src.paths import MASTER_PATH
from src.sheets.group_settings import load_group_settings
from src.sheets.master import (
    ReportEntry,
    load_master,
    shared_report_ids,
)
from src.sheets.schedule import ScheduleRule, load_schedule


def main(argv: list[str] | None = None) -> int:
    """コマンドを実行して終了コードを返す（0=成功 / 1=失敗）。

    **相互参照エラー（後述）は複数あってもすべて列挙してから 1 を返す。** 1 件目で
    止めると、業務担当者が「直したらまた次が出て」を繰り返す羽目になるため。
    """
    args = _build_parser().parse_args(argv)
    try:
        cross_errors = args.run(args)
    except ComkenError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    if cross_errors:
        print()
        for message in cross_errors:
            print(message, file=sys.stderr)
        print(
            f"\n相互参照エラー: {len(cross_errors)} 件（管理表を開いて直してから"
            "もう一度 sfdl check を実行してください）",
            file=sys.stderr,
        )
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
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


def _run_check(args: argparse.Namespace) -> list[str]:
    """管理表とスケジュールを読んで、件数・注意点・相互参照エラーを出す。

    **3 シート（管理表 / スケジュール / 設定）を読み込んだうえで、相互参照を検査する:**

    - 「スケジュール」のレポートキーが「管理表」の ID に無いか
    - 「管理表」で有効な行のグループが「設定」に無いか

    見つかった相互参照エラーをすべて列挙した ``list[str]`` を呼び出し元へ返す
    （1 件目で停止しない）。呼び出し元の ``main()`` がエラー件数を出してから
    終了コード 1 を返す。
    """
    path = args.path or MASTER_PATH
    entries = load_master(path)  # 書き方の誤りはここで例外になる
    rules = load_schedule(path)
    settings = load_group_settings(path)

    _print_summary(path, entries, rules, settings)
    _print_reference_notes(entries, rules)

    # 相互参照エラー。1件目で止めず全て集めて呼び出し元へ返す
    return _collect_cross_errors(entries, rules, settings)


def _print_summary(
    path: Path,
    entries: dict[str, ReportEntry],
    rules: list[ScheduleRule],
    settings: dict[str, Path],
) -> None:
    """登録件数と「同じ Salesforce レポートを指している管理番号」を出す。"""
    enabled = [entry for entry in entries.values() if entry.enabled]
    disabled = [entry for entry in entries.values() if not entry.enabled]
    print(f"読めました: {path}")
    print(f"  登録 {len(entries)} 件（有効 {len(enabled)} 件 / 無効 {len(disabled)} 件）")
    print(f"  スケジュール {len(rules)} 件、設定 {len(settings)} 件")

    shared = shared_report_ids(entries)
    if not shared:
        return
    # エラーにはしない。意図している場合もあるので、気づけるようにするだけ
    print()
    print("同じ Salesforce レポートを指している管理番号があります:")
    for report_id, keys in shared.items():
        names = "、".join(f"{key}（{entries[key].summary}）" for key in keys)
        print(f"  {report_id}: {names}")


def _print_reference_notes(entries: dict[str, ReportEntry], rules: list[ScheduleRule]) -> None:
    """参考情報の表示（エラーではないが気づけるように出す行）。

    - 有効な管理番号のうち、スケジュール行が1つもないもの（毎回取得される後方互換）
    - 有効なスケジュール行が、無効化されたレポートを指しているもの（実行時にスキップ）
    """
    referenced_keys: set[str] = set()
    invalid_target_rules: list[str] = []
    for rule in rules:
        if rule.report_key not in entries:
            continue
        referenced_keys.add(rule.report_key)
        target = entries[rule.report_key]
        if not target.enabled:
            invalid_target_rules.append(
                f"{rule.schedule_key}（→ {rule.report_key}（{target.summary}））"
            )

    enabled_keys = [entry.key for entry in entries.values() if entry.enabled]
    unscheduled = [key for key in enabled_keys if key not in referenced_keys]

    if unscheduled:
        print()
        print(
            f"有効な管理番号 {len(unscheduled)} 件はスケジュール行が登録されていません"
            "（毎回取得される後方互換の挙動です。意図と合っているか確認してください）:"
        )
        for key in unscheduled:
            print(f"  {key}（{entries[key].summary}）")

    if invalid_target_rules:
        print()
        print(
            "有効なスケジュール行が、無効化された管理表のレポートを指しています"
            "（実行時にスキップされます。管理表の「有効」を直すか、"
            "スケジュール行を「×」にしてください）:"
        )
        for line in invalid_target_rules:
            print(f"  {line}")


def _collect_cross_errors(
    entries: dict[str, ReportEntry],
    rules: list[ScheduleRule],
    settings: dict[str, Path],
) -> list[str]:
    """相互参照エラーをすべて集めて返す（1件目で止めない）。

    検査するエラー:

    - スケジュールの「レポートキー」が管理表の「ID」に無い
    - 管理表で使われているグループが「設定」シートに登録されていない
    """
    cross_errors: list[str] = []
    for rule in rules:
        if rule.report_key not in entries:
            cross_errors.append(
                f"スケジュールの「レポートキー」が管理表に存在しません: "
                f"{rule.schedule_key}（→ {rule.report_key}）"
            )

    missing_groups = sorted(
        {entry.group for entry in entries.values() if entry.enabled} - set(settings)
    )
    for group in missing_groups:
        cross_errors.append(
            f"管理表で使われているグループが「設定」シートに登録されていません: {group}"
        )
    return cross_errors


if __name__ == "__main__":
    raise SystemExit(main())

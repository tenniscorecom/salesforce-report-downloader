r"""src/cli.py — 管理表まわりの保守コマンド。

    python -m src.cli check                       管理表を検査する
    python -m src.cli init [path]                 管理表の雛形を作る

**これは保守用のコマンドで、業務の定期実行ではない。** 毎日の取得は個別プロジェクトから
`download_scheduled()` を呼ぶ（ライブラリには**実行される単位を置かない**）。

`check` は、管理表を編集したあとに「プログラムから正しく読めるか」を確かめる。
書き方の誤りは取得のときにも止まるが、編集した直後にその場で分かるほうが直すのが早い。

`init` は、「管理表」「スケジュール」「設定」「記入方法」の 4 シートを 1 ブックにした
雛形を新規作成する。**既存ファイルがある場合は上書きしない**（雛形をやり直したいときは
既存ファイルを移動してから実行する）。
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
from src.template_writer import create_combined_workbook


def main(argv: list[str] | None = None) -> int:
    """コマンドを実行して終了コードを返す（0=成功 / 1=失敗）。

    **相互参照エラーは複数あってもすべて列挙してから 1 を返す**（1 件ずつ直す手間を避ける）。
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
        description="Salesforce レポート管理表の作成・検査",
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

    init = subparsers.add_parser(
        "init", help="管理表の雛形（管理表/スケジュール/設定/記入方法）を作る"
    )
    init.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=f"管理表のパス（省略時: {MASTER_PATH}）。既存ファイルがある場合は上書きしない",
    )
    init.set_defaults(run=_run_init)

    return parser


def _run_check(args: argparse.Namespace) -> list[str]:
    """管理表とスケジュールを読んで、件数・注意点・相互参照エラーを出す。

    3 シート（管理表 / スケジュール / 設定）を読み込み、相互参照を検査する:

    - 「スケジュール」のレポートキーが「管理表」の ID に無いか
    - 「管理表」で有効な行のグループが「設定」に無いか

    見つかったエラーをすべて ``list[str]`` で返す（件数表示と終了コードは ``main()``）。
    """
    path = args.path or MASTER_PATH
    entries = load_master(path)  # 書き方の誤りはここで例外になる
    rules = load_schedule(path)
    settings = load_group_settings(path)

    _print_summary(path, entries, rules, settings)
    _print_reference_notes(entries, rules)

    # 相互参照エラーは全て集めて返す
    return _collect_cross_errors(entries, rules, settings)


def _run_init(args: argparse.Namespace) -> list[str]:
    """管理表の雛形を作る。既存ファイルがある場合は上書きせず終了コード 1。

    ここではパスの決定と既存チェックだけを行い、雛形は ``create_combined_workbook()`` が
    作る。``--force`` は持たせない（実データの管理表を誤って消さないため）。
    既存ファイルがあれば ``ComkenError`` を送出し、``main()`` が「エラー: ...」を出して
    終了コード 1 にする。正常完了時は空リスト。
    """
    path = args.path or MASTER_PATH
    if path.exists():
        raise ComkenError(
            f"既にあります: {path}（上書きしません。"
            "別のパスを指定するか、既存ファイルを移動してから実行してください）"
        )
    create_combined_workbook(path)
    print(f"作成しました: {path}")
    print("記入後に python -m src.cli check で確認してください")
    return []


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

    - 有効な管理番号のうち、スケジュール行が1つもないもの（実行時は取得されず警告が出る）
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
            "（実行時に取得されず警告が出ます。意図と合っているか確認してください）:"
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

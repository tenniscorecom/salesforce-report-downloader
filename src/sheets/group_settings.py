"""src/sheets/group_settings.py — グループ設定シート。

**`sheets/` には、ワークブック・CSVの「1枚（1ファイル）」ごとに、そこにある列と意味を
宣言するモジュールを集めている**（`master.py` / `schedule.py` / `history.py` / このファイル）。

**このシートはレポート管理表と同じブック内の別シート**として配置する（人がブックを
1つだけ開けば管理表と設定の両方を確認できるように）。1行に「グループ名」と
「ベースパス」を書き、管理表の `ReportEntry.group` から出力先を引くときに使う。
`report_folder()` は設定シートのベースパスをそのまま返す。

**Excel の見出しは「ベースURL」だが、書くのは URL ではなくフォルダのパス。**
`src.paths.report_folder()` が組み立てルールの一次情報。

``load_group_settings()`` はパスを引数で受け取る（``schedule.py`` と同じ設計）。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from comken.core.timer import measure

from src.report_master import MasterRow, column

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GroupSetting(MasterRow):
    """「設定」シートの1行。出力先の起点（ベースパス）をグループに紐付ける。

    ``unique=True`` の ``group`` 列で、**同じグループ名が2行に書かれていたらエラーにする**
    （ベースパスが2つあると決められないため）。
    """

    SHEET_NAME = "設定"
    # ``PATH`` は設定しない（``load_group_settings()`` の引数で指定する）

    group: str = column(
        "グループ",
        unique=True,
        help="レポート管理表の「グループ」列と一致させる社内のグループ名・部署名。"
        "同じ名前は1行しか登録できません",
    )
    base_path: Path = column(
        "ベースURL",
        help="このグループの出力先の起点パス（例: \\\\server\\share\\営業本部）。"
        "出力ファイルはこのベースパスの直下に置きます"
        "（`provider.report_folder()` の戻り値をそのまま保存先フォルダとして使う）",
    )


@measure
def load_group_settings(path: str | Path) -> dict[str, Path]:
    """設定シートを読んで、{グループ名: ベースパス} の辞書を返す。

    設定シートは管理表と同じブックにあるので、``path`` には管理表のパスを渡す。

    Args:
        path: 設定シートを含む Excel のパス。

    Returns:
        ``{グループ名: ベースパス}``。グループ名は「設定」シートに書かれた順を保つ。
    """
    source = Path(path)
    logger.debug("グループ設定読込開始: path=%s", source)
    settings = {setting.group: setting.base_path for setting in GroupSetting.load(source)}
    logger.debug("グループ設定読込完了: path=%s, 件数=%d", source, len(settings))
    return settings

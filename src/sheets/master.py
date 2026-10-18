r"""src/sheets/master.py — レポート管理表の列を決める。

**`sheets/` には、ワークブック・CSVの「1枚（1ファイル）」ごとに、そこにある列と意味を
宣言するモジュールを集めている**（`schedule.py` = スケジュールシート、`group_settings.py` =
設定シート、`history.py` = 履歴CSV）。Excel の読み書きの仕組みは `src.report_master` にあり、
ここは**どんな列があるか**だけを宣言する。

    | ID   | グループ | 担当者 | 概要     | Salesforce URL              | 有効 |
    |------|----------|--------|----------|-----------------------------|------|
    | 1001 | 営業本部  | 山田   | 顧客一覧 | https://.../Report/00O.../  | 有効 |

**Salesforce のレポート ID は入力させない。** URL を貼れば `report_id_from_url()` が
取り出す（人が抜き出すと写し間違いが起きる）。

**ID（管理番号）は Salesforce のレポート ID ではない。** 社内で決める論理的な番号で、
同じ意味のデータを指す限り変えない。参照先の Salesforce レポートを差し替えても、
利用側の Python コード（`CUSTOMER_LIST = "1001"`）は変えずに済む。

**出力先フォルダは Excel のセルには書かない。** `グループ` 列 + `設定` シート
（`group_settings.py`）の組み合わせで Python 側で組み立てる（`src.paths.report_folder()`）。
`担当者` / `概要` は記録用で、出力パスには使わない。

このファイルが持つもの:
- 管理表にどんな列があるか
- 各列の意味と書き方
- URL からレポートIDを取り出すこと

ここに書かないもの:
- Excel をどう読むか・どう検証するか → src.report_master
- 取得の実行・保存 → src.service
- 履歴の読み取り・形式 → src.sheets.history
- 出力先フォルダの組み立て → src.paths / src.sheets.group_settings
"""

import logging
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from comken.core.timer import measure
from comken.exceptions import SalesforceReportIDNotFoundError
from comken.toolbox.salesforce.report import report_id_from_url

from src.report_master import MasterRow, column

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ReportEntry(MasterRow):
    """レポート管理表の1行。"""

    SHEET_NAME = "管理表"

    key: str = column(
        "ID",
        unique=True,
        help="社内で決める管理番号。Salesforce のレポート ID ではありません。"
        "参照先のレポートを差し替えても、この番号は変えません。"
        "前ゼロ（0001 など）や記号入りの値も使えます",
    )
    # 出力先は「ベースパス（設定シート）」のみ。`assignee` / `summary` は記録用で出力パスに無関係
    group: str = column(
        "グループ",
        help="出力先を決めるグループ名。sheets/group_settings.py の設定シートに"
        "登録したグループ名と一致させてください",
    )
    assignee: str = column(
        "担当者",
        help="記録用の担当者名。出力パスには使いません",
    )
    summary: str = column(
        "概要",
        help="人が読んで何のレポートか分かる説明。記録用。出力パスには使いません",
    )
    # 既定値 `True`（=「個人情報あり」）は「分からないものは安全側」の意味。保存処理は
    # この値を読まない（記録用のみ）ので、`True` に倒れても挙動は変わらない。
    # 既定値のある列は見出しごと無くても読める。
    # `choices` で `○` `×` 以外を弾く（既定の bool 変換は一覧に無い文字を黙って `False` に
    # する）。bool 列の `choices` は1つ目を True、2つ目を False の表記として雛形へ書く
    has_personal_info: bool = column(
        "個人情報",
        choices=("○", "×"),
        default=True,
        help="そのレポートに個人情報（氏名・電話番号・住所など）が含まれるなら「○」、"
        "含まれないことを確認したら「×」。空欄は「○」とみなします"
        "（分からないものは安全側として扱うため）",
    )
    url: str = column(
        "Salesforce URL",
        help="Salesforce でレポートを開いたときのアドレスを、そのまま貼り付けてください。"
        "レポート ID を抜き出す必要はありません",
    )
    # 既定値を持たせない: 空欄を「有効」にすると書き忘れがそのまま有効になる。
    # 表記は `choices` で「○」「×」に統一する
    enabled: bool = column(
        "有効",
        choices=("○", "×"),
        help="「○」か「×」と書いてください。使わなくなったら「×」にし、"
        "行は消さないでください（過去の履歴と対応が取れなくなります）",
    )
    # 既定値 `×`: 書き忘れても厳しい側（エラー）に倒れるだけで、データは失われない。
    # `choices` の扱いは `has_personal_info` と同じ
    allow_empty: bool = column(
        "0件あり",
        choices=("○", "×"),
        default=False,
        help="その日のデータが 0 件になることがあるレポートなら「○」。"
        "「×」のときに 0 件だとエラーになります"
        "（指しているレポートが違う可能性に気づけるようにするため）",
    )
    # 既定値 `×`: 書き忘れても通常の Report API 経由のまま動く。「○」でブラウザ経由に
    # 切り替わる。SOQL 列が「○」の行では SOQL 経由が優先される
    exceeds_row_limit: bool = column(
        "2000件超",
        choices=("○", "×"),
        default=False,
        help="Report API の2000行上限を超えることが分かっているレポートなら「○」。"
        "取得実行側がブラウザ経由（画面のエクスポート機能）に切り替えます。"
        "「SOQL」列が「○」のときはこの列より優先されます",
    )
    # 既定値 `×`。「○」にする場合は同じ管理番号の `SoqlReport.KEY` を
    # `src.soql_reports` へ登録しておく（無いと設定ミスとして例外で止まる）
    use_soql: bool = column(
        "SOQL",
        choices=("○", "×"),
        default=False,
        help="SOQL化済みで、同じ管理番号のSOQL定義が"
        "src.soql_reports に登録されているなら「○」。"
        "取得実行側はSOQL経由に切り替えます（「2000件超」列より優先）",
    )
    # 既定値 `×`（毎回新しいファイル）: ``{管理番号}_{YYYYmmdd_HHMM}.csv`` + 連番で保存する。
    # 「○」だと ``{管理番号}_{HHMM}.csv`` に置き換える（日付は入らず、翌日の同時刻に上書き）。
    # 1 日に何度も最新が必要で、過去分をファイルとして残す必要が無いレポート向け
    overwrite: bool = column(
        "上書き",
        choices=("○", "×"),
        default=False,
        help="「○」にすると、毎回同じ名前（{管理番号}_{HHMM}.csv）で上書き保存します。"
        "「×」は取得ごとに日時付きの別ファイルを作ります",
    )

    @cached_property
    def report_id(self) -> str:
        """URL から取り出した Salesforce のレポート ID。

        ``cached_property`` で同じ entry について 1 回だけ URL をパースする
        （1 件のダウンロード内で複数回参照されるため）。例外時はキャッシュしない。

        **行番号ではなく管理番号で示す。** 空行を飛ばして読むので行番号はズレうるが、
        管理番号なら管理表を検索して一発で見つかる。

        Raises:
            SalesforceReportIDNotFoundError: URL からレポート ID を取り出せない場合。
        """
        try:
            return report_id_from_url(self.url)
        except SalesforceReportIDNotFoundError as e:
            # 管理番号も出したいので添えて上げ直す
            raise SalesforceReportIDNotFoundError(f"{self.url}（管理番号 {self.key}）") from e


@measure
def load_master(path: str | Path | None = None) -> dict[str, ReportEntry]:
    """管理表を読んで、管理番号をキーにした辞書を返す。

    Args:
        path: 管理表（Excel）のパス。

    Returns:
        {管理番号: ReportEntry}。管理表に並んでいる順を保つ。
    """
    logger.debug("レポート管理表読込開始: path=%s", path)
    entries = {}
    for entry in ReportEntry.load(path):
        entry.report_id  # noqa: B018 — URL が壊れていれば、ここで読み込みごと止める
        entries[entry.key] = entry
    logger.debug("レポート管理表読込完了: path=%s, 件数=%d", path, len(entries))
    return entries


def shared_report_ids(entries: dict[str, ReportEntry]) -> dict[str, list[str]]:
    """同じ Salesforce レポートを指している管理番号を返す。

    **同じレポートを複数のプロジェクトが別々の管理番号で使っている**ことが分かる。
    エラーにはしない——意図してそうしている場合（保存先を分けたい等）もあるため、
    気づけるようにするだけにする。

    Returns:
        {Salesforce のレポート ID: [管理番号, ...]}。2つ以上のものだけ。
    """
    by_report_id: dict[str, list[str]] = {}
    for entry in entries.values():
        by_report_id.setdefault(entry.report_id, []).append(entry.key)
    duplicates = {report_id: keys for report_id, keys in by_report_id.items() if len(keys) > 1}
    logger.debug("重複レポートIDの検出: 対象=%d, 重複=%d", len(by_report_id), len(duplicates))
    return duplicates

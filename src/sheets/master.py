r"""src/sheets/master.py — レポート管理表の列を決める。

**`sheets/` には、ワークブック・CSVの「1枚（1ファイル）」ごとに、そこにある列と
意味を宣言するモジュールを集めている**（`schedule.py` = スケジュールシート、
`group_settings.py` = 設定シート、`history.py` = 履歴CSV）。
Excel の読み書きそのものの仕組みは含めない（`src.report_master` など、
`sheets/` の外に置く）。

**このファイルにあるのは「社内の取り決め」だけ。** Excel を読む・検証する・雛形を作る
仕組みは `src.report_master` にあり、ここは
**どんな列があるか**を宣言する。

    | ID   | グループ | 担当者 | 概要     | Salesforce URL              | 有効 |
    |------|----------|--------|----------|-----------------------------|------|
    | 1001 | 営業本部  | 山田   | 顧客一覧 | https://.../Report/00O.../  | 有効 |

**Salesforce のレポート ID は入力させない。** URL を貼れば `report_id_from_url()` が
取り出す。ID を人が抜き出す工程を挟むと、そこで写し間違いが起きるうえ、
「どのレポートか」を確かめるには結局 URL を開くことになる。

**ID（管理番号）は Salesforce のレポート ID ではない。** 社内で決める論理的な番号で、
同じ意味のデータを指す限り変えない。参照先の Salesforce レポートを差し替えても、
利用側の Python コード（`CUSTOMER_LIST = "1001"`）は変えずに済む。

**出力先フォルダは Excel のセル（保存先列）には書かない。** `グループ` 列 +
`設定` シート（`group_settings.py`）の組み合わせで Python 側で組み立てる
（`src.paths.report_folder()`）。Excel の数式で組み立てる案は openpyxl が数式
セルを信頼できないため採用しなかった。

**2026-09 から `担当者` / `概要` は出力パスに使わなくなった。** 管理表には
残してある（記録用のため）が、フォルダ階層の組み立てには影響しない。

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
from pathlib import Path

from comken.core.timer import measure
from comken.exceptions import SalesforceReportIDNotFoundError
from comken.toolbox.salesforce.report import report_id_from_url

from src.report_master import MasterRow, column

logger = logging.getLogger(__name__)

# 記入例（雛形に入れる）。2行目は別のレポートにする——同じ URL を並べると、
# check が「同じレポートを指している」と報告してしまう
_DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"
EXAMPLES = [
    {
        "key": "1001",
        "group": "営業本部",
        "assignee": "山田太郎",
        "summary": "顧客一覧",
        "url": f"{_DOMAIN}/00O5g00000ABCDE/view",
        "enabled": True,
        "allow_empty": False,  # 普段はデータがあるが、念のため「×」（既定）
        "exceeds_row_limit": False,  # 2000行に収まる通常のレポート（既定）
        "use_soql": False,  # Report API のまま（既定）
    },
    {
        "key": "1002",
        "group": "営業本部",
        "assignee": "佐藤花子",
        "summary": "売上実績",
        "url": f"{_DOMAIN}/00O5g00000FGHIJ/view",
        "enabled": True,
        "allow_empty": True,  # 「該当データ無し」が普通に起きるレポートの例
        "exceeds_row_limit": False,
        "use_soql": False,
    },
]


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
    # **出力先の組み立て:** 「ベースパス（設定シート）」のみ。第1階層は
    # `group_settings.load_group_settings()` で引いたベースパス（Python 側で
    # 組み立てるので、フォルダ列を人が打つ必要は無い）。`assignee` / `summary`
    # は記録用に残してあるが、出力パスには影響しない
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
    url: str = column(
        "Salesforce URL",
        help="Salesforce でレポートを開いたときのアドレスを、そのまま貼り付けてください。"
        "レポート ID を抜き出す必要はありません",
    )
    # **既定値を持たせない。** 空欄を「有効」にすると、書き忘れがそのまま有効になり、
    # 「まだ有効にしたくない」のか「書き方が分からず空にした」のか区別できなくなる。
    # `choices` で「○」「×」に統一（「有効/無効」と混在させない）。表記が1つに絞られるため、
    # ドロップダウンからの選択・表記ルールの案内が雛形から読み取れる
    enabled: bool = column(
        "有効",
        choices=("○", "×"),
        help="「○」か「×」と書いてください。使わなくなったら「×」にし、"
        "行は消さないでください（過去の履歴と対応が取れなくなります）",
    )
    # **既定値 `×`（＝「普段はデータがある」）を持たせる。** 意味が反転する列だが、
    # この列だけ事情が違う:
    # - 書き忘れると `×` になり、**厳しい側（エラー）へ倒れる**。
    #   誤報が出るだけで、データは失われない（運用側で `○` に直せば正しくなる）
    # - 既定値を持つ列は**見出しごと無くても読める**。列を足した瞬間に既存管理表が
    #   すべて読めなくなり全プロジェクトの業務が止まる事故を防ぐ
    # `choices` で `○` `×` 以外を弾く（既定の bool 変換は一覧に無い文字を黙って
    # `False` として通すため危険）。bool 列の `choices` は1つ目を True、2つ目を
    # False の表記として雛形へ書き出す
    allow_empty: bool = column(
        "0件あり",
        choices=("○", "×"),
        default=False,
        help="その日のデータが 0 件になることがあるレポートなら「○」。"
        "「×」のときに 0 件だとエラーになります"
        "（指しているレポートが違う可能性に気づけるようにするため）",
    )
    # **既定値 `×`。** 書き忘れても通常の Report API 経由のまま動く
    # （安全側＝挙動が変わらない側に倒す）。「○」にすると取得実行側が
    # ブラウザ経由（画面のエクスポート機能）に切り替える。SOQL 列が「○」の
    # 行では、この列の値に関わらず SOQL 経由が優先される（下記参照）
    exceeds_row_limit: bool = column(
        "2000件超",
        choices=("○", "×"),
        default=False,
        help="Report API の2000行上限を超えることが分かっているレポートなら「○」。"
        "取得実行側がブラウザ経由（画面のエクスポート機能）に切り替えます。"
        "「SOQL」列が「○」のときはこの列より優先されます",
    )
    # **既定値 `×`。** 書き忘れても通常の Report API 経由のまま動く。
    # 「○」にする場合は、同じ管理番号（`key`）の `SoqlReport.KEY` を
    # `src.soql_reports` へ登録しておくこと
    # （無いと設定ミスとして例外で止まる）
    use_soql: bool = column(
        "SOQL",
        choices=("○", "×"),
        default=False,
        help="SOQL化済みで、同じ管理番号のSOQL定義が"
        "src.soql_reports に登録されているなら「○」。"
        "取得実行側はSOQL経由に切り替えます（「2000件超」列より優先）",
    )

    @property
    def report_id(self) -> str:
        """URL から取り出した Salesforce のレポート ID。

        **行番号ではなく管理番号で示す。** 空行を飛ばして読むので行番号はズレうるが、
        管理番号なら管理表を検索して一発で見つかる。

        Raises:
            SalesforceReportIDNotFoundError: URL からレポート ID を取り出せない場合。
        """
        try:
            return report_id_from_url(self.url)
        except SalesforceReportIDNotFoundError as e:
            # ``report_id_from_url`` のメッセージは URL の生テキストしか含まない。
            # 管理表読み込み時は「どの管理番号か」も出したいので、管理番号を添えて上げ直す。
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

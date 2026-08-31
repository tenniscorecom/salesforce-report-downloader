r"""comken_salesforce_downloader/master.py — レポート管理表の列を決める。

**このファイルにあるのは「社内の取り決め」だけ。** Excel を読む・検証する・雛形を作る
仕組みは `comken_salesforce_downloader.report_master` にあり、ここは
**どんな列があるか**を宣言する。

    | ID   | 概要     | Salesforce URL              | 実行方式 | 保存先              | 有効 |
    |------|----------|-----------------------------|----------|---------------------|------|
    | 1001 | 顧客一覧 | https://.../Report/00O.../  | 定期     | \\server\A\input    | 有効 |

**Salesforce のレポート ID は入力させない。** URL を貼れば `report_id_from_url()` が
取り出す。ID を人が抜き出す工程を挟むと、そこで写し間違いが起きるうえ、
「どのレポートか」を確かめるには結局 URL を開くことになる。

**ID（管理番号）は Salesforce のレポート ID ではない。** 社内で決める論理的な番号で、
同じ意味のデータを指す限り変えない。参照先の Salesforce レポートを差し替えても、
利用側の Python コード（`CUSTOMER_LIST = "1001"`）は変えずに済む。

このファイルが持つもの:
- 管理表にどんな列があるか
- 各列の意味と書き方
- URL からレポートIDを取り出すこと

ここに書かないもの:
- Excel をどう読むか・どう検証するか → report_master.py
- 取得・保存・履歴 → service.py / history.py
"""

from dataclasses import dataclass
from pathlib import Path

from comken.core.timer import measure
from comken.exceptions import SalesforceReportIDNotFoundError
from comken.toolbox.salesforce.report import report_id_from_url

from comken_salesforce_downloader.exceptions import InvalidReportURLError
from comken_salesforce_downloader.report_master import MasterRow, column

# 「実行方式」に書ける値
SCHEDULED = "定期"
ON_DEMAND = "個別"


# 記入例（雛形に入れる）。2行目は別のレポートにする——同じ URL を並べると、
# check が「同じレポートを指している」と報告してしまう
_DOMAIN = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report"
# 雛形に入れる「消してよい行」だと示すための備考。記入例2行の「備考」セルに書く
_EXAMPLE_NOTE = "記入例です。使う前にこの行を消してください"
EXAMPLES = [
    {
        "key": "1001",
        "summary": "顧客一覧",
        "url": f"{_DOMAIN}/00O5g00000ABCDE/view",
        "schedule": SCHEDULED,
        "folder": r"\\server\案件集計\input",
        "enabled": True,
        "allow_empty": False,  # 普段はデータがあるが、念のため「×」（既定）
        "note": _EXAMPLE_NOTE,
    },
    {
        "key": "1002",
        "summary": "売上実績",
        "url": f"{_DOMAIN}/00O5g00000FGHIJ/view",
        "schedule": ON_DEMAND,
        "folder": r"\\server\売上帳票\input",
        "enabled": True,
        "allow_empty": True,  # 「該当データ無し」が普通に起きるレポートの例
        "note": _EXAMPLE_NOTE,
    },
]


@dataclass(frozen=True, kw_only=True)
class ReportEntry(MasterRow):
    """レポート管理表の1行。"""

    SHEET_NAME = "管理表"
    # 「記入方法」シートの冒頭に出す案内。docs/ を読まない編集者へ向けた唯一の
    # 1次情報として、ここに置く（マス目を増やす必要が無いよう簡潔に）
    # 「この表」と書くと、読んでいる「記入方法」シートに足すように見えるので、
    # シート名で言う（SHEET_NAME から採って、名前を変えても食い違わないようにする）
    GUIDE_INTRO = (
        f"「{SHEET_NAME}」シートに行を足すだけで、新しいレポートを取得できます。"
        "プログラム（コード）を直す必要はありません。"
    )

    key: str = column(
        "ID",
        unique=True,
        help="社内で決める管理番号。Salesforce のレポート ID ではありません。"
        "参照先のレポートを差し替えても、この番号は変えません。"
        "前ゼロ（0001 など）や記号入りの値も使えます",
    )
    summary: str = column(
        "概要", help="人が読んで何のレポートか分かる説明。保存するファイル名にも使われます"
    )
    url: str = column(
        "Salesforce URL",
        help="Salesforce でレポートを開いたときのアドレスを、そのまま貼り付けてください。"
        "レポート ID を抜き出す必要はありません",
    )
    schedule: str = column(
        "実行方式",
        choices=(SCHEDULED, ON_DEMAND),
        help=f"「{SCHEDULED}」は毎日決まった時刻にまとめて取ります。"
        f"「{ON_DEMAND}」は、使うプログラムから呼ばれたときだけ取ります",
    )
    folder: Path = column(
        "保存先",
        help="落としたファイルを置くフォルダ。フォルダが無いとエラーになります"
        "（打ち間違いに気づけるよう、勝手には作りません）",
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
    # いちばん右に置く（読み取りに使う列の後ろ）。業務側の覚え書き用。
    # 既定値 `""`（書かなくてよい列）。
    note: str = column(
        "備考",
        default="",
        help="編集者の覚え書き。空のままで構いません",
    )

    @property
    def report_id(self) -> str:
        """URL から取り出した Salesforce のレポート ID。

        **行番号ではなく管理番号で示す。** 空行を飛ばして読むので行番号はズレうるが、
        管理番号なら管理表を検索して一発で見つかる。

        Raises:
            InvalidReportURLError: URL からレポート ID を取り出せない場合。
        """
        try:
            return report_id_from_url(self.url)
        except SalesforceReportIDNotFoundError as e:
            raise InvalidReportURLError(self.key, self.url, str(e)) from e

    @property
    def is_scheduled(self) -> bool:
        """定期取得の対象か。"""
        return self.schedule == SCHEDULED


@measure
def load_master(path: str | Path | None = None) -> dict[str, ReportEntry]:
    """管理表を読んで、管理番号をキーにした辞書を返す。

    Args:
        path: 管理表（Excel）のパス。

    Returns:
        {管理番号: ReportEntry}。管理表に並んでいる順を保つ。
    """
    entries = {}
    for entry in ReportEntry.load(path):
        entry.report_id  # noqa: B018 — URL が壊れていれば、ここで読み込みごと止める
        entries[entry.key] = entry
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
    return {report_id: keys for report_id, keys in by_report_id.items() if len(keys) > 1}


r"""src/run.py — Salesforce のレポートをまとめて落として、決まった場所へ置く。

管理表（レポート一覧.csv）に書いたレポートを順に落とし、**行ごとの置き場所**へ
「名前_日付.csv」で保存する。何を落とすかはコードに書かない——レポートは増えるので、
増えるたびにコードを直す形にしないため。

    レポート一覧.csv
        名前,レポートURL,置き場所
        案件一覧,https://.../lightning/r/Report/00O.../view,\\server\案件集計\input

    → \\server\案件集計\input\案件一覧_20260814.csv

**置き場所には、使う側が読みに行くフォルダをそのまま書く。** 中間の受け渡しフォルダは
挟まない（挟んでも、そこから配る処理が増えるだけで得がない）。同じレポートを2つの
プロジェクトが使うなら、行を2つ書いて2回落とす。

**組織は URL のドメインで決まる。** 管理表に組織を選ぶ列は作らない。人が選ぶ形にすると、
URL と食い違ったときに別の組織へ問い合わせて「レポートが無い」という分かりにくい失敗に
なる。組織ごとにまとめてから接続するので、認証は組織につき1回で済む。

決めてあること:

1. **すでに今日の分が置いてあれば取りに行かない。** 落とし直しを省くためだけでなく、
   **人が手で置いたファイルを上書きしないため**。取得が失敗した分を手で置いて再実行
   すれば、残りだけを取りに行く。
2. **1件失敗しても残りは続ける。** 組織ごと落ちた場合も、その組織の分だけを失敗にする。
   5本のうち1本が落ちたときに全部やり直すと、手で用意する手間が5本ぶんになる。
3. **0 行は成功にしない。** 空の CSV を置くと、使う側は「データが無い日」と
   「取得が失敗した日」を区別できなくなる。
4. **置き場所のフォルダが無ければ作らずに失敗させる。** 無いということは書き間違いの
   可能性が高く、黙って新しいフォルダを作ると、誰も読まない場所に置き続けてしまう。
5. **失敗があれば最後に例外で止める。** 途中で続けたぶん、終了コードで必ず報せる。
   ログだけに出して正常終了すると、RPA 基盤から見て成功と区別が付かない。
"""

import logging
from pathlib import Path

from comken.csv import CsvReader, CsvWriter
from comken.exceptions import ComkenError
from comken.salesforce import SalesforceBase, report_id_from_url
from comken.salesforce.sites import site_for
from comken.utils import Timer
from comken.utils.clock import today

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 管理表。config.ini は置かない——変えるのは管理表の中身であって、管理表の場所ではない
REPORT_LIST_PATH = PROJECT_ROOT / "レポート一覧.csv"

# API の計測（所要時間・リトライ回数・24 時間の API 消費量）の追記先。
# 1回ぶんはログにも出るが、CSV に貯めておくと「先週より遅い」「リトライが増えた」が分かる
METRICS_CSV_PATH = PROJECT_ROOT / "logs" / "API計測.csv"

# 管理表の列名。実物の見出しと合わせる
NAME_COLUMN = "名前"
URL_COLUMN = "レポートURL"
FOLDER_COLUMN = "置き場所"

# 置くファイルの名前の形。使う側は FileFinder(...).today() でこの日付を頼りに探す
DATE_FORMAT = "%Y%m%d"
SUFFIX = ".csv"


def run() -> None:
    """管理表のレポートをまとめて落とし、行ごとの置き場所へ置く。

    Raises:
        DownloadFailedError: 1件でも落とせなかった場合（残りは落としたうえで送出する）。
    """
    reports = CsvReader(REPORT_LIST_PATH).read_rows()
    logger.info("管理表を読みました: %d 件", len(reports))

    pending, skipped, failed = _group_by_site(reports)
    for site, rows in pending.items():
        failed.extend(_download_site(site, rows))
    _finish(reports, skipped, failed)


def _group_by_site(
    reports: list[dict],
) -> tuple[dict[type[SalesforceBase], list[dict]], list[dict], list[dict]]:
    """まだ置かれていないレポートを組織ごとにまとめる。組織を引けなかったものは失敗にする。

    **名前ではなく行そのもので数える。** 同じレポートを2か所へ置くときは同じ名前の行が
    2つ並ぶので、名前で扱うと片方しか報告されない。

    Returns:
        （組織クラス → その組織のレポート行, すでに置かれていた行, 組織を引けなかった行）。
    """
    pending: dict[type[SalesforceBase], list[dict]] = {}
    skipped: list[dict] = []
    failed: list[dict] = []
    for report in reports:
        path = _path_of(report)
        if path.is_file():
            # 手で置いたものも「置いてある」として扱う。上書きしない
            logger.info("すでに置かれているので飛ばします: %s", path)
            skipped.append(report)
            continue
        try:
            site = site_for(report[URL_COLUMN])
        except ComkenError as e:
            logger.error("組織を特定できません: %s（%s）", report[NAME_COLUMN], e)
            failed.append(report)
            continue
        pending.setdefault(site, []).append(report)
    return pending, skipped, failed


def _download_site(site: type[SalesforceBase], reports: list[dict]) -> list[dict]:
    """1つの組織へつないで、その組織のレポートを順に落とす。失敗した行を返す。"""
    failed: list[dict] = []
    handled = 0
    try:
        with site() as salesforce:
            try:
                for report in reports:
                    try:
                        _download(salesforce, report)
                    except ComkenError as e:
                        # 1件の失敗で残りを落とさない。何が失敗したかは最後にまとめて出す
                        logger.error("取得に失敗しました: %s（%s）", report[NAME_COLUMN], e)
                        failed.append(report)
                    handled += 1
            finally:
                # 失敗した実行こそ、時間とリトライの記録が要る。成功時だけにしない
                _record_metrics(salesforce)
    except ComkenError as e:
        # 認証やネットワークで組織ごと落ちた場合。ほかの組織は続ける。
        # まだ手を付けていない残りだけを失敗にする（処理済みの分を二重に数えない）
        logger.error("%s へつなげませんでした（%s）", site.__name__, e)
        failed.extend(reports[handled:])
    return failed


def _download(salesforce: SalesforceBase, report: dict) -> None:
    """レポート1本を落として、置き場所へ CSV で書く。"""
    name = report[NAME_COLUMN]
    path = _path_of(report)
    if not path.parent.is_dir():
        # 作らずに失敗させる。無いのは書き間違いのことが多く、勝手に作ると
        # 誰も読まない場所へ置き続けることになる
        raise DestinationFolderNotFoundError(name, path.parent)

    # 何秒かかったかを1本ずつ残す。遅いレポートが分かると、絞り込みや SOQL 化の
    # 判断材料になる（タイムアウトは 60 秒で SalesforceConnectionError になる）
    with Timer(f"{name} の取得"):
        rows = salesforce.report.run(report_id_from_url(report[URL_COLUMN]))

    if not rows:
        # 0 行は「取れた」と言い切れないので、置かずに失敗として扱う
        raise EmptyReportError(name, report[URL_COLUMN])

    CsvWriter(path, list(rows[0])).write_rows(rows)
    logger.info("置きました: %s（%d 行）", path, len(rows))


def _record_metrics(salesforce: SalesforceBase) -> None:
    """API の計測をログと CSV に残す。

    呼び出し回数・エラー回数・リトライ回数（再認証 / サーバーエラー / 制限超過）・
    合計秒数・24 時間の API 消費量が入る。**問題が起きたときに見るものなので、
    失敗した実行でも必ず残す。**
    """
    salesforce.metrics.log_summary()
    salesforce.metrics.append_csv(METRICS_CSV_PATH)


def _path_of(report: dict) -> Path:
    """その行のレポートを置くパスを組み立てる（実在は見ない）。

    使う側はこの名前を頼りに探すので、**手で置くときも同じ名前にする**。
    """
    name = f"{report[NAME_COLUMN]}_{today().strftime(DATE_FORMAT)}{SUFFIX}"
    return Path(report[FOLDER_COLUMN]) / name


def _finish(reports: list[dict], skipped: list[str], failed: list[str]) -> None:
    """結果をまとめてログに出し、失敗があれば例外で止める。

    **ログの末尾だけを見れば状況が分かるよう、件数の内訳を必ず1行出す。**
    成功した日と失敗した日でログの形が変わると、どこを見ればよいか分からなくなる。
    """
    downloaded = len(reports) - len(skipped) - len(failed)
    logger.info(
        "結果: %d 件中 %d 件を置き、%d 件はすでにあり、%d 件が失敗しました。",
        len(reports),
        downloaded,
        len(skipped),
        len(failed),
    )
    if not failed:
        return

    logger.error("取得できなかった %d 件。手で置く場合の置き場所:", len(failed))
    for report in failed:
        logger.error("  %s", _path_of(report))
    raise DownloadFailedError(
        [report[NAME_COLUMN] for report in failed], [_path_of(report) for report in failed]
    )


class EmptyReportError(ComkenError):
    """レポートは取れたが明細が 0 行だった。

    comken 側の例外にしないのは、0 行を異常と見るかが業務ごとに違うため。
    「毎日必ず何か入っている」と分かっているこのプロジェクトでだけ失敗にする。
    """

    def __init__(self, name: str, url: str) -> None:
        super().__init__(
            f"レポートの明細が 0 行でした: {name}\n"
            f"{url}\n"
            "取得の失敗と区別できないため、ファイルは置きません。\n"
            "本当に 0 件の日であれば、空の CSV を手で置いてください。"
        )


class DestinationFolderNotFoundError(ComkenError):
    """管理表に書かれた置き場所のフォルダが無い。"""

    def __init__(self, name: str, folder: Path) -> None:
        super().__init__(
            f"置き場所のフォルダがありません: {name}\n"
            f"{folder}\n"
            "レポート一覧.csv の「置き場所」を確認してください。\n"
            "共有フォルダの場合は、つながっているか（権限があるか）も確認してください。"
        )


class DownloadFailedError(ComkenError):
    """1件以上のレポートを落とせなかった（落とせたものは置いてある）。"""

    def __init__(self, names: list[str], paths: list[Path]) -> None:
        places = "\n".join(f"  {path}" for path in paths)
        super().__init__(
            f"{len(names)} 件のレポートを取得できませんでした: {', '.join(names)}\n"
            f"手で置く場合は、次の場所へ同じ名前で置いてください:\n{places}\n"
            "置いてから、もう一度実行してください（すでに置いたものは飛ばします）。"
        )

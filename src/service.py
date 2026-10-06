r"""src/service.py — 取得の本体。

管理表・スケジュール・SOQL・取得の実行と「管理表の行から履歴に書く値を組み立てる
部分（``record()``）」を 1 つのリポジトリ（`src/`）に置いた形の本体。

    from src.service import download_scheduled

    CUSTOMER_LIST = "1001"        # 各プロジェクトで、意味の分かる名前を付ける

`download_scheduled()` は「**今この瞬間にまとめて取りに行く**」。管理表で
`有効` になっているレポートを全て対象に、定期実行のプロジェクトから呼ばれる。
戻り値は `list[Path]` で、定期取得の呼び出し側が中身を読まず「取らせる」
のが目的なので、`Table` を返さない（役割の違いが戻り値の型に出ている）。

**境界は履歴（ダウンロード履歴.csv）。** 履歴の形式・読み取り・ロック・置き場所は
``comken.services.salesforce_downloader.history`` / ``history_file_lock`` /
``paths`` 側に集約され、ダウンローダーは自分で持たない。``record()``
（``src.history``）だけがダウンローダーに残った「管理表の行から履歴に書く値を
組み立てる」部分で、実際の書き込みは comken の ``append_history()`` へ委譲する。

**急いでその場の最新値が必要なときは、`download_scheduled()` をスケジュール外に
直接実行する。** それ専用のAPIは用意していない。実行タイミングを分ける必要が
あれば、呼び出し側でスケジューラを増やす。Downloader 側に「今すぐ取りに行く」
だけの関数を残すと、定期取得が動いていないことに誰も気づかなくなるため。

プロジェクト側のコードに Salesforce の URL もレポート ID も現れない。管理表の
参照先を差し替えても、`CUSTOMER_LIST = "1001"` はそのままでよい。

このファイルが持つもの:
- 1件を取得して保存し履歴に残す流れ
- Salesforce への問い合わせ

ここに書かないもの:
- 「このプロジェクトのときは」という分岐 → main.py / src/run.py
- 管理表にどんな列があるか → src.sheets.master
- 履歴の形式・読み取り関数 → comken.services.salesforce_downloader.history
- 履歴のロック → comken.services.salesforce_downloader.history_file_lock
- 履歴の置き場所（`HISTORY_PATH`）→ comken.services.salesforce_downloader.paths
- 履歴への値の組み立て（`record()`）→ src.history
- Salesforce の認証・API の叩き方 → comken/toolbox/salesforce/
- 取得済みファイルの取り出し（`output_path`）→ src.paths
- 管理表の置き場所（`MASTER_PATH`）→ src.paths
- 管理表から1行を引く `_find()` → src.paths（`requests` を経由しない側に置く）
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from comken.core.dates import now as clock_now
from comken.core.files import atomic_write
from comken.core.table.model import Table
from comken.core.timer import measure
from comken.exceptions import (
    ComkenError,
    CSVError,
    HistoryLockTimeoutError,
    HistoryWriteError,
    SalesforceReportTruncatedError,
)
from comken.runtime import is_dry_run
from comken.services.salesforce_downloader import history
from comken.services.salesforce_downloader import paths as history_paths
from comken.services.salesforce_downloader.history import (
    ROUTE_API,
    ROUTE_BROWSER,
    ROUTE_BROWSER_FALLBACK_EMPTY,
    ROUTE_BROWSER_FALLBACK_TRUNCATED,
    ROUTE_SOQL,
    HistoryRow,
)
from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock
from comken.toolbox.csv import CSV
from comken.toolbox.salesforce.sites import site_for

from src import paths
from src.exceptions import (
    BrowserFallbackFailedError,
    EmptyReportError,
    ReportFolderNotFoundError,
    ReportNotRegisteredError,
    ReportReservePathLimitError,
    ScheduledDownloadFailedError,
)
from src.history import record
from src.sheets.master import (
    ReportEntry,
    load_master,
    shared_report_ids,
)
from src.sheets.schedule import ScheduleRule, load_schedule
from src.soql_reports import soql_report_for

if TYPE_CHECKING:
    # ブラウザ経由の戻り値の型だけ。``selenium`` 依存を実際にブラウザ経由の
    # レポートを使うときだけ読み込むために、``_fetch_via_browser()`` 内で
    # import する。``from __future__ import annotations`` で注釈を文字列扱いに
    # しているので、``TYPE_CHECKING`` に閉じてよい（無いと Python 3.13 以下で
    # import 時に NameError）
    from comken.toolbox.browser.sites.salesforce.base import SalesforceReportBrowser

logger = logging.getLogger(__name__)

# 履歴の「原因区分」列に出す5値。運用する人が履歴からすぐ「誰が動くか」を判断できるように、
# 抽象クラス名ではなく誰が直すかで分ける（設計判断は docs/開発/仕様書.md 4.28 参照）
CAUSE_CONFIG = "設定"
CAUSE_SALESFORCE = "Salesforce"
CAUSE_EMPTY_DATA = "データなし"
CAUSE_FILE = "ファイル"
CAUSE_PROGRAM = "プログラム"

# ``_reserve_path`` が連番を足して空きファイル名を探索する回数の上限。
# ``comken.core.dates.WORKDAY_SEARCH_LIMIT`` と同じ理由で、
# 共有サーバーの同期・権限異常などで ``FileExistsError`` が返り続けると無限
# ループになるため、必ず上限を切る。
RESERVE_PATH_LIMIT = 1000


def _history_path() -> Path:
    """呼び出し時点の履歴CSVのパスを ``Path`` で返す。

    履歴CSVの置き場は ``comken.services.salesforce_downloader.paths.HISTORY_PATH``
    で、テストでは ``monkeypatch.setattr`` で comken 側のモジュール属性を
    差し替える運用なので、呼び出し時点で ``history_paths.HISTORY_PATH`` を読む
    （``from ... import HISTORY_PATH`` で名前を固定すると差し替えが効かない）。
    """
    return Path(history_paths.HISTORY_PATH)


@measure
def download_scheduled(
    project: str = "定期実行",
    *,
    filters_by_report: dict[str, list[dict]] | None = None,
) -> list[Path]:
    """管理表で有効なレポートをまとめて取得する。

    定期実行のプロジェクトから呼ぶ。**1件失敗しても残りは続ける**。戻り値は `list[Path]`
    のままで `CSV` を返さない（定期取得の呼び出し側は中身を読まないため）。

    ``filters_by_report`` は、管理番号ごとに Salesforce Report API の実行時フィルタを
    指定する。指定のない管理番号は、Salesforce に保存されているレポート条件のまま実行する。
    URL やレポート ID ではなく管理番号をキーにするため、管理表で参照先を差し替えても
    呼び出し側のコードは変えなくてよい。

    **API・ブラウザ経由・SOQLのどれで取るかは、呼び出し側のコードではなく管理表の
    「2000件超」「SOQL」列で決まる**（`_fetch()` を参照）。呼び出し側は管理番号を
    意識せずに `download_scheduled()` を呼ぶだけでよい。ブラウザ経由のレポートは、
    ブラウザを開いた直後に ``login_with_credentials()`` を呼び、ID/パスワードを
    DPAPI から自動入力したうえで MFA をスマホで承認する（無人で承認されなければ
    ``LoginFailedError`` として失敗扱いになる。認証情報が未登録なら
    ``CredentialNotFoundError`` が送出される。詳しくは `_fetch_via_browser()` を
    参照）。SOQL経由のレポートは同じ管理番号の `SoqlReport` が `src.soql_reports`
    に登録されている必要がある（詳しくは `_fetch_via_soql()` を参照）。

    **ブラウザの使い回し:** ブラウザ経由のレポートは、1 回の `download_scheduled()`
    の実行の中で組織ごとにブラウザを 1 つだけ開いて使い回す（毎回起動しない）。
    取得中に例外が出たブラウザはその場で閉じて dict から外し、次のレポートでは
    新しいブラウザを開き直す（壊れた状態のブラウザを次のレポートへ引き継がない）。

    **「スケジュール」シート**にこのレポートの行が無いときは、
    「有効」だけで毎回対象にする（後方互換）。
    この機能追加を境に既存のレポートが突然取得されなくなる事故を防ぐため。

    **同時起動の扱い:** WinActor からの呼び出しが重なったり、前回の取得が
    長引いて 2 回目のポーリングが食い違うと、同じレポートを 2 プロセスが
    「今日は未取得」と判定して二重に取得する事故が起きる。これを防ぐため、
    この関数全体（対象選定〜履歴追記の完了まで）を `HistoryFileLock` で
    プロセス間排他する。ロックが取れない場合は**例外にせず**ログだけ出して
    空リスト ``[]`` を返す（終了コード0）。別の実行が進行中なら、それが
    今回の分の取得も担当するため、次のポーリングで追従できる。失敗扱いに
    すると運用者に誤報が飛ぶため、敢えて正常終了にする。

    **実行日時の扱い:** 23:59 に始まった取得が日付をまたいで終わると、
    判定・出力ファイル名・履歴の「実行日時」がバラバラの日付になり、
    翌日のスケジュールキーが「昨日成功済み」で飛ぶ事故が起きる。これを
    防ぐため、関数冒頭で 1 回取った `clock_now()` の値を全工程（スケジュール
    判定・``output_path``・履歴の「実行日時」）で共有する。

    Args:
        project: 履歴の「プロジェクト」列へ残す呼び出し元の名前。
        filters_by_report: ``{管理番号: [Report APIフィルタ, ...]}``。各フィルタは
            ``{"column": ..., "operator": ..., "value": ...}`` の形で指定する。
            ブラウザ経由・SOQL経由のレポートには指定できない（``ValueError``）。

    Returns:
        保存したファイルのパス一覧。1件も取得対象が無いとき・別の実行が
        進行中でロックが取れなかったときは空リスト ``[]``。

    Raises:
        ReportNotRegisteredError: ``filters_by_report`` に管理表未登録の管理番号がある場合。
        SoqlReportNotRegisteredError: 管理表の「SOQL」列が「○」なのに、同じ管理番号の
            `SoqlReport` が登録されていない場合。
        ScheduledDownloadFailedError: 1件でも取得できなかった場合。**取得できたものは
            保存したうえで**送出する。ログだけに出して正常終了すると、スケジューラや
            RPA 基盤から見て成功と区別が付かない。
    """
    # **同時起動防止:** 履歴CSVとは別のロックファイル
    # （``{history_path}.run.lock``）で、対象選定〜履歴追記をひとまとまりに
    # プロセス間排他する。ロックが取れないときは別プロセスが進行中なので、
    # 例外にせず警告ログだけ出して ``[]`` を返す。
    # ``HistoryFileLock`` は名前のとおり履歴用ロックの部品だが、Windows の
    # msvcrt ロックはファイル単位なので、``history_path`` とは違うファイルを
    # 渡せば履歴読み書き（``{history_path}.lock``）と衝突しない。
    # ``ExitStack`` を使い、ロック取得**だけ**を try/except で囲う。本体側で
    # ``HistoryLockTimeoutError`` が上がっても、それは履歴読み書きの障害なので
    # そのまま伝播させる（``[]`` で握りつぶすと「履歴の障害」が別の実行に紛れて
    # 隠れる）。
    history_path = _history_path()
    run_lock_path = Path(f"{history_path}.run")
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(HistoryFileLock(run_lock_path, timeout=0))
        except HistoryLockTimeoutError:
            # 進行中の実行が今回の分の取得も担当する。失敗扱いにすると
            # WinActor / RPA 基盤から見て誤報になるため、空リスト + 正常終了
            logger.warning("別の実行が進行中のため今回はスキップします: %s", run_lock_path)
            return []
        return _download_scheduled_locked(project, filters_by_report=filters_by_report)


def _download_scheduled_locked(
    project: str,
    *,
    filters_by_report: dict[str, list[dict]] | None,
) -> list[Path]:
    """``download_scheduled()`` のロック取得後に動く本体。

    実行ロックを ``download_scheduled()`` で取得し、その内側で動くコア部分を
    切り出したヘルパー。``download_scheduled()`` 自体は薄いラッパーに
    留めて、本体の長さ・複雑度を上げないようにする。
    """
    entries = load_master(paths.MASTER_PATH)
    filters_by_report = filters_by_report or {}
    _validate_filters_by_report(filters_by_report, entries)
    _warn_shared_reports(entries)

    # スケジュール管理表を読んで、レポートキーで引けるように索引化。**有効行だけ**を
    # 評価対象にする（無効行は曜日・時刻を足切りする材料にならない）。
    # シートが無い場合は空リスト（後方互換）
    schedule_rules = load_schedule(paths.MASTER_PATH)
    rules_by_report: dict[str, list[ScheduleRule]] = {}
    for rule in schedule_rules:
        if rule.enabled:
            rules_by_report.setdefault(rule.report_key, []).append(rule)

    # **この実行の基準日時を 1 か所で固定。** スケジュール判定・ファイル名・
    # 履歴の「実行日時」の 3 か所が同じ ``current`` を見ることで、23:59 に
    # 始まった取得が日付をまたいでも、開始日で揃う（翌日 00:01 に終わった
    # として履歴が残ると、翌日の同じスケジュールキーが「成功済み」で飛ぶ
    # 事故を防ぐ）。
    current = clock_now()

    targets, already_failed = _select_targets(entries, rules_by_report, current)
    logger.info("定期取得の対象: %d 件", len(targets))

    saved: list[Path] = []
    # 1件失敗したら ``failed`` に積む。RPA 基盤から「失敗扱い」を判断できるよう、
    # 末尾で ``ScheduledDownloadFailedError`` を送出して終了コード1 にする
    failed: list[str] = list(already_failed)
    # 失敗時に ``ScheduledDownloadFailedError`` から ``__cause__`` で辿れるよう、
    # 直近の捕捉した例外を覚えておく（同じ失敗が複数件あっても、最後の1件だけを
    # 連鎖させる）。``None`` のままだと「原因例外が無い」ことを示す
    last_exception: BaseException | None = None
    # ブラウザ経由のレポートで「同じ組織のブラウザを 1 回の実行で使い回す」ための入れ物。
    # ``site_for()`` が返す ``type`` をキーに、開いたインスタンスを保持する。
    # 最終的に ``finally`` で全部 ``__exit__()`` を呼んで閉じる（正常終了・想定外
    # 例外どちらでも）。``_fetch_via_browser()`` 側で取得中に例外が出た場合は、
    # 該当エントリを ``pop`` して ``__exit__()`` を呼んだうえで例外を上げる
    # （壊れた状態のブラウザを次のレポートへ引き継がないため）
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] = {}
    try:
        for entry, schedule_key, schedule_rule in targets:
            try:
                saved.append(
                    _download(
                        entry,
                        project,
                        _history_path(),
                        schedule_key,
                        schedule_run_time=(
                            schedule_rule.desired_time if schedule_rule is not None else None
                        ),
                        filters=filters_by_report.get(entry.key),
                        current=current,
                        browser_sessions=browser_sessions,
                    )
                )
            except (ComkenError, OSError) as e:
                # **想定した失敗は続ける。想定していない失敗は止める。**
                # - `ComkenError` は `docs/ERRORS.md` に対処法が載っている想定内の失敗なので続行する
                # - `OSError` は共有サーバー断・権限・パスなど運用上の失敗。保存先は
                #   レポートごとに違うので、1本ダメでも他は書けるので続行する
                # - それ以外（`TypeError` などプログラムのバグ）は想定していない。
                #   `ScheduledDownloadFailedError`（＝「1件取れませんでした」）の顔で
                #   出てくると、非エンジニアが「もう一度実行してみる」を繰り返すだけなので、
                #   ここでは捕捉せず、その場で落として気づかせる
                # KeyboardInterrupt など処理中断を示す例外は `ComkenError` / `OSError` の
                #   どちらでもないので、これもそのまま抜ける
                logger.error("取得に失敗しました: %s（%s）", entry.key, e)
                failed.append(entry.key)
                last_exception = e
    finally:
        # 使い回していたブラウザを全部閉じる。``_fetch_via_browser()`` 側で取得中に
        # 例外が出たものは既に dict から外して ``__exit__()`` を呼んでいるので、
        # ここに残っているのは「成功裡に使い終わったブラウザ」だけ。
        # 想定外例外が ``for`` ループを抜けた場合もこの ``finally`` で閉じる
        for sf in list(browser_sessions.values()):
            try:
                # ``exc_type`` は ``None`` を渡す（``type(None)`` ≒ ``NoneType`` を
                # 渡すと、comken の ``BrowserSession.__exit__`` 側で
                # ``exc_type is not None`` が真になり、正常終了でも毎回エラー時
                # スクリーンショットが撮られてしまう）
                sf.__exit__(None, None, None)
            except Exception as close_exc:
                logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)

    logger.info("定期取得: %d 件中 %d 件を取得しました。", len(targets), len(saved))
    if failed:
        # 続けたぶん、最後に必ず知らせる（終了コードで落ちたことが分かるように）。
        # 直近の失敗を ``__cause__`` に乗せて送出する（呼び出し側が
        # ``raise X from original`` 相当の診断情報を得られるようにする）
        raise ScheduledDownloadFailedError(failed, _history_path()) from last_exception
    return saved


class _Attempt:
    """1件の取得（フォルダ確認 → 取得 → 保存）を取り持つ文脈。

    履歴を書く関数に毎回同じ値の組を渡す煩雑さを消すための箱。
    コンストラクタで文脈を1回だけ受け取り、開始時刻もここで `time.perf_counter()`
    で記録する。`record_*()` は履歴とログを書くだけ。`_download()` 側の
    `try/except` はそのまま（失敗の段階と原因区分は `_failure_row()` が
    例外の型だけから決める）。

    ``schedule_key`` はスケジュール行に紐付く取得で値が入り、スケジュール行が無い
    レポートの取得（後方互換）は空文字。``_matched_schedule_key()`` が
    戻り値の第2要素として返した値をそのまま受け取り、``HistoryRow.schedule_key``
    に詰めて履歴へ書く。
    """

    def __init__(
        self,
        entry: ReportEntry,
        project: str,
        history_path: Path,
        schedule_key: str = "",
        *,
        executed_at: dt.datetime | None = None,
    ) -> None:
        self._entry = entry
        self._project = project
        self._history_path = history_path
        self._schedule_key = schedule_key
        # この実行の開始時刻。``_download_scheduled_locked()`` で 1 回取った
        # ``clock_now()`` の値をそのまま運ぶ。日付をまたぐ長い実行でも、
        # 履歴の「実行日時」が開始時刻で揃う（=翌日分のスケジュールキーが
        # 「昨日成功済み」として誤判定されるのを防ぐ）
        self._executed_at = executed_at
        self._started = time.perf_counter()

    def record_failure(self, exc: BaseException, *, route: str = "") -> None:
        """失敗時の履歴とログ。"""
        row = _failure_row(
            exc,
            time.perf_counter() - self._started,
            schedule_key=self._schedule_key,
            route=route,
        )
        # 値を計算した場所（ここ）で、ログにも書く。`_download()` 抜けたあと
        # 別の層で「同じ値」を再利用することはない（後付けで属性を渡さない）
        logger.error("取得に失敗しました: %s（%s / 区分=%s）", self._entry.key, exc, row.cause)
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=self._executed_at,
        )

    def record_success(
        self,
        path: Path,
        rows: Table,
        *,
        route: str = "",
    ) -> None:
        """成功時の履歴とログ。"""
        seconds = time.perf_counter() - self._started
        row = HistoryRow(
            succeeded=True,
            fetched_from_salesforce=True,
            saved_to_file=True,
            file_name=path.name,
            row_count=len(rows),
            seconds=seconds,
            schedule_key=self._schedule_key,
            route=route,
        )
        if rows:
            logger.info("取得しました: %s（%d 行 / %.1f 秒）", path, len(rows), seconds)
        else:
            logger.info("取得しました: %s（0 件 / 0件ありのため正常）", path)
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=self._executed_at,
            # **履歴の「保存先」は実際にファイルを置いたフォルダを書く**。出力パスは
            # ベース配下に「概要」フォルダを掘った 2 階層になるので、 ``path.parent``
            # (= ベース / 概要) を渡す。渡さないと ``record()`` 側で
            # ``output_path().parent`` を組み立て直そうとして、概要の決定ロジックが
            # 履歴側に漏れ出す。
            folder=path.parent,
        )

    def record_success_with_route(
        self,
        path: Path,
        rows: Table,
        *,
        route: str,
        executed_at: dt.datetime | None,
    ) -> None:
        """成功履歴の記録（自動切替で経路を上書きする）。

        ``record_success()`` と ``record_failure()`` のどちらにも該当しない、
        「ブラウザに切り替えて成功」専用の経路。 ``HistoryRow.route`` に
        ``ROUTE_BROWSER_FALLBACK_*`` を入れて記録する（SOQL 化や管理表の修正が
        要るレポートを履歴から拾えるようにするため）。
        """
        seconds = time.perf_counter() - self._started
        row = HistoryRow(
            succeeded=True,
            fetched_from_salesforce=True,
            saved_to_file=True,
            file_name=path.name,
            row_count=len(rows),
            seconds=seconds,
            schedule_key=self._schedule_key,
            route=route,
        )
        logger.info(
            "取得しました（自動切替で成功）: %s（%d 行 / %.1f 秒 / route=%s）",
            path,
            len(rows),
            seconds,
            route,
        )
        record(
            self._history_path,
            entry=self._entry,
            project=self._project,
            row=row,
            executed_at=executed_at,
            folder=path.parent,
        )


def _download(
    entry: ReportEntry,
    project: str,
    history_path: Path,
    schedule_key: str = "",
    *,
    schedule_run_time: dt.time | None = None,
    filters: list[dict] | None = None,
    current: dt.datetime | None = None,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
) -> Path:
    """1件を取得して保存し、成否を履歴に残す。

    ``schedule_run_time`` は唯一の出力ファイル名にスケジュール時刻を埋め込むために
    ``_save()`` まで運ぶ（``paths.output_path(entry, schedule_run_time)`` で
    ``%Y%m%d_%H%M`` のタイムスタンプ値として使われる）。
    スケジュール行が無いレポート（後方互換、``schedule_key == ""``）は ``None``
    のままでよく、``_save()`` 側で現在時刻にフォールバックする。

    ``current`` は ``_download_scheduled_locked()`` 冒頭で固定した ``clock_now()``
    の値。``_save()`` 経由で ``paths.output_path(now=current)`` に渡し、
    ``_Attempt`` 経由で履歴の「実行日時」に渡す。日付をまたぐ長い実行でも
    判定・ファイル名・履歴が同じ「開始日」で揃うために 1 実行で同じ値を
    共有する。

    ``browser_sessions`` は ``_download_scheduled_locked()`` で作られた
    使い回し用の dict（同じ組織のブラウザを 1 回の実行で共有するため）。
    ``None`` のときは従来の「その場で開いて閉じる」動作（``_fetch()`` /
    ``_fetch_via_browser()`` 側のキーワード既定値に従う）。

    **自動切替のオーケストレーション**: Report API 経路（``use_soql=False`` かつ
    ``exceeds_row_limit=False``）のとき、

    - ``SalesforceReportTruncatedError`` → ブラウザ経由で取り直し
      （``route=ROUTE_BROWSER_FALLBACK_TRUNCATED``）
    - 0 行 + ``allow_empty=False`` → ブラウザ経由で取り直し
      （``route=ROUTE_BROWSER_FALLBACK_EMPTY``）。ブラウザでも 0 行なら
      ``EmptyReportError`` で失敗扱い（``Data=""`` のまま）

    を取り直す。「実行時フィルタ」 (``filters``) 付きのときはブラウザに
    機能制限があるので取り直さず、今どおりのエラーをそのまま上げる。
    """
    attempt = _Attempt(entry, project, history_path, schedule_key, executed_at=current)
    # ``route`` は例外処理（``record_failure``）でも参照するため ``try`` の外に
    # 置いて、``_fetch_with_auto_fallback`` が取り直して書き換えた値を反映させる。
    # 初期値は「不明」を示す空文字（``_Attempt.record_failure`` で
    # ``HistoryRow.route`` の既定値に揃える）
    route = ""
    try:
        try:
            _require_folder(entry)
            route, table = _fetch_with_auto_fallback(
                entry,
                filters=filters,
                browser_sessions=browser_sessions,
            )
            path = _save(entry, table, schedule_run_time=schedule_run_time, current=current)
        except EmptyReportError as exc:
            # 0 行 + ``allow_empty=False`` の「自動切替」が走る可能性がある唯一の場所。
            # ブラウザに切り替えて 0 行以外が取れればクリア、取れなければ ``EmptyReportError``
            # をそのまま上げる（=この場合は ``route`` は API のまま、ブラウザに切り替えて
            # いないことが履歴で分かる）。
            if (
                route == ROUTE_API
                and not entry.allow_empty
                and filters is None
                and browser_sessions is not None
                and _is_api_path(entry)
            ):
                logger.warning(
                    "Report API が 0 行を返したため、ブラウザ経由に自動で切り替えます: "
                    "%s（%s）。管理表の「0件あり」を「○」にするか SOQL 化を検討してください",
                    entry.key,
                    entry.summary,
                )
                try:
                    browser_table = _fetch_via_browser(entry, browser_sessions=browser_sessions)
                except Exception as browser_exc:
                    # 0 件取り直しのブラウザ取得自体が失敗 → 自動切替は成功しなかった扱いとし、
                    # 2000 件超の取り直し失敗と同じ ``BrowserFallbackFailedError`` で表現する
                    # （``DownloaderError`` 系なので ``_failure_row()`` で「Salesforce」区分に入る）
                    raise BrowserFallbackFailedError(
                        entry.key, entry.summary, "0件", ROUTE_BROWSER_FALLBACK_EMPTY
                    ) from browser_exc
                if browser_table:
                    # ブラウザで取れたので、保存し直して履歴は「成功・自動切替」で記録
                    # （``_Attempt`` の開始時刻はそのまま使い、route だけ書き換える）
                    path = _save(
                        entry,
                        browser_table,
                        schedule_run_time=schedule_run_time,
                        current=current,
                    )
                    attempt.record_success_with_route(
                        path,
                        browser_table,
                        route=ROUTE_BROWSER_FALLBACK_EMPTY,
                        executed_at=current,
                    )
                    return path
                # ブラウザでも 0 行 → ブラウザ経路を閉じて取り直し
                logger.warning(
                    "ブラウザ経由でも 0 行でした。EmptyReportError で失敗します: %s（%s）",
                    entry.key,
                    entry.summary,
                )
            # 取り直さない or 取り直しても 0 行だった → 失敗として記録
            try:
                attempt.record_failure(exc, route=route)
            except (
                HistoryWriteError,
                HistoryLockTimeoutError,
                CSVError,
            ) as history_exc:
                raise HistoryWriteError(history_path, str(history_exc), original=exc) from exc
            raise
        except Exception as exc:
            # ``BrowserFallbackFailedError`` は外側の ``except BrowserFallbackFailedError``
            # 段で受け取る（同じ ``try`` の ``except`` 間で再評価されないため）。
            # ここではその他の ``ComkenError`` / ``OSError`` を一括で処理する。
            if not isinstance(exc, BrowserFallbackFailedError):
                try:
                    attempt.record_failure(exc, route=route)
                except (
                    HistoryWriteError,
                    HistoryLockTimeoutError,
                    CSVError,
                ) as history_exc:
                    raise HistoryWriteError(history_path, str(history_exc), original=exc) from exc
            raise
    except BrowserFallbackFailedError as exc:
        # 0 件取り直しのブラウザ失敗は ``except EmptyReportError`` 内で ``raise`` される。
        # ``except`` 内で ``raise`` した例外は同じ ``try`` の別の ``except`` には飛ばず
        # 外側に素通りするため（Python 仕様）、外側の段で受けて ``record_failure`` /
        # 再 ``raise`` を行う。``_fetch_with_auto_fallback()`` が ``2000件超`` で
        # 投げた ``BrowserFallbackFailedError`` もここで受けて ``route`` を揃える
        if exc.route and route != exc.route:
            route = exc.route
        try:
            attempt.record_failure(exc, route=route)
        except (
            HistoryWriteError,
            HistoryLockTimeoutError,
            CSVError,
        ) as history_exc:
            raise HistoryWriteError(history_path, str(history_exc), original=exc) from exc
        raise
    attempt.record_success(path, table, route=route)
    return path


def _is_api_path(entry: ReportEntry) -> bool:
    """このレポートが「Report API」経路かどうか。

    SOQL 経由・ブラウザ経由（最初から）は自動切替対象外。``use_soql=False`` かつ
    ``exceeds_row_limit=False`` のときだけ Report API 経路。
    """
    return not entry.use_soql and not entry.exceeds_row_limit


def _fetch_with_auto_fallback(
    entry: ReportEntry,
    *,
    filters: list[dict] | None,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None,
) -> tuple[str, Table]:
    """Report API 経路での自動切替（``SalesforceReportTruncatedError``）を扱う。

    戻り値は ``(route, table)`` の2要素タプル:

    - ``route``: 最終的に使った取得経路（``ROUTE_API`` / ``ROUTE_SOQL`` /
      ``ROUTE_BROWSER`` / 自動切替2種）
    - ``table``: 取得できた Table

    Report API 経路以外（SOQL・最初からブラウザ）のときは自動切替せず、``_fetch()`` の
    戻り値をそのまま ``route=ROUTE_SOQL`` / ``route=ROUTE_BROWSER`` で返す。
    0行+``allow_empty=False`` の自動切替は ``_download()`` 側で ``_save()`` が
    ``EmptyReportError`` を投げた後に行う。
    """
    if not _is_api_path(entry):
        # SOQL / 最初からブラウザ経由は自動切替しない
        table = _fetch(entry, filters, browser_sessions=browser_sessions)
        route = ROUTE_SOQL if entry.use_soql else ROUTE_BROWSER
        return route, table

    # Report API 経路。``SalesforceReportTruncatedError`` が出たら自動でブラウザに切り替え
    try:
        table = _fetch(entry, filters, browser_sessions=browser_sessions)
        return ROUTE_API, table
    except SalesforceReportTruncatedError:
        if filters is not None:
            # 実行時フィルタ付きはブラウザに機能制限があり取り直し無意味。
            # そのまま呼び出し側へ例外を上げる（``_download()`` 側の ``except Exception``
            # で ``ScheduledDownloadFailedError`` に変換される）
            raise
        if browser_sessions is None:
            # ``browser_sessions`` が無い経路（単体テスト・直接呼び出し）はブラウザを
            # 開く契約ではないので、自動切替せず例外を上げる
            raise
        logger.warning(
            "Report API が 2000件超で打ち止められたため、ブラウザ経由に自動で切り替えます: "
            "%s（%s）。管理表の2000件超を○にするか SOQL 化を検討してください",
            entry.key,
            entry.summary,
        )
        try:
            browser_table = _fetch_via_browser(entry, browser_sessions=browser_sessions)
        except Exception as browser_exc:
            # ブラウザ取り直し自体に失敗 → 自動切替は成功しなかった扱いとし、
            # ``BrowserFallbackFailedError`` で「ブラウザ取り直し失敗」を表現する。
            # ``SalesforceReportTruncatedError.__init__(self, report_id, row_limit)``
            # のシグネチャに合わせようとすると ``TypeError`` になり、原因区分が
            # 「プログラム」化けるため、ダウンローダー側で専用例外を新設する。
            # ``DownloaderError`` 系なので ``_failure_row()`` で「Salesforce」区分に入る
            raise BrowserFallbackFailedError(
                entry.key, entry.summary, "2000件超", ROUTE_BROWSER_FALLBACK_TRUNCATED
            ) from browser_exc
        return ROUTE_BROWSER_FALLBACK_TRUNCATED, browser_table


def _require_folder(entry: ReportEntry) -> None:
    """保存先フォルダが無ければ `ReportFolderNotFoundError`。**勝手に作らない。**

    作らずに失敗させる。無いのは書き間違いのことが多く、勝手に作ると
    誰も読まない場所へ置き続けることになる。

    検査するのは「**ベースフォルダ**」のみ。出力パスは「ベース / 概要」の
    2 階層になったが、ここで作るかを分けるのはベースの有無だけで十分
    （概要のフォルダは ``_save()`` が ``mkdir(exist_ok=True)`` で自動作成する。
    ここで概要の有無まで検査すると、保存のたびに人が概要フォルダを掘る必要が
    出て、運用上の意味がない）。

    保存先フォルダは ``paths.base_folder()`` が組み立てる
    （管理表の「グループ」＋設定シートの「ベースURL」）。
    `entry.group` が設定シートに無い場合はここより先に
    `GroupNotRegisteredError` が上がる（フォルダの有無より先に、
    そもそも出力先を決められないという、より根本的なエラーとして扱う）。
    """
    base = paths.base_folder(entry)
    if not base.is_dir():
        raise ReportFolderNotFoundError(entry.key, base)


def _fetch(
    entry: ReportEntry,
    filters: list[dict] | None = None,
    *,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
) -> Table:
    """Salesforce へ問い合わせて明細表を返す。

    つなぐ組織は URL のドメインで決まる（`site_for()`）。管理表に組織を選ぶ列は
    作らない——人が選ぶ形にすると、URL と食い違ったときに別の組織へ問い合わせて
    「レポートが見つからない」という分かりにくい失敗になる。

    管理表の「SOQL」「2000件超」列で経路を切り替える（優先順位順）:

    1. ``entry.use_soql`` が真 → SOQL経由（`_fetch_via_soql()`）
    2. ``entry.exceeds_row_limit`` が真 → ブラウザ経由（`_fetch_via_browser()`）
    3. どちらも偽 → 通常の Report API（``_fetch_via_api()``）

    **Report API 経路での「自動切替」は `_download()` 側でオーケストレーションする。
    この関数は与えられた経路をそのまま実行するだけのシンプルな役割に留め、
    ``SalesforceReportTruncatedError`` が出た／0件 + ``allow_empty`` × という
    2 つの失敗条件の検知と、ブラウザ経由の取り直し + 経路記録 (``route``) は
    ``_download()`` に集約する。``_fetch`` 自体は「経路を選んで取得する」
    だけを知っていればよい（``_download()`` のテスト容易性を上げるため）。

    SOQL経由・ブラウザ経由のどちらも `filters` は使えない（実行時フィルタの
    仕組みが無い）。

    ``browser_sessions`` は ``_download_scheduled_locked()`` で作られた
    使い回し用の dict。``None`` のときは従来通り「その場で開いて閉じる」
    （``_fetch_via_browser()`` 側のキーワード既定値）。
    """
    if entry.use_soql:
        if filters is not None:
            raise ValueError(f"SOQL経由のレポートには実行時フィルタを指定できません: {entry.key}")
        return _fetch_via_soql(entry)
    if entry.exceeds_row_limit:
        if filters is not None:
            raise ValueError(
                f"ブラウザ経由のレポートには実行時フィルタを指定できません: {entry.key}"
            )
        return _fetch_via_browser(entry, browser_sessions=browser_sessions)
    return _fetch_via_api(entry, filters)


def _fetch_via_soql(entry: ReportEntry) -> Table:
    """SOQL経由で entry を取得し、`_fetch()` と同じ Table を返す。

    ``entry.key`` と同じ ``KEY`` を持つ ``SoqlReport`` を comken の
    ``soql_report_for()`` で探す。管理表の「SOQL」列が「○」なのに登録が無い
    場合は ``SoqlReportNotRegisteredError``（設定ミス）で止まる。

    つなぐ組織は ``SoqlReport.URL`` ではなく ``entry.url``（管理表側）を使う。
    参照先の単一の正は管理表で、``SoqlReport.URL`` は
    ``download_soql_reports()``（管理表を経由しない独立した経路）専用の値
    だからここでは読まない。

    取得は ``SalesforceBase.bulk_query()`` で行う。Bulk API 2.0 の
    ジョブで長時間データをサーバ側で処理できるため、件数が多い
    SOQL レポートでの往復回数を抑えられる。集計関数・``GROUP BY`` 等
    Bulk API 2.0 が受け付けない SOQL はこの経路では使えない。
    """
    report_cls = soql_report_for(entry.key)
    instance = report_cls()
    site = site_for(entry.url)
    with site() as salesforce:
        return salesforce.bulk_query(instance.soql())


def _fetch_via_api(
    entry: ReportEntry,
    filters: list[dict] | None,
) -> Table:
    """通常の Report API で取得する（自動切替なし）。``_download()`` が呼び、
    ``SalesforceReportTruncatedError`` の取り直しも ``_download()`` 側で
    オーケストレートする。

    ``browser_sessions`` は **使わない**（API 経路ではブラウザを開かない）。
    """
    site = site_for(entry.url)
    with site() as salesforce:
        if filters is None:
            return salesforce.report.get(entry.report_id)
        return salesforce.report.get(entry.report_id, filters=filters)


def _fetch_via_browser(
    entry: ReportEntry,
    *,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
) -> Table:
    """ブラウザ経由で entry を取得し、`_fetch()` と同じ Table を返す。

    **ログインの扱い:** ブラウザを新規に開くとき（使い回し中でないとき）は
    ``login_with_credentials()`` を呼び、ID/パスワードを DPAPI から自動入力した
    うえで MFA の承認をスマホで待つ（無人で承認され
    なければ ``LoginFailedError`` を送出し、そのレポートは失敗として記録される。
    認証情報が未登録なら ``CredentialNotFoundError``、復号できなければ
    ``CredentialError`` が出る。いずれも ``ComkenError`` 系なので
    ``download_scheduled()`` 側の既存処理で次のレポートへ続行される）。
    ``login_with_credentials()`` が例外を出した場合は ``try/except`` で拾い、
    壊れたブラウザを dict から外して閉じる（壊れた状態のブラウザを次のレポートへ
    引き継がないため）。ブラウザを使い回す 2 件目以降では ``login_with_credentials()``
    を再実行しない（既にログイン済みのブラウザをそのまま使う）。

    **ブラウザの使い回し:** ``browser_sessions`` が渡されたときは、同じ組織
    （``site_for()`` が返すクラス）のブラウザを 1 回の実行の中で使い回す。
    まだ開いていなければ ``site_class()`` の ``__enter__()`` を直接呼んで開き、
    ``login_with_credentials()`` を 1 回だけ実行して dict に登録する。次の同組織の
    レポートは同じインスタンスを再利用する（``login_with_credentials()`` は
    再実行しない）。取得中に例外が出たブラウザは dict から外して ``__exit__()``
    を直接呼び、次は新しいブラウザを開き直す（壊れた状態のブラウザを次へ引き継がない）。
    ``browser_sessions`` が ``None`` のときは単体テスト・直接呼び出し経路として
    「開いて閉じる」従来動作に戻る（``with site_class() as sf:``）。

    `selenium` 依存を、実際にブラウザ経由のレポートを使うときだけ読み込むよう、
    ここで初めて import する（管理表で誰も「2000件超」を「○」にしていない
    運用では `service.py` を import しても `selenium` は要らない）。
    """
    from comken.toolbox.browser.sites.salesforce import site_for as browser_site_for

    site_class = browser_site_for(entry.url)
    if browser_sessions is None:
        # 単体テスト・直接呼び出し経路。今まで通り「開いて閉じる」
        with site_class() as sf:
            # ID/パスワードは DPAPI から自動入力、MFA はスマホで承認（無人で承認されなければ
            # ``LoginFailedError``、認証情報が無ければ ``CredentialNotFoundError`` /
            # ``CredentialError``。いずれも ``with`` を抜ける際に ``__exit__()`` が呼ばれる）
            sf.login_with_credentials()
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
                dict(sf.export_reports({entry.url: tmp_path}))
                with CSV(tmp_path, read_only=True) as source:
                    return source.read()
        raise AssertionError("unreachable")  # 上の with 内で必ず return する

    # 同じ組織のブラウザが既に開いていれば再利用、無ければ開く
    sf = browser_sessions.get(site_class)
    if sf is None:
        # ``__enter__()`` を直接呼ぶのは、``sf`` を ``browser_sessions`` に乗せて
        # ``_download_scheduled_locked()`` 側の ``finally`` まで保持するため。
        # クロージャに入れると外から参照できなくなる。``__exit__()`` は例外時
        # または ``_download_scheduled_locked()`` 最後の ``finally`` で呼ぶ
        sf = site_class()
        sf.__enter__()
        # ``__enter__()`` が成功した時点で dict に登録する。``login_with_credentials()``
        # が例外を出しても「失敗したら閉じて入れ物から外す」範囲（下の ``try/except``）
        # に入るので、後続のレポートで「壊れたブラウザ」を再利用しない。
        # ``__enter__()`` 自体が失敗した場合は何も開いていないので、dict にも入れず、
        # 閉じもしない（``__exit__`` も未呼び出し）
        browser_sessions[site_class] = sf
        try:
            # ID/パスワードを DPAPI から自動入力したうえで MFA の承認をスマホで待つ。
            # 認証情報の例外（``CredentialNotFoundError`` / ``CredentialError``）も
            # 下の ``except Exception`` で拾われ、閉じて dict から外したうえで元の例外を
            # そのまま上げる（``download_scheduled()`` 側の既存処理で次のレポートへ続行）
            sf.login_with_credentials()
        except Exception:
            # ``login_with_credentials()`` 失敗時も「壊れたブラウザを dict に残したまま
            # 次へ渡さない」ため、閉じて dict から外す。閉じるときに起きた例外は元の
            # 失敗を隠すので、警告ログに留めて元例外を上げる
            browser_sessions.pop(site_class, None)
            try:
                sf.__exit__(None, None, None)
            except Exception as close_exc:
                logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)
            raise
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
            dict(sf.export_reports({entry.url: tmp_path}))
            with CSV(tmp_path, read_only=True) as source:
                return source.read()
    except Exception:
        # 壊れた状態のブラウザを次回以降に渡さないため、ここで閉じて dict から外す。
        # 閉じるときに起きた例外は元の失敗を隠すので、警告ログに留めて元例外を上げる
        browser_sessions.pop(site_class, None)
        try:
            sf.__exit__(None, None, None)
        except Exception as close_exc:
            logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)
        raise


def _validate_filters_by_report(
    filters_by_report: dict[str, list[dict]], entries: dict[str, ReportEntry]
) -> None:
    """実行時フィルタの管理番号がすべて管理表に存在することを確認する。"""
    for report_key in filters_by_report:
        if report_key not in entries:
            raise ReportNotRegisteredError(report_key, list(entries), paths.MASTER_PATH)


def _save(
    entry: ReportEntry,
    table: Table,
    *,
    schedule_run_time: dt.time | None = None,
    current: dt.datetime | None = None,
) -> Path:
    """行数と `allow_empty` に応じて保存先へ書き込み、書き終わったパスを返す。

    0 行・`allow_empty` × → `EmptyReportError`（=失敗）／○ → 空 CSV を置く。
    ``report.get()`` が返す ``Table.columns`` を使うため、0 行でも Salesforce の
    メタデータから得た見出しを保存する。

    保存先は ``paths.output_path()`` が返す単一のパス
    （``ベース/概要/{管理番号}_{スケジュール時刻}.csv``）。
    衝突回避は ``_reserve_unique_path()`` で常に（連番 ``_1`` / ``_2`` … を付けて）。
    同じパスへの上書きは想定しない — 取得済みキャッシュを読む側（`pathlib.Path.glob()`
    で `paths.output_path()` が組み立てたパスを探す運用）は、同じフォルダ内の連番を全部
    候補に含めて新しい順で拾えるので、何度実行しても履歴と当日の最新状態は崩れない。

    **概要のフォルダは保存名の予約前にここで ``mkdir`` する。** ベースフォルダは
    ``_download()`` 側の ``_require_folder()`` で先に検査済みなので、
    ``parents=False`` で「無ければ失敗する」をそのまま流用できる（ベースが既に
    無い場合はここで ``FileNotFoundError`` が出る。先に ``_require_folder()`` で
    ``ReportFolderNotFoundError`` に変換する流れが壊れないよう、 ``_save()`` 単体で
    動くテスト経路のために ``OSError`` のまま伝播させる）。**dry-run 中は
    ``mkdir`` もしない**（``comken.runtime.dry_run`` のブロック内では保存もしない設計に
    沿って、フォルダだけ作ると「書き込まないのにフォルダだけが残る」事故を防ぐ）。

    ``current`` は ``_download_scheduled_locked()`` で固定した ``clock_now()`` の値。
    ``paths.output_path(now=current)`` に渡すことで、23:59 に始まった実行が日付をまたいで
    終わっても、出力ファイル名が開始日で揃う。``None`` のときは ``paths.output_path()``
    側で ``clock_now()`` にフォールバックする（既存の単体テスト経路を残すため）。

    **失敗時の後始末について:** 書き込みが例外の原因になった場合は、既存の
    ``_download()`` の ``except Exception`` 経路で ``_Attempt.record_failure()``
    を経由して ``ScheduledDownloadFailedError`` に変換される。書きかけのファイルも
    ``unlink(missing_ok=True)`` で後始末する。
    """
    base = paths.output_path(entry, schedule_run_time, now=current)
    # **概要のフォルダは保存名の予約前に ``mkdir`` する。** ベースフォルダは
    # ``_require_folder()`` で先に検査済みなので ``parents=False`` で安全。
    # dry-run 中は作らない（保存もしない運用に合わせた防御）
    if not is_dry_run():
        base.parent.mkdir(exist_ok=True)
    path = _reserve_unique_path(base, entry.key)
    try:
        # 問い合わせは成功したが明細が無い。**0件あり × なら失敗扱い、○ なら正常終了**
        if not table and not entry.allow_empty:
            raise EmptyReportError(entry.key, entry.summary, entry.url)
        _write_csv(path, table)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _reserve_unique_path(base_path: Path, report_key: str) -> Path:
    """排他的な新規作成でパスを予約し、既存ファイルを上書きしない。

    同じフォルダに既存ファイルがあると連番（ ``_1`` / ``_2`` …）を足して別の
    ファイル名を探す。 ``RESERVE_PATH_LIMIT`` を超えると ``ReportReservePathLimitError``
    を送出する（権限・同期の異常で ``FileExistsError`` が返り続ける無限ループを
    避けるため）。 ``WORKDAY_SEARCH_LIMIT`` と同じ考え方で上限を切っている。

    ``report_key`` はエラーメッセージに含めるためだけで、ファイル名の組み立てに
    は使わない（呼び出し側が ``base_path`` を組み立てる時点で決定済みのため）。
    """
    candidate = base_path
    sequence = 0
    for _ in range(RESERVE_PATH_LIMIT):
        try:
            candidate.open("x").close()
            return candidate
        except FileExistsError:
            sequence += 1
            candidate = base_path.with_stem(f"{base_path.stem}_{sequence}")
    raise ReportReservePathLimitError(report_key, base_path, RESERVE_PATH_LIMIT)


def _write_csv(path: Path, table: Table) -> None:
    """一時ファイルへ書いてから置き換える。

    複数のプロジェクトが同時に呼ぶので、直接書くと**読んでいる最中のファイルが
    半端な状態**になりうる。同じフォルダ内の置き換えは一度に入れ替わる。
    """
    with atomic_write(path) as tmp, CSV(tmp) as csv_file:
        csv_file.replace(table)


def _warn_shared_reports(entries: dict[str, ReportEntry]) -> None:
    """同じ Salesforce レポートを複数の管理番号が指していればログに出す。

    エラーにはしない（意図している場合もある）。**気づけるようにするだけ**。
    """
    for report_id, keys in shared_report_ids(entries).items():
        logger.info(
            "同じ Salesforce レポートを %d 件の管理番号が指しています: %s（%s）",
            len(keys),
            "、".join(str(key) for key in keys),
            report_id,
        )


def _matched_schedule_key(
    entry: ReportEntry,
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
) -> tuple[bool, str]:
    """このレポートを今取得すべきか、すべきなら根拠のスケジュールキーを返す。

    戻り値は ``(取得すべきか, スケジュールキー)``。スケジュールキーは、
    取得すべきだった場合に「どのスケジュール行が根拠になったか」を表し、
    履歴の ``スケジュールキー`` 列にそのまま記録する。スケジュール行が無い
    レポート（後方互換）の取得時は空文字を返す。

    判定ロジック:

    - スケジュール行が無いレポートは ``downloaded_today()`` ベースで「今日まだ
      取れていなければ」取得（後方互換）
    - スケジュール行がある場合、いずれかの行が ``is_due()`` True で、かつ
      ``schedule_succeeded_today()`` が False（=今日まだ成功していない）なら取得。
      複数の行が True を返す場合は取得時刻が一番遅い行のキーを採用（それより早い
      時刻の行は無視する）。``start_time is None`` の行は最も早い扱いとし、具体的な
      時刻を持つ行がある限りそちらを優先する
    - いずれの行も ``is_due()`` False なら False, ""
    - いずれかの行が ``is_due()`` True でも、今日すでに成功済みなら False, ""

    祝日判定は ``ScheduleRule.is_due()`` が ``comken.core.dates`` の統一
    カレンダーを直接見るため、呼び出し側でカレンダーを用意する必要はない。

    ``current`` は呼び出し元で固定した基準日時。dedup 判定にも ``current.date()``
    を渡し、スケジュール判定と履歴チェックが同じ「日」を見るようにする
    （23:59 をまたぐ実行で「判定は開始日・履歴は翌日」となる事故を防ぐ）。
    """
    rules = rules_by_report.get(entry.key)
    if not rules:
        # スケジュール行が無いレポート: ``downloaded_today()`` で 1 日 1 回までに
        # 制限する（後方互換）。戻り値のキーは空文字
        history_path = _history_path()
        return not history.downloaded_today(history_path, entry.key, date=current.date()), ""
    due_rules = [
        rule
        for rule in rules
        if rule.is_due(current)
        and not history.schedule_succeeded_today(
            _history_path(), rule.schedule_key, date=current.date()
        )
    ]
    if not due_rules:
        return False, ""
    # ``start_time is None`` の行は具体的な時刻より優先度が低い（時刻条件なしの行で
    # 取得すると、後の時刻の行を再評価する余地がなくなるため）。
    latest = max(due_rules, key=lambda rule: rule.start_time or dt.time.min)
    return True, latest.schedule_key


def _select_targets(
    entries: dict[str, ReportEntry],
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
) -> tuple[list[tuple[ReportEntry, str, ScheduleRule | None]], list[str]]:
    """定期取得の対象を「有効」かつ「取得すべき」レポートに絞る。

    ``download_scheduled()`` から対象選定ロジックだけを抜き出したヘルパー。
    関数本体が複雑にならないように分離している（``download_scheduled`` 自体は
    既に10近くの分岐があり、複雑度の上限に近い）。

    **以前**は「当日中に ``SalesforceReportTruncatedError`` で失敗したレポートは
    再問い合わせしない」スキップを入れていた。2026-10 に **自動切替**
    （Report API が 2000件超で打ち止められたら自動でブラウザ経由に
    取り直す）を入れた結果、このスキップは不要になった（自動切替で取れる）。
    同じ日の 2 回目の実行でも Report API → ブラウザ自動切替が動くので、毎回
    Salesforce への問い合わせを行う。
    戻り値の ``already_failed`` は常に空リスト。

    戻り値の ``targets`` は ``(ReportEntry, schedule_key, ScheduleRule | None)``
    の3要素タプル。``ScheduleRule`` を一緒に運ぶのは、唯一の出力ファイル名に
    スケジュール時刻を埋め込むために ``start_time`` が必要だから。``ScheduleRule`` が
    ``None`` のときはスケジュール行が無いレポート（後方互換）で、その場合は
    ``paths.output_path()`` 側で現在時刻にフォールバックする。

    Returns:
        ``(targets, already_failed)``。``targets`` は実際に取得を試みる
        ``(ReportEntry, schedule_key, ScheduleRule | None)`` のリスト。
        ``already_failed`` は空（``_download_scheduled_locked()`` の呼び出し
        シグネチャを保つために残してある）。
    """
    targets: list[tuple[ReportEntry, str, ScheduleRule | None]] = []
    for entry in entries.values():
        if not entry.enabled:
            continue
        is_due, schedule_key = _matched_schedule_key(entry, rules_by_report, current)
        if not is_due:
            continue
        # ``schedule_key`` は取得後に履歴へ記録し、``schedule_succeeded_today()``
        # が再判定に使う。スケジュール行が無いレポート（後方互換）は空文字 +
        # ``ScheduleRule = None``
        matched_rule: ScheduleRule | None = None
        if schedule_key:
            for rule in rules_by_report.get(entry.key, ()):
                if rule.schedule_key == schedule_key:
                    matched_rule = rule
                    break
        targets.append((entry, schedule_key, matched_rule))
    return targets, []


def _failure_row(
    exc: BaseException,
    seconds: float,
    schedule_key: str = "",
    *,
    route: str = "",
) -> HistoryRow:
    """失敗時の履歴1行を、**例外の型だけから**組み立てる。

    `fetched` / `saved` は `_download()` の何処で失敗したかを例外で判別する。
    判定順は上から5行（狭い条件から順に評価）:

    1. `ReportFolderNotFoundError` → 取得段階の前（保存先フォルダ検査で停止）
    2. `EmptyReportError` → 取得は成功、保存は未到達（0件 × 一意）
    3. `OSError` → 保存段階（書き込み・権限・共有サーバー断）
    4. その他の `ComkenError` → 取得段階（Salesforce 通信と認証）
    5. それ以外 → プログラム（comken 側の想定外）

    4. は `download_scheduled()` が捕捉する範囲と一致。1か所の判断で
    「握りつぶす範囲」と「履歴側でバグと書く範囲」を揃える。

    `schedule_key` は成功時と同じく履歴の「スケジュールキー」列へ書くためのもの。
    dedup 判定は成功行しか見ないが、履歴を後から追ったときに「どのスケジュール行が
    いつ失敗したか」が追えるよう、`record_success()` と対称にここで渡す
    （後方互換のため既定値は空文字）。

    `route` は取得経路（``ROUTE_API`` / ``ROUTE_SOQL`` / ``ROUTE_BROWSER`` /
    自動切替2種のいずれか）。失敗時の履歴にも書く。**例外の型から判別できない**
    （=``SalesforceReportTruncatedError`` が出ても「取り直して成功」ならここに来ない）
    ので、``_download()`` が経路を確定したあとに渡せるようキーワード引数で受ける。
    """
    if isinstance(exc, ReportFolderNotFoundError):
        fetched, saved, cause = None, None, CAUSE_CONFIG
    elif isinstance(exc, EmptyReportError):
        fetched, saved, cause = True, None, CAUSE_EMPTY_DATA
    elif isinstance(exc, OSError):
        fetched, saved, cause = True, False, CAUSE_FILE
    elif isinstance(exc, ComkenError):
        fetched, saved, cause = False, None, CAUSE_SALESFORCE
    else:
        fetched, saved, cause = False, None, CAUSE_PROGRAM
    return HistoryRow(
        succeeded=False,
        fetched_from_salesforce=fetched,
        saved_to_file=saved,
        seconds=seconds,
        cause=cause,
        error_code=type(exc).__name__,
        error=str(exc),
        schedule_key=schedule_key,
        route=route,
    )

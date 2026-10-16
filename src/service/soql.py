r"""src/service/soql.py — SOQL 経由の取得と件数検証。

``download_scheduled()`` の 1 件分で、管理表の「SOQL」列が「○」のときに呼ばれる。
SOQL の本文は ``soql_report_for(entry.key)`` で引いた ``SoqlReport`` が出す —
``_fetch_via_soql()`` はそれを ``SalesforceBase.bulk_query()`` に渡す。

    from src.service.soql import (
        _fetch_via_soql,
        _verify_soql_count,
        _fetch_report_total_count,
        _extract_row_count_from_report_payload,
        INCOMPLETE_SOQL_MARKER,
    )

``INCOMPLETE_SOQL_MARKER`` の文字列は soql-collector 側の ``INCOMPLETE_MARKER``
と同じ。 変えるときは両方同時に。
"""

from __future__ import annotations

import logging
from typing import Any

from comken.core.table.model import Table
from comken.exceptions import (
    SalesforceError,
    SalesforceReportIDNotFoundError,
    SalesforceRequestError,
)
from comken.toolbox.salesforce.sites import site_for

from src.exceptions import (
    SoqlCountMismatchError,
    SoqlIncompleteError,
    SoqlTransformError,
)
from src.sheets.master import ReportEntry
from src.soql_reports import soql_report_for

logger = logging.getLogger("src.service")

# soql-collector の下書きが SOQL を完成できなかったときに 1 行目へ付ける印。
# 1 行目（前後の空白を除く）がこの文字列で始まる SOQL はそのままでは Salesforce に
# 問い合わせず、 ``SoqlIncompleteError`` で止める。 soql-collector 側の
# ``INCOMPLETE_MARKER`` と同じ。 変えるときは両方同時に。
INCOMPLETE_SOQL_MARKER = "【未完成】"


def _fetch_via_soql(entry: ReportEntry) -> Table:
    """SOQL経由で entry を取得し、`_fetch()` と同じ Table を返す。

    ``entry.key`` と同じ ``KEY`` を持つ ``SoqlReport`` を comken の
    ``soql_report_for()`` で探す。管理表の「SOQL」列が「○」なのに登録が無い
    場合は ``SoqlReportNotRegisteredError``（設定ミス）で止まる。

    つなぐ組織は管理表の ``entry.url`` を使う。 ``SoqlReport`` には URL などの
    接続情報を持たせず、管理表の ``ReportEntry`` を単一の正にしている
    （管理表を経由しない別ルートは廃止）。

    取得は ``SalesforceBase.bulk_query()`` で行う。サーバ側で
    データを処理する非同期ジョブのため、件数が多い SOQL レポートでの
    往復回数を抑えられる。集計関数・``GROUP BY`` 等 ``bulk_query()``
    が受け付けない SOQL はこの経路では使えない。

    **未完成の SOQL の検知（2026-10 改定）:** ``soql()`` が返した本文の
    先頭（前後の空白を除く）が ``INCOMPLETE_SOQL_MARKER`` で始まる SOQL は
    そのままでは Salesforce に問い合わせられない。 ``bulk_query()`` を呼ぶ
    **前**に ``SoqlIncompleteError`` で止める（Salesforce には問い合わせない）。
    印の文字列は ``soql.INCOMPLETE_SOQL_MARKER`` の定数で soql-collector 側の
    ``INCOMPLETE_MARKER`` と同じ。

    **件数の比較（2026-10 改定）:** ``SoqlReport.CHECK_COUNT`` が ``True`` のとき
    （既定）、 ``bulk_query()`` の **直後** に **同じ接続のうち** にもう 1 度
    Report API を叩いて ``factMap["T!T"]["aggregates"]`` の ``RowCount`` を取り、
    ``len(table)`` と比べる。一致すれば INFO ログを 1 行出して返す。**一致
    しなければ** ``SoqlCountMismatchError`` で **保存させずに**例外を上げる
    （一時ファイルも作らない）。レポート ID が URL から取れない / ``RowCount``
    が無い / 401・403 以外の HTTP エラー / ``SalesforceError`` は WARNING を出して
    保存を続ける（401・403 は既存の失敗扱いに揃える）。 ``CHECK_COUNT=False`` のときは
    Report API を 1 度も叩かずに ``bulk_query()`` の結果をそのまま返す
    （=レポートの GET が必要ないレポート向け）。**同じ接続のうち** に比較するため
    ``site_for()`` も 1 回しか呼ばれない（ログインは 1 回だけ）。

    **加工 transform()（2026-10 改定）:** 件数の比較の **あと**に
    ``SoqlReport.transform()`` を加工前の生の結果に対して呼ぶ。 件数の比較は
    加工前の件数で行われる（レポートの総件数と比べられるのは加工前の件数
    のため）。 加工後が 0 行のときは既存の ``ALLOW_EMPTY`` 判定がそのまま動く。
    ``transform()`` の中で起きた例外（ ``ComkenError`` 系以外も含む）は
    ``SoqlTransformError`` （ ``DownloaderError`` 系）に包み直す。 戻り値が
    ``Table`` でない（ ``None`` を返した等）場合も ``SoqlTransformError``
    で止める。 どちらも ``from exc`` で元の例外を繋ぐ。 メッセージには
    **行の値や SOQL 本文を含めない** （ ``str(exc)`` をそのまま載せない）。
    """
    report_cls = soql_report_for(entry.key)
    instance = report_cls()
    # SOQL 経由のとき、実際に使った ``SoqlReport`` のクラス名と所在モジュールを
    # INFO で 1 行出す（管理表の「SOQL」列 = ○ のレポートが、コードのどの
    # ``reports/<ファイル>.py`` で定義されているかを運用時に追えるようにするため）。
    # SOQL 本文は DEBUG（=既定では出ない。問い合わせの中身は障害調査で見たい想定）
    logger.info(
        "SOQL レポートを使用します: クラス=%s, モジュール=%s, 管理番号=%s",
        report_cls.__name__,
        report_cls.__module__,
        entry.key,
    )
    soql_text = instance.soql()
    logger.debug("SOQL: %s", soql_text)
    # 未完成の印（先頭・空白無視）。本文の途中（文字列値）に同じ文字列が
    # 出ても止めない。Salesforce には問い合わせない
    if soql_text.lstrip().startswith(INCOMPLETE_SOQL_MARKER):
        raise SoqlIncompleteError(entry.key, entry.summary)
    site = site_for(entry.url)
    with site() as salesforce:
        table = salesforce.bulk_query(soql_text)
        # 件数の比較。 ``CHECK_COUNT=False`` のときは Report API を 1 度も叩かない。
        # 同じ接続のうちに行うので、 1 レポートあたり ``site_for()`` が 2 回
        # （=ログイン 2 回）にならない。 比較は **加工前** の生の結果で行う。
        if report_cls.CHECK_COUNT:
            _verify_soql_count(entry, table, salesforce)
        # 加工（ transform() ）。 例外／非 Table 戻り値は ``SoqlTransformError``
        # に包み直して、 当該レポートだけの失敗として記録させる（次レポートへ続く）。
        # 元の例外クラス名だけをメッセージに含め、 ``str(exc)`` の全文は載せない
        # （行の値／ SOQL 本文が漏れないため）。
        try:
            transformed = instance.transform(table)
        except Exception as exc:
            raise SoqlTransformError(
                entry.key,
                entry.summary,
                report_cls.__name__,
                type(exc).__name__,
            ) from exc
        if not isinstance(transformed, Table):
            raise SoqlTransformError(
                entry.key,
                entry.summary,
                report_cls.__name__,
                type(transformed).__name__,
                reason="transform() の戻り値が Table ではありません",
            )
        return transformed


def _verify_soql_count(entry: ReportEntry, table: Table, salesforce: Any) -> None:
    """SOQL の件数と、レポート API の総件数が合うか検証する。

    接続は呼び出し元（ ``_fetch_via_soql()`` ）から渡された ``salesforce`` を
    **そのまま使う** ── ここで ``site_for()`` をもう一度呼ぶとログインが
    2 回走るため、 接続は外側からもらう（同じ ``with site() as salesforce:`` の
    うちに呼び出される前提）。

    一致 → INFO ログ（件数だけ）。不一致 → ``SoqlCountMismatchError``
    （保存はしない。一時ファイルも作らない）。比べられない（URL から
    レポート ID が取れない / ``RowCount`` が無い / 401・403 以外の HTTP
    エラー / ``SalesforceError`` 全体）→ WARNING を出して保存に進む。
    401・403 は ``SalesforceRequestError`` のまま上位へ送出する
    （既存の「取得の失敗」扱い）。
    """
    try:
        report_id = entry.report_id
    except SalesforceReportIDNotFoundError:
        # URL がレポートの URL でない（ID が取れない）ときは比べる対象が無い。
        # 黙って通すと「件数が違う」事故を見落とすので、理由を添えて WARNING
        logger.warning(
            "SOQL 件数の比較をスキップしました（レポート ID を URL から取り出せません"
            "）: 管理番号=%s, 概要=%s",
            entry.key,
            entry.summary,
        )
        return

    report_count = _fetch_report_total_count(salesforce, report_id)

    if report_count is None:
        # 取れる形ではなかった（``RowCount`` が無い / 形式違い / 401・403 以外の
        # HTTP エラー / ``SalesforceError`` 全体）。 ``_fetch_report_total_count()``
        # 側で WARNING を出している
        return

    soql_count = len(table)
    if soql_count == report_count:
        logger.info(
            "SOQL 件数がレポート総件数と一致しました: 管理番号=%s, 件数=%d",
            entry.key,
            soql_count,
        )
        return

    logger.warning(
        "SOQL 件数がレポート総件数と一致しません: 管理番号=%s, SOQL=%d, レポート=%d",
        entry.key,
        soql_count,
        report_count,
    )
    raise SoqlCountMismatchError(entry.key, entry.summary, soql_count, report_count)


def _fetch_report_total_count(salesforce: Any, report_id: str) -> int | None:
    """レポート API から総件数（``RowCount``）を取り出す。

    ``?includeDetails=false`` を付けるので明細データは返らない（2000 件超でも
    総件数が取れる）。 ``RowCount`` が ``reportMetadata.aggregates`` に無い /
    ``factMap["T!T"]["aggregates"]`` の対応位置が無い / 値が整数でない /
    形式が違う（``dict`` の ``value`` を採る場合を含む）など、**取れる形に
    なっていない**ときは ``None`` 。 **401 / 403 は ``SalesforceRequestError``
    のまま上位へ送出**（既存の取得失敗扱いに揃え、 WARNING で握りつぶさない）。
    それ以外の ``SalesforceError`` （ ``SalesforceRequestError`` 以外の通信
    断・タイムアウト等）は WARNING を 1 行出して ``None`` を返し、保存は続ける
    （HTTP 以外の致命的なら、それは Salesforce 全体に問題があるときで、
    レポートの本体の取得は別経路で実行済み ── 件数比較だけを諦めて続行する
    方が、 取得済みのレコードを捨てずに済む）。

    実装は soql-collector の ``445b817`` （ ``src/fetch.py`` ）から移植した
    ヘルパーとほぼ同じ形。**値の形**は ``int`` の他に ``{"label": ..., "value": n}``
    形式の ``dict`` を受ける（bool は除外）。取れる形が無い場合は WARNING を
    1 行出し、 ``None`` を返す。
    """
    reports_path = salesforce.data_path("/analytics/reports")
    path = f"{reports_path}/{report_id}?includeDetails=false"
    try:
        result, _ = salesforce.request("GET", path, component="report")
    except SalesforceRequestError as exc:
        # 401 / 403 は既存の取得失敗と同じ扱いに揃える（``SalesforceRequestError``
        # をそのまま上位へ）。その他は WARNING で握りつぶして保存は続ける
        if exc.status_code in (401, 403):
            raise
        logger.warning(
            "SOQL 件数の比較に使うレポート総件数の取得に失敗しました（HTTP %d）: Report ID=%s",
            exc.status_code,
            report_id,
        )
        return None
    except SalesforceError:
        # HTTP 以外の ``SalesforceError`` （ ``SalesforceRequestError`` 以外の
        # 通信断・タイムアウト等）。 401/403 は ``SalesforceRequestError`` 側で
        # 既に再送出しているので、 ここはそれ以外の Salesforce 系のみ捕捉する。
        # 件数比較のためだけなので、 WARNING を 1 行出して保存は続ける
        logger.warning(
            "SOQL 件数の比較に使うレポート総件数の取得に失敗しました"
            "（HTTP以外のSalesforceError）: Report ID=%s",
            report_id,
        )
        return None
    count = _extract_row_count_from_report_payload(result)
    if count is None:
        logger.warning(
            "SOQL 件数の比較に使うレポート総件数が取れませんでした"
            "（RowCount が見つかりません）: Report ID=%s",
            report_id,
        )
    return count


def _extract_row_count_from_report_payload(payload: object) -> int | None:
    """``client.request`` の戻り値からレポート総件数を抽出する。

    ``reportMetadata.aggregates`` の中で ``"RowCount"`` の位置にある
    ``factMap["T!T"]["aggregates"]`` の値を返す。位置が取れない /
    値が想定形でない（ ``int`` / ``{"value": int}`` の ``dict`` 以外 /
    ``bool`` ）のときは ``None`` 。
    """
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("reportMetadata")
    if not isinstance(metadata, dict):
        return None
    aggregates = metadata.get("aggregates")
    if not isinstance(aggregates, list):
        return None

    row_count_idx: int | None = None
    for idx, agg in enumerate(aggregates):
        if isinstance(agg, str) and agg == "RowCount":
            row_count_idx = idx
            break
    if row_count_idx is None:
        return None

    fact_map = payload.get("factMap")
    if not isinstance(fact_map, dict):
        return None
    t_t = fact_map.get("T!T")
    if not isinstance(t_t, dict):
        return None
    fact_aggregates = t_t.get("aggregates")
    if not isinstance(fact_aggregates, list):
        return None
    if row_count_idx >= len(fact_aggregates):
        return None
    value = fact_aggregates[row_count_idx]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, bool):
            return None
        if isinstance(inner, int):
            return inner
    return None

r"""src/service/soql.py — SOQL 経由の取得と件数検証・列名の置き換え。

``download_scheduled()`` の 1 件分で、管理表の「SOQL」列が「○」のときに呼ばれる。
SOQL の本文は ``soql_report_for(entry.key)`` で引いた ``SoqlReport`` が出す。
``fetch_via_soql()`` はそれを ``SalesforceBase.bulk_query()`` に渡す
（``SoqlReport.USE_BULK`` が ``False`` のときは ``SalesforceBase.query()``）。

    from src.service.soql import (
        fetch_via_soql,
        _verify_soql_count,
        _fetch_report_total_count,
        _extract_row_count_from_report_payload,
        INCOMPLETE_SOQL_MARKER,
    )
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
from src.reports.base import SoqlReport
from src.sheets.master import ReportEntry
from src.soql_reports import soql_report_for
from src.soql_reports.column_names import extract_main_object, load_column_names_for
from src.soql_reports.runner import apply_column_names

logger = logging.getLogger("src.service")

# soql-collector の下書きが SOQL を完成できなかったときに 1 行目へ付ける印。
# 1 行目（前後の空白を除く）がこの文字列で始まる SOQL はそのままでは Salesforce に
# 問い合わせず、 ``SoqlIncompleteError`` で止める。 soql-collector 側の
# ``INCOMPLETE_MARKER`` と同じ。 変えるときは両方同時に。
INCOMPLETE_SOQL_MARKER = "【未完成】"


def fetch_via_soql(entry: ReportEntry) -> Table:
    """SOQL経由で entry を取得し、``fetch_via_api()`` と同じ Table を返す。

    ``entry.key`` と同じ ``KEY`` を持つ ``SoqlReport`` を ``soql_report_for()`` で
    探す。管理表の「SOQL」列が「○」なのに登録が無い場合は
    ``SoqlReportNotRegisteredError``（設定ミス）で止まる。
    つなぐ組織は管理表の ``entry.url`` を使う。

    取り方は ``SoqlReport.USE_BULK`` で選ぶ（既定は Bulk API 2.0 の
    ``SalesforceBase.bulk_query()``、``False`` なら REST の
    ``SalesforceBase.query()``）。集計関数・``GROUP BY`` を使う SOQL は
    ``USE_BULK=False`` にする。

    取得後の流れは「取得 → 件数の比較 → 列名の置き換え → ``transform()``」の順。

    - **件数の比較**（``SoqlReport.CHECK_COUNT`` が真のとき）: 同じ接続のうちにもう
      1 度 Report API を叩いて総件数を取り、SOQL の件数と比べる。**一致しなければ
      ``SoqlCountMismatchError`` で保存せずに止める**（一時ファイルも作らない）。
      レポート ID が取れない / 総件数が取れない / 401・403 以外の HTTP エラーは
      WARNING を出して保存を続ける（401・403 は取得の失敗として扱う）。
      ``CHECK_COUNT=False`` のときは Report API を 1 度も叩かない。
    - **列名の置き換え**: ``SoqlReport.COLUMN_NAMES``（手書き）と
      ``列名の対応.csv``（soql-collector の出力）で SOQL の API 名の列を表示名に
      置き換える。見出しが衝突すると ``SoqlColumnNameConflictError`` で止まる
      （そのレポートだけの失敗になり、CSV は書かれない）。
    - **加工 transform()**: ``SoqlReport.transform()`` を呼ぶ。受け取る ``Table``
      は**列名を置き換えたあとのもの**（見出しは表示名）なので、``transform()`` は
      表示名の列で書ける。中の例外（``ComkenError`` 系以外も含む）や ``Table`` でない
      戻り値は ``SoqlTransformError``（``DownloaderError`` 系）に包み直す。
      メッセージには **行の値や SOQL 本文を含めない**。

    **未完成の SOQL の検知:** ``soql()`` が返した本文の先頭（前後の空白を除く）が
    ``INCOMPLETE_SOQL_MARKER`` で始まる SOQL は、そのままでは Salesforce に
    問い合わせられない。取得を呼ぶ **前** に ``SoqlIncompleteError`` で止める
    （Salesforce には問い合わせない）。
    """
    report_cls = soql_report_for(entry.key)
    instance = report_cls()
    # SOQL 経由のとき、実際に使った ``SoqlReport`` のクラス名と所在モジュールを
    # INFO で 1 行出す（管理表の「SOQL」列 = ○ のレポートが、コードのどの
    # ``src/reports/<ファイル>.py`` で定義されているかを運用時に追えるようにするため）。
    # 取得方法（Bulk / REST）も一緒に出す（``USE_BULK`` で選べるため）。
    # SOQL 本文は DEBUG（=既定では出ない。問い合わせの中身は障害調査で見たい想定）
    logger.info(
        "SOQL レポートを使用します: クラス=%s, モジュール=%s, 管理番号=%s, 取得方法=%s",
        report_cls.__name__,
        report_cls.__module__,
        entry.key,
        "Bulk" if report_cls.USE_BULK else "REST",
    )
    soql_text = instance.soql()
    logger.debug("SOQL: %s", soql_text)
    # 未完成の印（先頭・空白無視）。本文の途中（文字列値）に同じ文字列が
    # 出ても止めない。Salesforce には問い合わせない
    if soql_text.lstrip().startswith(INCOMPLETE_SOQL_MARKER):
        raise SoqlIncompleteError(entry.key, entry.summary)
    site = site_for(entry.url)
    with site() as salesforce:
        # 取り方は ``SoqlReport.USE_BULK`` で選ぶ（既定は Bulk API 2.0）。
        # False のときは REST の ``query()``（ページ送り）になる。
        if report_cls.USE_BULK:
            table = salesforce.bulk_query(soql_text)
        else:
            table = salesforce.query(soql_text)
        # 件数の比較。 ``CHECK_COUNT=False`` のときは Report API を 1 度も叩かない。
        # 同じ接続のうちに行うので、 1 レポートあたり ``site_for()`` が 2 回
        # （=ログイン 2 回）にならない。 比較は **加工前** の生の結果で行う。
        if report_cls.CHECK_COUNT:
            _verify_soql_count(entry, table, salesforce)
        # 列名の置き換え（``COLUMN_NAMES`` / ``列名の対応.csv``）。 ``transform()``
        # が表示名の列で書けるよう、 加工の **前** に行う。
        table = _apply_soql_columns(entry, report_cls, table, soql_text)
        # 加工（transform()）。 例外／非 Table 戻り値は ``SoqlTransformError``
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

    接続は呼び出し元（``fetch_via_soql()``）から渡された ``salesforce`` を
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
    それ以外の ``SalesforceError`` （``SalesforceRequestError`` 以外の通信
    断・タイムアウト等）は WARNING を 1 行出して ``None`` を返し、保存は続ける
    （HTTP 以外の致命的なら、それは Salesforce 全体に問題があるときで、
    レポートの本体の取得は別経路で実行済み ── 件数比較だけを諦めて続行する
    方が、 取得済みのレコードを捨てずに済む）。
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
        # HTTP 以外の ``SalesforceError`` （``SalesforceRequestError`` 以外の
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
    値が想定形でない（``int`` / ``{"value": int}`` の ``dict`` 以外 /
    ``bool``）のときは ``None`` 。

    入れ子の取り出しを 3 段（``_resolve_report_metadata()`` →
    ``_find_row_count_index()`` → ``_resolve_fact_aggregates()`` →
    ``_coerce_row_count_value()``）に分け、形をチェックする段で ``None`` を弾く
    ことで、メインの本体はガード文の連なりだけになる。
    """
    aggregates = _resolve_report_metadata(payload)
    if aggregates is None:
        return None

    row_count_idx = _find_row_count_index(aggregates)
    if row_count_idx is None:
        return None

    fact_aggregates = _resolve_fact_aggregates(payload, row_count_idx)
    if fact_aggregates is None:
        return None

    return _coerce_row_count_value(fact_aggregates[row_count_idx])


def _resolve_report_metadata(payload: object) -> list | None:
    """``payload`` から ``reportMetadata.aggregates`` を取り出す。

    ``payload`` が dict 以外 / ``reportMetadata`` が dict 以外 /
    ``aggregates`` が list 以外のときは ``None`` 。 想定外の入れ子を
    一箇所に集約することで ``_extract_row_count_from_report_payload()`` 側の
    ガード文の連なりを減らす。
    """
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("reportMetadata")
    if not isinstance(metadata, dict):
        return None
    aggregates = metadata.get("aggregates")
    if not isinstance(aggregates, list):
        return None
    return aggregates


def _find_row_count_index(aggregates: list) -> int | None:
    """``aggregates`` の中の ``"RowCount"`` の位置を返す。無ければ ``None`` 。

    先頭から ``"RowCount"`` を探すので、ループ本体は ``if`` 1 つだけになる。
    """
    for idx, agg in enumerate(aggregates):
        if isinstance(agg, str) and agg == "RowCount":
            return idx
    return None


def _resolve_fact_aggregates(payload: object, row_count_idx: int) -> list | None:
    """``payload`` から ``factMap["T!T"]["aggregates"]`` を取り出す。

    ``row_count_idx`` が範囲外のときも ``None`` 。 想定外の入れ子を 1 か所に
    集約することで ``_extract_row_count_from_report_payload()`` 側のガード文を
    減らす。
    """
    if not isinstance(payload, dict):
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
    return fact_aggregates


def _coerce_row_count_value(value: object) -> int | None:
    """``fact_aggregates[idx]`` を ``RowCount`` として ``int`` に整形する。

    Salesforce の API は ``int`` か ``{"label": ..., "value": n}`` 形式の
    ``dict`` を返す。 ``bool`` は ``int`` のサブクラスなので先に除外する。
    それ以外の形（``str`` / ``float`` / その他 ``dict``）は ``None`` 。
    件数の比較にしか使わないので、要件外の形は黙って捨てる。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return _coerce_dict_value(value)
    return None


def _coerce_dict_value(value: dict) -> int | None:
    """``{"value": int}`` 形式の ``dict`` を ``int`` に整形する。

    ``bool`` は ``int`` のサブクラスなので先に除外する。 それ以外の
    キー（``label`` 等）は使わないので、 ``"value"`` だけが対象。
    """
    inner = value.get("value")
    if isinstance(inner, bool):
        return None
    if isinstance(inner, int):
        return inner
    return None


def _apply_soql_columns(
    entry: ReportEntry,
    report_cls: type[SoqlReport],
    table: Table,
    soql_text: str,
) -> Table:
    """列名の置き換えを適用した ``Table`` を返す。

    ``SoqlReport.COLUMN_NAMES`` が人が読む列名への対応表を持っている。
    ``soql-collector`` が出力する ``列名の対応.csv`` からも自動で拾う
    （手書きが無い列や新しいレポートでも、 soql-collector を一度走らせれば
    反映される）。 ``COLUMN_NAMES`` の **手書きは優先**（同じキーは手書きが勝つ）
    ──「直したい列だけ書けばよい」を崩さないため。

    見出しが衝突すると ``SoqlColumnNameConflictError`` が送出される。 ここは
    取得側なので、 CSV は書かれない（当該レポートだけの失敗になる）。

    引数は ``fetch_via_soql()`` が既に持っているもの（``report_cls`` /
    ``soql_text``）をそのまま使う（``soql_report_for()`` で引き直さない）。
    """
    # ``table.columns`` は ``Table`` が所有する ``list`` で、 後続の
    # ``apply_column_names()`` は新しい Table を返すので元の list は
    # 書き変わらない。 コピーは不要
    original_columns = table.columns
    extra = _load_soql_extra_column_names(entry, report_cls, soql_text)
    updated = apply_column_names(report_cls, table, extra=extra)
    _log_column_name_replacements(entry, original_columns, report_cls, extra)
    return updated


def _load_soql_extra_column_names(
    entry: ReportEntry,
    report_cls: type[SoqlReport],
    soql_text: str,
) -> dict[str, str]:
    """``列名の対応.csv`` からこのレポートの主オブジェクトに該当する行を引く。

    主オブジェクトが取れない（SOQL に ``FROM`` が無い等）ときは空 ``dict`` を
    返し、 API 名のまま保存させる（取得は失敗させない）。 WARNING には SOQL 本文
    （WHERE の値等）を出さず、 DEBUG にだけ本文を出す（既存の
    ``logger.debug("SOQL: %s", ...)`` と同ルール）。
    """
    main_object = extract_main_object(soql_text)
    if main_object is None:
        logger.warning(
            "SOQL から主オブジェクトを取り出せませんでした。"
            "API 名のまま保存します: 管理番号=%s, クラス=%s",
            entry.key,
            report_cls.__name__,
        )
        logger.debug("SOQL: %s", soql_text)
        return {}
    return load_column_names_for(main_object)


def _log_column_name_replacements(
    entry: ReportEntry,
    original_columns: list[str],
    report_cls: type[SoqlReport],
    extra: dict[str, str],
) -> None:
    """``COLUMN_NAMES`` で置き換わった列数を出所別に数えて INFO を出す。

    手書きが ``column_names`` に勝ち、かつ wins が元と変わった列だけが
    「手書きで置換」扱い。 0 件 = 全部元の見出しのままなら出さない（ログを
    常に汚さないように）。 列名そのものは見出しに過ぎず、認証情報にもレポート
    の値にも触れないので INFO で出してよい。
    """
    file_replaced = 0
    manual_replaced = 0
    for col in original_columns:
        if col in report_cls.COLUMN_NAMES:
            if report_cls.COLUMN_NAMES[col] != col:
                manual_replaced += 1
        elif col in extra:
            if extra[col] != col:
                file_replaced += 1
    replaced_count = file_replaced + manual_replaced
    if replaced_count > 0:
        logger.info(
            "COLUMN_NAMES で %d 列を置き換えました（ファイル=%d, 手書き=%d）: 管理番号=%s",
            replaced_count,
            file_replaced,
            manual_replaced,
            entry.key,
        )

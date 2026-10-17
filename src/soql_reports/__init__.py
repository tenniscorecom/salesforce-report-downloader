"""src/soql_reports/__init__.py — SOQLレポートの公開面。

``SalesforceBase.bulk_query()`` で取るレポートを 1 レポート=1ファイルで定義する。
**登録は自動** — ``reports/`` パッケージに ``SoqlReport`` サブクラスを置くと、
``_registry.registered_reports()`` が ``pkgutil.iter_modules`` で走査して
自動で集める。明示的なタプル編集は不要。

取得の入口は **管理表ルートだけ** に統一した。管理表の「SOQL」列が「○」の行は
取得実行側（``src.service.fetch_via_soql()`` / ``save_report()``）が
``soql_report_for()`` で管理番号から ``SoqlReport`` を引く。

``_registry.py`` は別モジュールに置く（``_registry`` 自体は ``src.soql_reports``
パッケージの別モジュールとして読み込み、 ``runner`` からの循環を避ける）。
"""

from src.exceptions import SoqlReportNotRegisteredError
from src.soql_reports import _registry
from src.soql_reports._registry import registered_reports
from src.soql_reports.base import SoqlReport

__all__ = ["SoqlReport", "registered_reports", "soql_report_for"]


def soql_report_for(key: str) -> type[SoqlReport]:
    """管理番号（``ReportEntry.key`` と同じ値）から ``SoqlReport`` サブクラスを引く。

    管理表の「SOQL」列が「○」の行を取得実行側が処理するときに使う想定。

    Args:
        key: 管理番号。``SoqlReport.KEY`` と一致するものを探す。

    Returns:
        該当する ``SoqlReport`` サブクラス。

    Raises:
        SoqlReportNotRegisteredError: ``registered_reports()`` に該当する ``KEY`` が無い場合
            （管理表の「SOQL」列を「○」にしたのに登録を忘れている設定ミス）。
    """
    # ``_registry`` モジュール経由で読む（import 時点のスナップショットに
    # 固定されないようにするため）。
    for report_cls in _registry.registered_reports():
        if key == report_cls.KEY:
            return report_cls
    raise SoqlReportNotRegisteredError(
        key, [report_cls.KEY for report_cls in _registry.registered_reports()]
    )

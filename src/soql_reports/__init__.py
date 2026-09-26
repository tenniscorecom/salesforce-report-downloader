"""src/soql_reports/__init__.py — SOQLレポートの公開面。

``SalesforceBase.query()`` で取るレポートを 1 レポート=1ファイルで定義する。
**登録は自動** — ``reports/`` パッケージに ``SoqlReport`` サブクラスを置くと、
``_registry.registered_reports()`` が ``pkgutil.iter_modules`` で走査して
自動で集める。明示的なタプル編集は不要。

``download_soql_reports()`` を呼ぶと ``registered_reports()`` の戻り値を取りに行く。
``reports/`` に何も置いていない状態なら空タプルが返り、何もしない（テストでは
``reports`` 引数で差し替える）。

管理表の「SOQL」列（``ReportEntry.use_soql``）が「○」の行は、取得実行側
（``src.service``）が ``soql_report_for()`` で管理番号から ``SoqlReport`` を引く。
``site_for()``（``comken.toolbox.salesforce.sites``）と同じ「URL・管理番号から
対応クラスを探す」役割。

``_registry.py`` は別モジュールに置く。``__init__.py`` が ``runner`` を
import する関係で、``runner`` から ``__init__.py`` を import すると循環するため
（``registered_reports()`` はこの循環を避けるためだけの存在）。
"""

from src.exceptions import SoqlReportNotRegisteredError
from src.soql_reports import _registry
from src.soql_reports._registry import registered_reports
from src.soql_reports.base import SoqlReport
from src.soql_reports.runner import download_soql_reports

__all__ = ["SoqlReport", "registered_reports", "download_soql_reports", "soql_report_for"]


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
    # ``_registry`` モジュール経由で読む（``download_soql_reports()`` と同じ理由）。
    # ``from registered_reports import ...`` だとこのモジュールの import 時点のスナップ
    # ショットに固定され、テストでの `_registry.registered_reports` 差し替えを拾えない
    for report_cls in _registry.registered_reports():
        if key == report_cls.KEY:
            return report_cls
    raise SoqlReportNotRegisteredError(
        key, [report_cls.KEY for report_cls in _registry.registered_reports()]
    )

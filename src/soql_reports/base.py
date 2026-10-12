r"""src/soql_reports/base.py — SOQLで取るレポートの基底クラス。

Report API（2000行上限）で取れない大きなレポートは、SOQL
（`SalesforceBase.bulk_query()`、上限なし）で取る。サブクラスは
1レポート=1ファイルで ``reports/`` に書く。
**登録は自動** — ファイルを置くと ``_registry.registered_reports()`` が
``pkgutil.iter_modules`` で走査して ``SoqlReport`` サブクラスを集める（明示的な
タプル編集は不要）。ファイル名が ``_`` で始まるモジュールは走査対象外
（``reports/_template.py`` のような雛形を登録せずに済む）。

    class LargeSalesReport(SoqlReport):
        KEY = "9001"

        def soql(self) -> str:
            return "SELECT Id, Name, Amount FROM Opportunity WHERE ..."

URL・保存先・0件可否は **管理表の ReportEntry** が単一の正。``SoqlReport``
には書かない（管理表なしでは取らない前提）。
"""

from __future__ import annotations

from typing import ClassVar


class SoqlReport:
    """Report API（2000行上限）で取れない大きなレポートを SOQL で取る基底クラス。

    サブクラスは ``KEY`` を上書きし、``soql()`` を実装する。1レポート=1ファイル
    で ``reports/`` に置くと ``_registry.registered_reports()`` が自動で登録する
    （ファイル名が ``_`` で始まるモジュールは対象外）。

    取得の入口は **管理表ルートだけ** に統一した。URL・保存先・0件可否は
    管理表の ``ReportEntry``（``url`` / ``group`` / ``allow_empty`` など）が
    単一の正で、ここには持たせない。``SoqlReport`` には ``KEY`` と ``soql()``
    だけを書き、管理表の「SOQL」列を「○」にして対応付ける。

    Attributes:
        KEY: 管理番号。``ReportEntry.key`` と同じ意味で、社内で決める論理的な
            番号（前ゼロ・記号入りも可）。Salesforce のレポート ID ではない。
            **空のままでは登録に失敗する**。
        COLUMN_NAMES: 出力 CSV の見出しを Salesforce の API 名（``Amount`` や
            ``Account.Name``）から人が読む列名に置き換える対応表。キーは
            ``soql()`` が返す SOQL の結果の列名（ドット付きの参照項目も
            そのまま）、値はその列を CSV に書くときの見出し。**貼り付け元は
            soql-collector の対応表**（人が ``reports/<ファイル>.py`` に
            コピペする前提）。空のときは何もしない。**結果に無いキーは無視**
            （SOQL を変えても対応を貼り直さずに済む）。置き換え後の見出しが
            2 つ以上重なると ``SoqlColumnNameConflictError``（CSV は書かれ
            ない）。管理表ルートの ``_save()`` が保存直前に適用する。
    """

    KEY: ClassVar[str] = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        """実行する SOQL クエリ文字列を返す。サブクラスで実装する。"""
        raise NotImplementedError

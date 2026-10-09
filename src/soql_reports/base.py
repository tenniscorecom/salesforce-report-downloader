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
        SUMMARY = "売上明細（SOQL、2000件超）"
        URL = "https://example.my.salesforce.com"
        FOLDER = r"\\server\share\reports\売上明細"

        def soql(self) -> str:
            return "SELECT Id, Name, Amount FROM Opportunity WHERE ..."

Excel の「スケジュール」シートとは独立している。いつ呼ぶかは呼び出し側
（プロジェクトの定期実行）が決める。
"""

from __future__ import annotations

from typing import ClassVar


class SoqlReport:
    """Report API（2000行上限）で取れない大きなレポートを SOQL で取る基底クラス。

    サブクラスは ``KEY`` / ``SUMMARY`` / ``URL`` / ``FOLDER`` を上書きし、
    ``soql()`` を実装する。1レポート=1ファイルで ``reports/`` に置くと
    ``_registry.registered_reports()`` が自動で登録する（ファイル名が ``_``
    で始まるモジュールは対象外）。

    Excel の「スケジュール」シートとは独立している。いつ呼ぶかは呼び出し側
    （プロジェクトの定期実行）が決める前提なので、この基底クラスには
    スケジュール判定を持たせない。

    Attributes:
        KEY: 管理番号。``download_scheduled()`` の ``ReportEntry.key`` と
            同じ意味で、社内で決める論理的な番号（前ゼロ・記号入りも可）。
            Salesforce のレポート ID ではない。**空のままでは登録に失敗する**。
        SUMMARY: 人が読んで何のレポートか分かる説明。保存するファイル名にも使われる。
        URL: レポートを開いた組織の My Domain の URL。``site_for()`` で
            組織を解決するために使う（``ReportEntry.url`` と同じ運用）。
        FOLDER: 保存先フォルダの絶対パス／UNC 文字列。**フォルダが無いと
            エラーにする**（``paths.move_into_place()`` と同じ判断。書き間違いに
            気づけるよう、勝手には作らない）。
        ALLOW_EMPTY: ``True`` なら 0 件のときも空 CSV を保存して成功扱い、
            ``False`` なら 0 件を ``EmptyReportError`` として失敗扱いする
            （``ReportEntry.allow_empty`` と同じ運用）。
        COLUMN_NAMES: 出力 CSV の見出しを Salesforce の API 名（``Amount`` や
            ``Account.Name``）から人が読む列名に置き換える対応表。キーは
            ``soql()`` が返す SOQL の結果の列名（ドット付きの参照項目も
            そのまま）、値はその列を CSV に書くときの見出し。**貼り付け元は
            soql-collector の対応表**（人が ``reports/<ファイル>.py`` に
            コピペする前提）。空のときは何もしない。**結果に無いキーは無視**
            （SOQL を変えても対応を貼り直さずに済む）。置き換え後の見出しが
            2 つ以上重なると ``SoqlColumnNameConflictError``（CSV は書かれ
            ない）。``runner._apply_column_names()`` が保存直前に適用する。
    """

    KEY: ClassVar[str] = ""
    SUMMARY: ClassVar[str] = ""
    URL: ClassVar[str] = ""
    FOLDER: ClassVar[str] = ""
    ALLOW_EMPTY: ClassVar[bool] = False
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        """実行する SOQL クエリ文字列を返す。サブクラスで実装する。"""
        raise NotImplementedError

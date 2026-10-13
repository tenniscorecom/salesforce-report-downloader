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
            そのまま）、値はその列を CSV に書くときの見出し。**手書きが無い
            列は ``soql-collector`` の出力（``列名の対応.csv``）から自動で
            引かれる**ため、 ``COLUMN_NAMES`` は **そのレポートだけ変えたい
            列だけ書けばよい**（同じキーは手書きが勝つ＝上書き）。空でも動く
            （その場合は ``列名の対応.csv`` の対応だけで置き換える）。**結果に
            無いキーは無視**（SOQL を変えても対応を貼り直さずに済む）。
            置き換え後の見出しが 2 つ以上重なると
            ``SoqlColumnNameConflictError`` （CSV は書かれない）。管理表
            ルートの ``_save()`` が保存直前に適用する。

            **2026-10 改定:** ``列名の対応.csv`` は **全体で 1 ファイル**
            （ ``主オブジェクト, SOQL列名, 表示名, 備考, 更新日`` の形式）に
            まとめられ、 ``(主オブジェクト, SOQL列名)`` が同じならどのレポート
            でも同じ表示名が引かれる。 ``service._save()`` は ``soql()`` の
            最初の ``FROM <名前>`` を主オブジェクトとして取り、
            ``column_names.load_column_names_for(main_object)`` で
            ``{SOQL列名: 表示名}`` を引き、 ``COLUMN_NAMES`` とマージする
            （手書き優先）。主オブジェクト名は Salesforce の API 名と同じく
            大文字小文字を区別しない。
    """

    KEY: ClassVar[str] = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

    def soql(self) -> str:
        """実行する SOQL クエリ文字列を返す。サブクラスで実装する。"""
        raise NotImplementedError

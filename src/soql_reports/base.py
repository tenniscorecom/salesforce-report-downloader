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

from comken.core.table.model import Table


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
            ルートの ``save_report()`` が保存直前に適用する。

            ``列名の対応.csv`` は **全体で 1 ファイル**
            （``主オブジェクト, SOQL列名, 表示名, 備考, 更新日`` の形式）に
            まとめられ、 ``(主オブジェクト, SOQL列名)`` が同じならどのレポート
            でも同じ表示名が引かれる。 ``service.save_report()`` は ``soql()`` の
            最初の ``FROM <名前>`` を主オブジェクトとして取り、
            ``column_names.load_column_names_for(main_object)`` で
            ``{SOQL列名: 表示名}`` を引き、 ``COLUMN_NAMES`` とマージする
            （手書き優先）。主オブジェクト名は Salesforce の API 名と同じく
            大文字小文字を区別しない。
        CHECK_COUNT: SOQL で取った件数を、レポート API の総件数と毎回比較するか。
            **既定は ``True``**（=比較する）。``fetch_via_soql()`` が
            ``bulk_query()`` の直後、同じ接続のうちにもう 1 度 Report API を
            叩いて ``factMap["T!T"]["aggregates"]`` の ``RowCount`` を取り、
            ``len(table)`` と比べる。一致しなければ ``SoqlCountMismatchError``
            で失敗扱い（保存しない）。比べるためのレポート ID が URL から
            取れない／``RowCount`` が無い／401・403 以外の HTTP エラーは
            WARNING を出して保存を続ける（401・403 は既存の失敗扱いに揃える）。
            **意図的にレポートと範囲が違う SOQL を書くときだけ** ``False`` に
            する（``False`` にする理由を ``#`` コメントに書く）。件数一致は
            中身の一致を意味しない点に注意。
    """

    KEY: ClassVar[str] = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}
    CHECK_COUNT: ClassVar[bool] = True

    def soql(self) -> str:
        """実行する SOQL クエリ文字列を返す。サブクラスで実装する。"""
        raise NotImplementedError

    def transform(self, table: Table) -> Table:
        """SOQL で取った Table を保存用に加工して返す。

        ``fetch_via_soql()`` の中で ``bulk_query()`` の直後、 件数の比較
        （``CHECK_COUNT``）と同じ接続のうち、 **加工前の生の結果**に
        対して呼ばれる（レポートの総件数と比べられるのは加工前の件数のため）。
        返した Table がそのまま CSV に書かれる。

        受け取る ``table`` の列名は SOQL の API 名（``Amount`` 、
        ``Account.Name``）。 返した ``Table`` には加工で新しく作った列が
        残っていてもよく、 列名の置き換え（``COLUMN_NAMES`` /
        ``列名の対応.csv``）はこのあと ``save_report()`` が保存直前に適用する。

        **既定は何もしない**（受け取った ``table`` をそのまま返す）。 サマリー
        レポートを Python で組み立てる用途（今までサマリーレポートでやって
        いたものを Python で加工して代わりにする）以外は上書きしなくてよい。
        例外が起きた／ ``Table`` 以外を返した場合は ``SoqlTransformError``
        に包まれて、 そのレポートだけの失敗として記録される（次レポートの取得は続く）。
        0 行を返したときは既存の ``ALLOW_EMPTY`` 判定がそのまま動く。

        Examples:
            ``Table`` の ``filter`` / ``select`` / ``group_by`` を使う::

                def transform(self, table):
                    return table.filter(lambda r: r["StageName"] == "Closed Won")

            pandas（``to_dataframe()`` → 加工 → ``from_dataframe()``）を使う::

                def transform(self, table):
                    df = table.to_dataframe()
                    summary = df.groupby("StageName")["Amount"].sum().reset_index()
                    return Table.from_dataframe(summary)
        """
        return table

r'''src/reports/base.py — SOQLで取るレポートの基底クラス。

Report API（2000行上限）で取れない大きなレポートは、SOQL
（``SalesforceBase.bulk_query()``、上限なし。``SoqlReport.USE_BULK`` を
``False`` にすると ``SalesforceBase.query()``（REST API）で取る）で取る。
サブクラスは 1レポート=1ファイルで ``src/reports/`` に書く。
**登録は自動** — ファイルを置くと ``_registry.registered_reports()`` が
``pkgutil.iter_modules`` で走査して ``SoqlReport`` サブクラスを集める（明示的な
タプル編集は不要）。ファイル名が ``_`` で始まるモジュールは走査対象外
（``src/reports/_template.py`` のような雛形を登録せずに済む）。

    class LargeSalesReport(SoqlReport):
        KEY = "9001"

        def soql(self) -> str:
            return """
                SELECT
                    Id,
                    Name,
                    Amount
                FROM Opportunity
                WHERE
                    CloseDate = LAST_N_DAYS:30
            """

SOQL は **三重引用符の文字列にそのまま書く**（関数で包まない）。 動的にしたい
日付だけ f-string で差し込む。 詳しくは ``_template.py`` の ``soql()`` を参照。

URL・保存先・0件可否は **管理表の ReportEntry** が単一の正。``SoqlReport``
には書かない（管理表なしでは取らない前提）。
'''

from __future__ import annotations

from typing import ClassVar

from comken.core.table.model import Table


class SoqlReport:
    """Report API（2000行上限）で取れない大きなレポートを SOQL で取る基底クラス。

    サブクラスは ``KEY`` を上書きし、``soql()`` を実装する。1レポート=1ファイル
    で ``src/reports/`` に置くと ``_registry.registered_reports()`` が自動で登録する
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
            ルートの ``fetch_via_soql()`` が **``transform()`` の前** に
            適用する（＝``transform()`` は表示名の列を受け取る）。

            ``列名の対応.csv`` は **全体で 1 ファイル**
            （``主オブジェクト, SOQL列名, 表示名, 備考, 更新日`` の形式）に
            まとめられ、 ``(主オブジェクト, SOQL列名)`` が同じならどのレポート
            でも同じ表示名が引かれる。 ``service.fetch_via_soql()`` は ``soql()`` の
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
        USE_BULK: SOQL の取り方。**既定は ``True``**（= Bulk API 2.0 で
            取る）。 ``True`` のときは ``SalesforceBase.bulk_query()`` を
            使い、 サーバ側の非同期ジョブで処理するため 件数が多い取得で
            往復回数を抑えられる。 集計関数（``COUNT`` / ``SUM`` 等）・
            ``GROUP BY``・``OFFSET``・SELECT 句の中の子サブクエリは
            ``bulk_query()`` が受け付けない（ジョブ作成時に 400 で失敗する）。
            ``False`` にすると ``SalesforceBase.query()``（REST API・
            ページ送り）で取るので、 集計関数・``GROUP BY`` も使える。
            **注意**: ``False`` のときは値が文字列にならず **数値・真偽値の
            まま** ``transform()`` に渡る（``bulk_query()`` は全部文字列）。
            結果が 0 件だと列が空になる（``bulk_query()`` は CSV の見出しから
            列名を取るため 0 件でも列が残る）。 全行で null の参照項目は
            列が出ない（``query()`` は応答に現れたキーから列を作るため）。
    """

    KEY: ClassVar[str] = ""
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}
    CHECK_COUNT: ClassVar[bool] = True
    USE_BULK: ClassVar[bool] = True

    def soql(self) -> str:
        """実行する SOQL クエリ文字列を返す。サブクラスで実装する。"""
        raise NotImplementedError

    def transform(self, table: Table) -> Table:
        """SOQL で取った Table を保存用に加工して返す。

        ``fetch_via_soql()`` の中で 取得（``bulk_query()`` / ``query()``）の
        直後、 件数の比較（``CHECK_COUNT``）と同じ接続のうち、 **列名を
        置き換えたあと** の Table に対して呼ばれる（レポートの総件数と
        比べられるのは加工前の件数なので、 件数の比較は列名の置き換えより
        先に行う）。
        返した Table がそのまま CSV に書かれる。

        受け取る ``table`` の列名は **置き換え後の表示名**（``COLUMN_NAMES`` /
        ``列名の対応.csv`` で置き換えたもの。 対応が無い列は SOQL の API 名の
        まま）。 返した ``Table`` には加工で新しく作った列が残っていてもよく、
        その列はそのまま CSV に出る。

        **既定は何もしない**（受け取った ``table`` をそのまま返す）。 サマリー
        レポートを Python で組み立てる用途（今までサマリーレポートでやって
        いたものを Python で加工して代わりにする）以外は上書きしなくてよい。
        例外が起きた／ ``Table`` 以外を返した場合は ``SoqlTransformError``
        に包まれて、 そのレポートだけの失敗として記録される（次レポートの取得は続く）。
        0 行を返したときは既存の ``ALLOW_EMPTY`` 判定がそのまま動く。

        Examples:
            ``Table`` の ``filter`` / ``select`` / ``group_by`` を使う
            （列は表示名を書く）::

                def transform(self, table):
                    return table.filter(lambda r: r["ステージ"] == "成約済み")

            ``group_by`` で集計する::

                def transform(self, table):
                    rows = [
                        {"ステージ": name, "金額合計": sum(int(r["金額"]) for r in sub)}
                        for name, sub in table.group_by("ステージ").items()
                    ]
                    return Table(["ステージ", "金額合計"], rows)
        """
        return table

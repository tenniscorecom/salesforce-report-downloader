"""src/soql_reports/reports/_template.py — 雛形（未使用）。

**このファイルはプレースホルダー。** コピーして ``reports/`` に実際のレポート名で
リネームして使う。ファイル名が ``_`` で始まるので ``registered_reports()`` の
走査対象にならず、登録されない（リネームすると自動で登録される）。
``KEY`` が空のまま残すと登録時にエラーになるので、**必ず管理番号を埋めてから**
使うこと。書き方は ``base.py`` のモジュール docstring、詳しい手順は
``docs/salesforce-downloader.md``「SOQLレポート（2000件超のレポートを移行する）」を参照。

``COLUMN_NAMES`` を書かなくても、 ``soql-collector`` の出力先（``OUTPUT_DIR``）が
``COLUMN_NAMES_CSV_PATH`` のフォルダにあれば ``列名の対応.csv`` から自動で
置き換わる。手書きは「直したい列だけ書けばよい」（同じキーは手書きが勝つ）。
``列名の対応.csv`` は **主オブジェクト × SOQL列名 で全体共通**の表示名を持つ
形式で、 このレポートの ``soql()`` の ``FROM <名前>`` で引かれる。同じ
``(主オブジェクト, SOQL列名)`` ならどのレポートでも同じ表示名が引かれるので、
レポートごとの表示名の差はこの ``COLUMN_NAMES`` で揃える。

``transform()`` を上書きすると、SOQL の生データを Python で加工して保存できる
（サマリーレポートを Python で組み立てる用途）。 既定のままで動くなら
上書きしなくてよい。 詳細は ``SoqlReport.transform()`` / ``docs`` 参照。
"""

from typing import ClassVar

from comken.core.table.model import Table

from src.soql_reports.base import SoqlReport


class NewReport(SoqlReport):
    """TODO: レポートの説明に書き換える（登録前にファイル名・クラス名もリネーム）。"""

    KEY = ""  # TODO: 社内で決める管理番号（例: "9001"）
    # 任意: {"Amount": "金額", "Account.Name": "取引先名"}
    # （無くても ``列名の対応.csv`` から自動置換）
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}
    # 任意: レポートと範囲が違う SOQL のときだけ ``False`` にする（理由をコメントに書く）
    CHECK_COUNT: ClassVar[bool] = True

    def soql(self) -> str:
        """TODO: 実行する SOQL クエリ文字列を返す。

        取得は Bulk API 2.0（``bulk_query()``）で行われるため、集計関数
        (``COUNT`` / ``SUM`` など)・``GROUP BY``・``OFFSET``・SELECT 句の中の
        子サブクエリ（例: ``SELECT Id, (SELECT Id FROM Contacts) FROM Account``）
        はジョブ作成時に 400 で失敗する。

        戻り値の ``Table`` は **全フィールドが文字列**（真偽値は ``"true"`` /
        ``"false"``、null は空文字）。参照項目（``Account.Name`` など）は
        ``Account.Name`` の形の列名になり、平坦化されない。

        URL・保存先・0件可否は管理表の ``ReportEntry`` が単一の正なので
        ここには書かない。
        """
        raise NotImplementedError("TODO: SOQL文をここに書く")

    # 任意: 取得した ``Table`` を Python で加工して保存する。 既定は何もしない。
    # サマリーレポートを Python で組み立てる用途（今までサマリーレポートで
    # やっていたものを Python で加工して代わりにする）以外は上書きしなくてよい。
    # 列名の置き換え（ ``COLUMN_NAMES`` / ``列名の対応.csv`` ）はこのあとに
    # ``_save()`` が保存直前に適用するため、 加工後の列名がそのまま CSV に出る。
    #
    # 例 1: Table の ``filter`` で行を絞る
    #
    #     def transform(self, table: Table) -> Table:
    #         return table.filter(lambda row: row["StageName"] == "Closed Won")
    #
    # 例 2: Table の ``select`` で列を絞る
    #
    #     def transform(self, table: Table) -> Table:
    #         return table.select("Id", "Name", "Amount")
    #
    # 例 3: pandas で group_by する（pandas がインストールされている環境のみ）
    #
    #     def transform(self, table: Table) -> Table:
    #         df = table.to_dataframe()
    #         summary = df.groupby("StageName", as_index=False)["Amount"].sum()
    #         return Table.from_dataframe(summary)
    #
    # 加工中の例外は ``SoqlTransformError`` （ ``DownloaderError`` 系）に包まれ、
    # そのレポートだけの失敗として記録される（次レポートの取得は続く）。
    def transform(self, table: Table) -> Table:
        """TODO: 必要なら取得した ``Table`` を加工して返す（上書きは任意）。"""
        return table

"""src/reports/_template.py — 雛形（未使用）。

**このファイルはプレースホルダー。** コピーして ``src/reports/`` に実際のレポート名で
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
上書きしなくてよい。 受け取る ``table`` の列名は **表示名**（``COLUMN_NAMES`` /
``列名の対応.csv`` で置き換えたもの。 対応が無い列は SOQL の API 名のまま）。
詳細は ``SoqlReport.transform()`` / ``docs`` 参照。
"""

from typing import ClassVar

from comken.core.table.model import Table

from src.reports.base import SoqlReport


class NewReport(SoqlReport):
    """TODO: レポートの説明に書き換える（登録前にファイル名・クラス名もリネーム）。"""

    KEY = ""  # TODO: 社内で決める管理番号（例: "9001"）
    # 任意: {"Amount": "金額", "Account.Name": "取引先名"}
    # （無くても ``列名の対応.csv`` から自動置換）
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}
    # 任意: レポートと範囲が違う SOQL のときだけ ``False`` にする（理由をコメントに書く）
    CHECK_COUNT: ClassVar[bool] = True
    # 任意: 取り方。``True``（既定）= Bulk API 2.0、``False`` = REST の
    # ``query()``（集計関数・``GROUP BY`` が使える。値は文字列にならない）
    USE_BULK: ClassVar[bool] = True

    def soql(self) -> str:
        '''TODO: 実行する SOQL クエリ文字列を返す。

        既定（``USE_BULK=True``）の Bulk API 2.0（``bulk_query()``）での取得では、
        集計関数（``COUNT`` / ``SUM`` など）、``GROUP BY``、``OFFSET``、
        SELECT 句の中の子サブクエリ（例: ``SELECT Id, (SELECT Id FROM Contacts)
        FROM Account``）はジョブ作成時に 400 で失敗する。 集計を使う SOQL なら
        ``USE_BULK=False``（REST の ``query()``）にする。

        戻り値の ``Table`` は **全フィールドが文字列**（真偽値は ``"true"`` /
        ``"false"``、null は空文字）。参照項目（``Account.Name`` など）は
        ``Account.Name`` の形の列名になり、平坦化されない。
        ``USE_BULK=False`` のときは値が文字列にならず数値・真偽値のままで、
        結果が 0 件だと列が空になり、全行で null の参照項目は列が出ない
        （列は応答に現れたキーから作るため）。

        URL・保存先・0件可否は管理表の ``ReportEntry`` が単一の正なので
        ここには書かない。

        SOQL は **三重引用符の文字列にそのまま書く**（関数で包まない）。
        ``SELECT`` / ``FROM`` / ``WHERE`` を行で分け、 中身は 1 段インデント。
        こうしておくと、 AI に読ませても「どの SOQL を出すか」がそのまま分かる。
        動的にしたい日付だけ f-string で差し込む。

        Examples:
            相対日付リテラル（``LAST_N_DAYS:30`` など）で済むものはそのまま
            書く::

                def soql(self) -> str:
                    return """
                        SELECT
                            Id,
                            Name,
                            CloseDate
                        FROM Opportunity
                        WHERE
                            CloseDate = LAST_N_DAYS:30
                    """

            相対日付リテラルに無い日付（前営業日）は **日付だけ** f-string で
            差し込む::

                from datetime import timedelta

                from comken.core.dates import clock
                from comken.core.dates.holidays import workday

                from src.reports.base import SoqlReport


                class PreviousWorkdayReport(SoqlReport):
                    KEY = "9003"
                    # レポートの相対日付リテラルに「前営業日」が無いので比較しない
                    CHECK_COUNT = False

                    def soql(self) -> str:
                        prev = workday(clock.today(), -1)  # 前営業日
                        next_day = prev + timedelta(days=1)
                        return f"""
                            SELECT
                                Id,
                                Name
                            FROM Opportunity
                            WHERE
                                CloseDate = {prev:%Y-%m-%d}
                                AND CreatedDate >= {prev:%Y-%m-%d}T00:00:00+09:00
                                AND CreatedDate < {next_day:%Y-%m-%d}T00:00:00+09:00
                        """

        注意:
            - 日付型の項目（``CloseDate`` など）は ``YYYY-MM-DD``、 日時型の項目
              （``CreatedDate`` など）は ``+09:00`` を付けて日本時間で書く。
              ``Z`` を付けると UTC になり 9 時間ずれる。
            - 基準日は ``clock.today()`` を使う（``date.today()`` でなく。 テストで
              日付を固定できる）。
            - ``CHECK_COUNT`` は Report API のレポートの総件数と比べる。 レポートに
              同じ範囲の日付条件が無いと件数が合わず失敗するので、 レポートと範囲が
              違う SOQL にするときは ``CHECK_COUNT = False`` にして理由をコメントに
              書く（「前営業日」はレポートの相対日付リテラルに無い）。
            - SELECT 句の子サブクエリ（``(SELECT ... FROM Contacts)``）を使うときは
              ``USE_BULK = False``。
        '''
        raise NotImplementedError("TODO: SOQL文をここに書く")

    # 任意: 取得した ``Table`` を Python で加工して保存する。 既定は何もしない。
    # サマリーレポートを Python で組み立てる用途以外は上書きしなくてよい。
    # 列名の置き換え（``COLUMN_NAMES`` / ``列名の対応.csv``）は **この前** に
    # ``fetch_via_soql()`` が適用する。 受け取る ``table`` の列名は **表示名**
    # （対応が無い列は SOQL の API 名のまま）なので、 加工のコードは表示名で書く。
    #
    # 例 1: Table の ``filter`` で行を絞る
    #
    #     def transform(self, table: Table) -> Table:
    #         return table.filter(lambda row: row["ステージ"] == "成約済み")
    #
    # 例 2: Table の ``select`` で列を絞る
    #
    #     def transform(self, table: Table) -> Table:
    #         return table.select("Id", "商談名", "金額")
    #
    # 例 3: Table の ``group_by`` で集計する
    #
    #     def transform(self, table: Table) -> Table:
    #         rows = [
    #             {"ステージ": name, "金額合計": sum(int(r["金額"]) for r in sub)}
    #             for name, sub in table.group_by("ステージ").items()
    #         ]
    #         return Table(["ステージ", "金額合計"], rows)
    #
    # 加工中の例外は ``SoqlTransformError`` （``DownloaderError`` 系）に包まれ、
    # そのレポートだけの失敗として記録される（次レポートの取得は続く）。
    def transform(self, table: Table) -> Table:
        """TODO: 必要なら取得した ``Table`` を加工して返す（上書きは任意）。"""
        return table

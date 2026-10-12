"""src/soql_reports/reports/_template.py — 雛形（未使用）。

**このファイルはプレースホルダー。** コピーして ``reports/`` に実際のレポート名で
リネームして使う。ファイル名が ``_`` で始まるので ``registered_reports()`` の
走査対象にならず、登録されない（リネームすると自動で登録される）。
``KEY`` が空のまま残すと登録時にエラーになるので、**必ず管理番号を埋めてから**
使うこと。書き方は ``base.py`` のモジュール docstring、詳しい手順は
``docs/salesforce-downloader.md``「SOQLレポート（2000件超のレポートを移行する）」を参照。
"""

from typing import ClassVar

from src.soql_reports.base import SoqlReport


class NewReport(SoqlReport):
    """TODO: レポートの説明に書き換える（登録前にファイル名・クラス名もリネーム）。"""

    KEY = ""  # TODO: 社内で決める管理番号（例: "9001"）
    # 任意: {"Amount": "金額", "Account.Name": "取引先名"}
    COLUMN_NAMES: ClassVar[dict[str, str]] = {}

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

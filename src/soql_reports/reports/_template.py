"""src/soql_reports/reports/_template.py — 雛形（未使用）。

**このファイルはプレースホルダー。** コピーして ``reports/`` に実際のレポート名で
リネームして使う。ファイル名が ``_`` で始まるので ``registered_reports()`` の
走査対象にならず、登録されない（リネームすると自動で登録される）。
``KEY`` が空のまま残すと登録時にエラーになるので、**必ず管理番号を埋めてから**
使うこと。書き方は ``base.py`` のモジュール docstring、詳しい手順は
``docs/salesforce-downloader.md``「SOQLレポート（2000件超のレポートを移行する）」を参照。
"""

from src.soql_reports.base import SoqlReport


class NewReport(SoqlReport):
    """TODO: レポートの説明に書き換える（登録前にファイル名・クラス名もリネーム）。"""

    KEY = ""  # TODO: 社内で決める管理番号（例: "9001"）
    SUMMARY = ""  # TODO: 人が読んで分かる説明（保存するファイル名にも使われる）
    URL = ""  # TODO: 接続先組織のMy Domain URL（site_for()が組織を解決する）
    FOLDER = r""  # TODO: 保存先フォルダの絶対パス／UNC（存在しないとエラー。勝手に作らない）
    ALLOW_EMPTY = False  # TODO: 0件を失敗として扱うか（普段データがあるなら False のまま）

    def soql(self) -> str:
        """TODO: 実行する SOQL クエリ文字列を返す。"""
        raise NotImplementedError("TODO: SOQL文をここに書く")

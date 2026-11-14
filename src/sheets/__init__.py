"""src/sheets/__init__.py — シート単位のモジュール置き場。

ワークブックの「1枚（1ファイル）」ごとに、そこにある列と意味を宣言する
モジュールを集めている:

    master.py           レポート管理表シート（``ReportEntry``）
    schedule.py         スケジュールシート（``ScheduleRule``）
    group_settings.py   設定シート（``GroupSetting``、出力先のベースパスを管理）

**履歴CSVは `sheets/` には無い。** 履歴の列・読み取り・書き込みは comken 側にあり
（``comken.services.salesforce_downloader.history``）、ダウンローダー側は
``src/history.py`` の ``record()`` が管理表の行から値を組み立てて委譲する。

Excel を読む・雛形を作る共通の仕組み（``MasterRow`` / ``column()``）は
`sheets/` の外、`src.report_master` にある。
"""

"""src/initial_rows.py — 雛形に最初から入れておきたい行（=「初期行」）の宣言。

``create_combined_workbook()`` や ``create_template()`` を、**既存のファイルが
無いパス**に対して実行したとき、ここに書いた行が雛形に最初から入る。
``MASTER_PATH`` などの既存の管理表が既にある場合はマイグレーションが優先され、
初期行は**無視**される（既存の業務データを上書きしないため）。

雛形を空（見出しだけ）で生成したい場合は、3つのリストを空のままにしておく。
既定ではすべて空。雛形を「記入例で埋める」運用は廃止した。

書き方（``ReportEntry`` / ``ScheduleRule`` / ``GroupSetting`` の frozen dataclass
をそのままインスタンス化して書く。``column()`` に既定値がある列は省略できる）::

    from src.sheets.group_settings import GroupSetting
    from src.sheets.master import ReportEntry
    from src.sheets.schedule import ScheduleRule

    REPORT_ENTRIES: list[ReportEntry] = [
        ReportEntry(
            key="1001",
            group="営業本部",
            assignee="山田太郎",
            summary="顧客一覧",
            url="https://example.salesforce.com/lightning/r/Report/00O.../view",
            enabled=False,  # 雛形として配置する時点では無効にしておく
        ),
    ]

    SCHEDULE_RULES: list[ScheduleRule] = [
        ScheduleRule(
            schedule_key="S001",
            report_key="1001",
            frequency="毎日",
            enabled=False,
        ),
    ]

    GROUP_SETTINGS: list[GroupSetting] = [
        GroupSetting(group="営業本部", base_path=Path("\\\\server\\share\\営業本部")),
    ]

初期行の ID（``key``）重複・必須列の欠落は ``ReportEntry`` 等を作る時点か、
雛形から読み戻した時点で既存の検証が見つける。**新しい検証は足していない。**

``ScheduleRule`` の「頻度」と「曜日」「日付」の組み合わせ（``毎週`` なのに
曜日が空、等）は ``ScheduleRule.validate()`` で検出されるが、これは
``MasterRow.load()`` 経由でしか呼ばれない。初期行を直接 ``ScheduleRule(...)``
で組み立てるときは、組み合わせが破綻していないか自分で確認する必要がある。
"""

from src.sheets.group_settings import GroupSetting
from src.sheets.master import ReportEntry
from src.sheets.schedule import ScheduleRule

REPORT_ENTRIES: list[ReportEntry] = []
SCHEDULE_RULES: list[ScheduleRule] = []
GROUP_SETTINGS: list[GroupSetting] = []


__all__ = ["REPORT_ENTRIES", "SCHEDULE_RULES", "GROUP_SETTINGS"]

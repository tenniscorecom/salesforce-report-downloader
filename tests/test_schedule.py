"""ScheduleRule.from_row() の行パースを検証する。

型変換は report_master.py の _to_bool / _to_time を共用しているため、
「はい」等の表記や datetime セルの受け取りもここで確かめる。
"""

import datetime as dt

import pytest

from comken_salesforce_downloader.exceptions import (
    ScheduleRequiredValueMissingError,
    ScheduleWeekdayInvalidError,
)
from comken_salesforce_downloader.schedule import ScheduleRule


def base_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "スケジュールキー": "S1",
        "レポートキー": "1001",
        "取得頻度": "毎日",
        "取得時刻": "09:00",
        "有効": "○",
    }
    row.update(overrides)
    return row


class TestFromRow:
    """必須項目と型変換。"""

    def test_required_fields_missing_raises(self):
        row = base_row()
        del row["レポートキー"]
        with pytest.raises(ScheduleRequiredValueMissingError):
            ScheduleRule.from_row(row)

    @pytest.mark.parametrize("text", ["○", "有効", "はい", "true", "True", "TRUE"])
    def test_enabled_accepts_common_true_words(self, text):
        rule = ScheduleRule.from_row(base_row(有効=text))
        assert rule.enabled is True

    def test_enabled_defaults_to_false_when_blank(self):
        rule = ScheduleRule.from_row(base_row(有効=""))
        assert rule.enabled is False

    def test_run_time_accepts_isoformat_string(self):
        rule = ScheduleRule.from_row(base_row(取得時刻="09:30"))
        assert rule.run_time == dt.time(9, 30)

    def test_run_time_accepts_datetime_cell(self):
        # Excel の時刻セルは datetime で返ることがある
        rule = ScheduleRule.from_row(base_row(取得時刻=dt.datetime(2026, 1, 1, 9, 30, 45)))  # noqa: DTZ001
        assert rule.run_time == dt.time(9, 30)

    def test_weekday_parses_japanese_name(self):
        rule = ScheduleRule.from_row(base_row(取得頻度="毎週", 曜日="水"))
        assert rule.weekday == 2

    def test_weekday_invalid_raises(self):
        with pytest.raises(ScheduleWeekdayInvalidError):
            ScheduleRule.from_row(base_row(取得頻度="毎週", 曜日="不明"))

    def test_month_end_accepts_true_words(self):
        rule = ScheduleRule.from_row(base_row(月末指定="○"))
        assert rule.month_end is True


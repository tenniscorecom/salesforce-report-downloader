"""comken_salesforce_downloader/schedule.py — 取得時刻を判定する。"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from comken_salesforce_downloader.exceptions import (
    ScheduleIntervalMissingError,
    ScheduleRequiredValueMissingError,
    ScheduleWeekdayInvalidError,
    UnsupportedScheduleFrequencyError,
)
from comken_salesforce_downloader.report_master import _to_bool, _to_time

FREQUENCY_HOURLY = "1時間ごと"
FREQUENCY_DAILY = "毎日"
FREQUENCY_WEEKLY = "毎週"
FREQUENCY_MONTHLY = "毎月"
HOLIDAY_SKIP = "取得しない"
WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")


@dataclass(frozen=True)
class ScheduleRule:
    """取得スケジュール管理表の1行。"""

    schedule_key: str
    report_key: str
    frequency: str
    run_time: dt.time | None = None
    start_time: dt.time | None = None
    end_time: dt.time | None = None
    interval_minutes: int | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    month_end: bool = False
    holiday_policy: str = HOLIDAY_SKIP
    enabled: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> Self:
        """日本語カラム名の辞書からスケジュールを作る。"""
        return cls(
            schedule_key=_required_text(row, "スケジュールキー"),
            report_key=_required_text(row, "レポートキー"),
            frequency=_required_text(row, "取得頻度"),
            run_time=_to_time(row.get("取得時刻")),
            start_time=_to_time(row.get("取得開始時刻")),
            end_time=_to_time(row.get("取得終了時刻")),
            interval_minutes=_parse_int(row.get("取得間隔（分）")),
            weekday=_parse_weekday(row.get("曜日")),
            day_of_month=_parse_int(row.get("日付")),
            month_end=_to_bool(row.get("月末指定")),
            holiday_policy=_text_or_default(row, "祝日対応", HOLIDAY_SKIP),
            enabled=_to_bool(row.get("有効")),
        )

    def is_due(
        self,
        now: dt.datetime,
        *,
        holidays: set[dt.date] | frozenset[dt.date] = frozenset(),
    ) -> bool:
        """指定時刻にこのスケジュールを実行すべきか判定する。"""
        if not self.enabled or not self._date_matches(now.date(), holidays):
            return False
        if self.frequency == FREQUENCY_HOURLY:
            return self._is_hourly_due(now)
        if self.frequency in {FREQUENCY_DAILY, FREQUENCY_WEEKLY, FREQUENCY_MONTHLY}:
            return self.run_time is not None and now.time() >= self.run_time
        raise UnsupportedScheduleFrequencyError(self.frequency)

    def job_key(self, target_date: dt.date) -> str:
        """履歴で取得済みか判定するキーを返す。"""
        return f"{self.schedule_key}:{target_date.isoformat()}"

    def _date_matches(self, date: dt.date, holidays: set[dt.date] | frozenset[dt.date]) -> bool:
        if self.holiday_policy == HOLIDAY_SKIP and date in holidays:
            return False
        if self.weekday is not None and date.weekday() != self.weekday:
            return False
        if self.day_of_month is not None and date.day != self.day_of_month:
            return False
        return not self.month_end or (date + dt.timedelta(days=1)).month != date.month

    def _is_hourly_due(self, now: dt.datetime) -> bool:
        if self.start_time is None or self.end_time is None or not self.interval_minutes:
            raise ScheduleIntervalMissingError()
        if now.time() < self.start_time or now.time() > self.end_time:
            return False
        start_minutes = self.start_time.hour * 60 + self.start_time.minute
        now_minutes = now.hour * 60 + now.minute
        return (now_minutes - start_minutes) % self.interval_minutes == 0


def _required_text(row: Mapping[str, object], column: str) -> str:
    value = str(row.get(column, "")).strip()
    if not value:
        raise ScheduleRequiredValueMissingError(column)
    return value


def _text_or_default(row: Mapping[str, object], column: str, default: str) -> str:
    value = str(row.get(column, "")).strip()
    return value or default


def _parse_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(str(value).strip())


def _parse_weekday(value: object) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip().removesuffix("曜日")
    if text not in WEEKDAY_NAMES:
        raise ScheduleWeekdayInvalidError(value)
    return WEEKDAY_NAMES.index(text)


__all__ = ["ScheduleRule"]


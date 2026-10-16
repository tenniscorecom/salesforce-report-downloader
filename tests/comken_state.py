"""comken の内部状態をテスト間で初期化する（comken 本体にはテスト用の入口が無いため）。"""

from __future__ import annotations

from comken.core.dates import holidays


def set_holiday_calendar(calendar: holidays._Holidays | None) -> None:
    """既定の祝日カレンダーを差し替える。``None`` で次回アクセス時に CSV から作り直す。"""
    holidays._singleton = calendar

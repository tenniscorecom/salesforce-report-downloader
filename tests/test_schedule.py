"""``ScheduleRule`` の ``raw_weekday`` / ``raw_day_of_month`` のパースと判定ロジックを検証する。

``raw_*`` フィールドは宣言だけでパースは行わず、``@property`` で派生値（``weekday`` /
``day_of_month`` / ``month_end`` / ``nth_business_day``）を取り出す。型変換は
``report_master.py`` の ``_to_bool`` / ``_to_time`` を ``MasterRow`` 経由で共用
しているため、``is_due()`` 側の挙動を主にここで確かめる。
"""

import datetime as dt
from typing import Any

import pytest
from comken.core.holidays import (
    is_holiday,
    is_workday,
    nth_workday,
)
from comken.core.holidays._holidays import _Holidays, _set_calendar_for_test

from src.exceptions import DownloaderError
from src.sheets.schedule import (
    FREQUENCY_BUSINESS_DAY,
    FREQUENCY_DAILY,
    HOLIDAY_AFTER,
    HOLIDAY_BEFORE,
    HOLIDAY_FETCH,
    HOLIDAY_SKIP,
    ScheduleRule,
)


def _rule(**overrides: Any) -> ScheduleRule:
    """ベースになる ``ScheduleRule`` を返す。``overrides`` でフィールドを差し替える。

    ``__init__`` のシグネチャは ``@dataclass(kw_only=True)`` のため、``column()``
    宣言のあるフィールドだけを ``**overrides`` で渡せば、残りは ``field`` の
    既定値（``str`` の ``""`` / ``bool`` の ``True`` / ``None`` / ``HOLIDAY_SKIP``）
    が埋まる。
    """
    defaults: dict[str, Any] = {
        "schedule_key": "S1",
        "report_key": "1001",
        "frequency": "毎日",
        "start_time": dt.time(9, 0),
        "raw_weekday": "",
        "raw_day_of_month": "",
        "holiday_policy": "取得しない",
        "enabled": True,
    }
    defaults.update(overrides)
    return ScheduleRule(**defaults)


# 会社休日（年末年始休暇 12/29-1/3）の影響を受けない **テスト専用** カレンダーを
# 作るヘルパー。国民の祝日を意図的に固定してテストする。テストの終わりに
# ``_set_calendar_for_test(None)`` でリセットする。


def _holiday_calendar(holiday_dates: set[dt.date]) -> _Holidays:
    """国民の祝日として ``holiday_dates`` を持つ ``_Holidays`` を返す。"""
    return _Holidays({d: f"h{d}" for d in sorted(holiday_dates)})


class _HolidaysScope:
    """``_set_calendar_for_test`` のセットアップ／後始末コンテキスト。

    pytest fixture ではなく with `` ``ブロックで使う。テスト中に祝日判定が
    影響を受ける場合のみ利用する（既定カレンダーを使うテストでは使わない）。
    """

    def __init__(self, holiday_dates: set[dt.date]) -> None:
        self._holiday_dates = holiday_dates

    def __enter__(self) -> _Holidays:
        cal = _holiday_calendar(self._holiday_dates)
        _set_calendar_for_test(cal)
        return cal

    def __exit__(self, *_args: object) -> None:
        _set_calendar_for_test(None)


class TestWeekdayProperty:
    """``raw_weekday`` から ``weekday`` プロパティへの変換。"""

    def test_returns_int_for_japanese_name(self):
        rule = _rule(frequency="毎週", raw_weekday="水")
        assert rule.weekday == 2

    def test_blank_returns_none(self):
        rule = _rule()
        assert rule.weekday is None

    def test_invalid_raises(self):
        rule = _rule(raw_weekday="不明")
        with pytest.raises(DownloaderError, match="曜日"):
            _ = rule.weekday


class TestDayOfMonthProperty:
    """``raw_day_of_month`` から ``day_of_month`` / ``month_end`` / ``nth_business_day`` への変換。

    3 つのプロパティを1クラスでまとめて検証する（パース入口が共通なので、テストも
    一箇所に集約する方が読みやすい）。
    """

    def test_number(self):
        rule = _rule(frequency="毎月", raw_day_of_month="15")
        assert rule.day_of_month == 15
        assert rule.month_end is False
        assert rule.nth_business_day is None

    def test_month_end_word(self):
        rule = _rule(raw_day_of_month="月末")
        assert rule.month_end is True
        assert rule.day_of_month is None
        assert rule.nth_business_day is None

    def test_nth_business_day(self):
        rule = _rule(raw_day_of_month="第2営業日")
        assert rule.nth_business_day == 2
        assert rule.day_of_month is None
        assert rule.month_end is False

    def test_nth_business_day_above_ten(self):
        """N が 10 以上の桁でも拾える。"""
        rule = _rule(raw_day_of_month="第15営業日")
        assert rule.nth_business_day == 15
        assert rule.day_of_month is None
        assert rule.month_end is False

    def test_blank_returns_no_specification(self):
        rule = _rule()
        assert rule.day_of_month is None
        assert rule.month_end is False
        assert rule.nth_business_day is None

    @pytest.mark.parametrize("text", ["第二営業日", "2営業日", "第2", "来月"])
    def test_invalid_format_raises_value_error(self, text):
        """正規表現にも数字にも合わない表記は ``ValueError``（=``int()`` 由来）。

        「第二営業日」は漢数字なので正規表現不一致 → ``int()`` で失敗。
        「2営業日」は「第」接頭辞が無いので正規表現不一致。
        「第2」は「営業日」サフィックスが無いので正規表現不一致。
        「来月」は数字でも正規表現でもないので ``int()`` で失敗。
        """
        rule = _rule(raw_day_of_month=text)
        with pytest.raises(ValueError):
            _ = rule.day_of_month


class TestNthBusinessDayIsDue:
    """``is_due()`` の「第N営業日」分岐をテスト専用カレンダーで確認する。"""

    @staticmethod
    def _monthly_rule(date_marker: str) -> ScheduleRule:
        return _rule(
            frequency="毎月",
            raw_weekday="",
            start_time=None,
            raw_day_of_month=date_marker,
        )

    def test_matches_on_calculated_nth_business_day(self):
        """「第2営業日」の設定で、テスト専用カレンダーの「第2営業日」日付にだけ ``True``。

        国民の祝日が 1/1（木）だけ存在するテスト用カレンダーを差し込み、
        1月の第2営業日が ``rule.is_due()`` で True になることを確認する。
        """
        holidays_set = {dt.date(2026, 1, 1)}  # 1/1（木）だけ国民の祝日扱い
        with _HolidaysScope(holidays_set):
            second_business_day = nth_workday(dt.date(2026, 1, 1), 2)
            rule = self._monthly_rule("第2営業日")
            on_target = dt.datetime.combine(second_business_day, dt.time(12, 0))
            assert rule.is_due(on_target) is True
            day_before = dt.datetime.combine(
                second_business_day - dt.timedelta(days=1), dt.time(12, 0)
            )
            assert rule.is_due(day_before) is False
            day_after = dt.datetime.combine(
                second_business_day + dt.timedelta(days=1), dt.time(12, 0)
            )
            assert rule.is_due(day_after) is False

    def test_exceeding_business_days_returns_false_silently(self):
        """「第35営業日」のような月の営業日数を超える指定で ``False`` を返す。

        ``WorkdayNotFoundError`` を上位へ伝播させず、この日を「対象外」として
        扱う（``service.py`` の「1件失敗でも他は続ける」設計を守るため）。
        """
        with _HolidaysScope(set()):  # 祝日は 1 件も無い想定
            rule = self._monthly_rule("第35営業日")
            for day in range(1, 32):
                when = dt.datetime(2026, 1, day, 12, 0)  # noqa: DTZ001
                assert rule.is_due(when) is False

    def test_uses_implicit_calendar_when_no_override(self):
        """``_set_calendar_for_test`` されていないとき ``is_due`` が既定カレンダーで動く。

        正月休みの影響を受けたくないので 2 月を使う。
        計算上の第 2 営業日を ``nth_workday`` で取得して、
        その日だけ ``is_due`` が True になることを確認する。
        """
        rule = self._monthly_rule("第2営業日")
        expected_second = nth_workday(dt.date(2026, 2, 1), 2)
        on_target = dt.datetime.combine(expected_second, dt.time(12, 0))
        assert rule.is_due(on_target) is True


class TestIsDueTimeOptional:
    """「毎日」「毎週」「毎月」で ``取得開始時刻`` を空欄にすると、時刻条件なしで due 判定する。"""

    @pytest.mark.parametrize("frequency", ["毎日", "毎週", "毎月"])
    def test_blank_start_time_means_due_at_any_time(self, frequency):
        """``start_time is None`` は「時刻条件なし」と解釈する。

        同じレポートを1日のうちいつ取っても中身が変わらない（例: 前日以前の確定済み
        データ）の用途を想定。「1時間ごと」では空欄を許さないので、この挙動は適用しない。

        国民の祝日・会社休日の影響を受けない日付（4 月）を使う。
        """
        rule = _rule(frequency=frequency, start_time=None)
        assert rule.is_due(dt.datetime(2026, 4, 1, 0, 0)) is True  # noqa: DTZ001
        assert rule.is_due(dt.datetime(2026, 4, 1, 23, 59)) is True  # noqa: DTZ001

    def test_blank_start_time_still_respects_date_match(self):
        """``start_time`` が空欄でも、曜日や月の日など日付条件は引き続き適用される。"""
        rule = _rule(frequency="毎週", raw_weekday="水", start_time=None)
        # 国民の祝日・会社休日に重ならない水曜を使う（4/1 は水曜）
        monday = dt.datetime(2026, 4, 6, 12, 0)  # noqa: DTZ001
        assert rule.is_due(monday) is False
        wednesday = dt.datetime(2026, 4, 1, 12, 0)  # noqa: DTZ001
        assert rule.is_due(wednesday) is True


class TestBusinessDayFrequency:
    """「毎営業日」(`FREQUENCY_BUSINESS_DAY`) の挙動を ``is_due()`` で確かめる。

    「毎営業日」は曜日フィルタ（土日を除く）のみで、祝日の除外は
    ``holiday_policy`` の組み合わせで実現する（``HOLIDAY_SKIP`` を既定で
    組み合わせれば、土日祝日を除く真の営業日だけになる）。
    """

    @pytest.mark.parametrize(
        ("date_", "expected"),
        [
            (dt.date(2026, 3, 2), True),  # 月
            (dt.date(2026, 3, 3), True),  # 火
            (dt.date(2026, 3, 4), True),  # 水
            (dt.date(2026, 3, 5), True),  # 木
            (dt.date(2026, 3, 6), True),  # 金
            (dt.date(2026, 3, 7), False),  # 土
            (dt.date(2026, 3, 8), False),  # 日
        ],
    )
    def test_skips_weekends_and_runs_on_weekdays(self, date_, expected):
        """「毎営業日」行は土曜・日曜で False、平日で True。"""
        rule = _rule(
            frequency=FREQUENCY_BUSINESS_DAY,
            raw_weekday="",
            raw_day_of_month="",
            start_time=None,
        )
        when = dt.datetime.combine(date_, dt.time(12, 0))
        assert rule.is_due(when) is expected

    def test_respects_start_time_on_business_day(self):
        """「毎営業日」行は ``start_time`` も従来どおり適用する（既存挙動と共通）。"""
        rule = _rule(
            frequency=FREQUENCY_BUSINESS_DAY,
            raw_weekday="",
            raw_day_of_month="",
            start_time=dt.time(9, 0),
        )
        early = dt.datetime(2026, 3, 9, 8, 0)  # noqa: DTZ001
        assert rule.is_due(early) is False
        on_time = dt.datetime(2026, 3, 9, 9, 0)  # noqa: DTZ001
        assert rule.is_due(on_time) is True
        saturday = dt.datetime(2026, 1, 10, 9, 0)  # noqa: DTZ001
        assert rule.is_due(saturday) is False

    def test_holiday_skip_combined_with_business_day_excludes_holidays(self):
        """「毎営業日」+「取得しない」で土日祝日を除く真の営業日だけになる。

        国民の祝日が月曜だけあるテスト用カレンダーを差し込み、
        「毎営業日」+「取得しない」の行がその月曜で False を返すことを確認する。
        """
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)だけ国民の祝日
            rule = _rule(
                frequency=FREQUENCY_BUSINESS_DAY,
                raw_weekday="",
                raw_day_of_month="",
                start_time=None,
                holiday_policy=HOLIDAY_SKIP,
            )
            holiday_monday = dt.datetime(2026, 3, 9, 12, 0)  # noqa: DTZ001
            assert rule.is_due(holiday_monday) is False
            other_monday = dt.datetime(2026, 3, 16, 12, 0)  # noqa: DTZ001
            assert rule.is_due(other_monday) is True


class TestDailyFrequencyRegression:
    """「毎日」(`FREQUENCY_DAILY`) が土日でも True のまま（既存挙動）を回帰確認する。

    「毎営業日」を新設した影響が「毎日」に漏れていないことを保証するための、
    1 回限りのスモークテスト。国民の祝日・会社休日に重ならない 4 月の日付を使う。
    """

    @pytest.mark.parametrize(
        "date_",
        [
            dt.date(2026, 4, 11),  # 土
            dt.date(2026, 4, 12),  # 日
            dt.date(2026, 4, 13),  # 月
        ],
    )
    def test_daily_returns_true_on_weekends(self, date_):
        """「毎日」は土日でも平日でも True。"""
        rule = _rule(
            frequency=FREQUENCY_DAILY,
            raw_weekday="",
            raw_day_of_month="",
            start_time=None,
        )
        when = dt.datetime.combine(date_, dt.time(12, 0))
        assert rule.is_due(when) is True


class TestHolidayPolicySkipFetch:
    """``HOLIDAY_SKIP`` / ``HOLIDAY_FETCH`` の既存挙動が変わっていないこと（回帰確認）。

    「1営業日前」「1営業日後」は ``TestHolidayPolicyShifted`` で別クラスにまとめる。
    祝日判定は ``comken.core.holidays.is_holiday`` を直接使う（=既定カレンダー）。
    """

    def test_skip_does_not_match_on_holiday(self):
        """「取得しない」行で、対象日が国民の祝日なら ``False``。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)だけ国民の祝日
            rule = _rule(frequency="毎週", raw_weekday="月", holiday_policy=HOLIDAY_SKIP)
            when = dt.datetime(2026, 3, 9, 12, 0)  # noqa: DTZ001
            assert rule.is_due(when) is False

    def test_fetch_matches_even_on_holiday(self):
        """「取得する」行は祝日でもそのまま取得する。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)だけ国民の祝日
            rule = _rule(frequency="毎週", raw_weekday="月", holiday_policy=HOLIDAY_FETCH)
            when = dt.datetime(2026, 3, 9, 12, 0)  # noqa: DTZ001
            assert rule.is_due(when) is True

    def test_skip_does_not_match_on_non_weekday_even_when_not_holiday(self):
        """曜日条件を満たさない日は祝日でなくても False（既存挙動）。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):
            rule = _rule(frequency="毎週", raw_weekday="月", holiday_policy=HOLIDAY_SKIP)
            when = dt.datetime(2026, 3, 11, 12, 0)  # noqa: DTZ001
            assert rule.is_due(when) is False


class TestHolidayPolicyShifted:
    """「1営業日前」「1営業日後」の探索ロジック。

    単発の祝日、複数連続の祝日、対象日が土日と重なるケース、
    既存挙動（HOLIDAY_SKIP/FETCH）が破壊されていないことを確かめる。
    """

    def _weekly_rule(self, weekday_name: str, holiday_policy: str) -> ScheduleRule:
        return _rule(
            frequency="毎週",
            raw_weekday=weekday_name,
            holiday_policy=holiday_policy,
            start_time=None,
        )

    def test_shifted_before_single_monday_holiday(self):
        """「1営業日前」: 月曜が祝日 → 前の金曜が True、月曜自身は False。

        国民の祝日が月曜だけあるテスト用カレンダーを差し込み、
        「1営業日前」行の前金曜が True になることを確認する。
        """
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)だけ国民の祝日
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 6, 12, 0)) is True  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 10, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_after_single_monday_holiday(self):
        """「1営業日後」: 月曜が祝日 → 翌火曜が True、月曜自身は False。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)だけ国民の祝日
            rule = self._weekly_rule("月", HOLIDAY_AFTER)

            assert (
                rule.is_due(dt.datetime(2026, 3, 10, 12, 0)) is True  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 6, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_before_consecutive_monday_tuesday_holidays(self):
        """「1営業日前」: 月・火が祝日 → 前の金曜が True、火曜は False。"""
        with _HolidaysScope({dt.date(2026, 3, 9), dt.date(2026, 3, 10)}):
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 6, 12, 0)) is True  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 10, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 11, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_after_only_one_of_consecutive_holidays_triggers(self):
        """「1営業日後」: 月・火が祝日で対象日が月曜 → 水曜が True、火曜が False。"""
        with _HolidaysScope({dt.date(2026, 3, 9), dt.date(2026, 3, 10)}):
            rule = self._weekly_rule("月", HOLIDAY_AFTER)

            assert (
                rule.is_due(dt.datetime(2026, 3, 11, 12, 0)) is True  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 10, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_before_target_date_itself_is_never_true(self):
        """「1営業日前」: 対象日が祝日でも非祝日でも、対象日自体は常に False。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 16, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_non_business_day_target_is_false(self):
        """「1営業日前/後」: ``date`` 自身が非営業日（土日）なら False。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 7, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 8, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_before_does_not_trigger_when_no_holiday_target_between(self):
        """「1営業日前」: 間に祝日対象日がなければ False（翌営業日に直接ぶつかる）。"""
        with _HolidaysScope(set()):  # 祝日は無い
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 10, 12, 0)) is False  # noqa: DTZ001
            )

    def test_shifted_before_with_three_consecutive_holidays(self):
        """「1営業日前」: 月・火・水と3日連続の祝日 → 探索区間に複数の祝日があっても True。"""
        with _HolidaysScope(
            {
                dt.date(2026, 3, 9),
                dt.date(2026, 3, 10),
                dt.date(2026, 3, 11),
            }
        ):
            rule = self._weekly_rule("月", HOLIDAY_BEFORE)

            assert (
                rule.is_due(dt.datetime(2026, 3, 6, 12, 0)) is True  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 9, 12, 0)) is False  # noqa: DTZ001
            )
            assert (
                rule.is_due(dt.datetime(2026, 3, 12, 12, 0)) is False  # noqa: DTZ001
            )


class TestBusinessDayHelpers:
    """``is_workday`` / ``is_holiday`` がテスト用カレンダーで動くことの確認。

    国民の祝日を直接コントロールして、営業日判定が期待通り変わることを確かめる。
    """

    def test_is_workday_respects_test_calendar(self):
        """``is_workday`` がテスト専用カレンダーを反映する。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):  # 1/5(月)を国民の祝日に
            assert is_workday(dt.date(2026, 3, 9)) is False
            assert is_holiday(dt.date(2026, 3, 9)) is True
            # 翌月曜は国民の祝日ではないので通常通り
            assert is_workday(dt.date(2026, 3, 16)) is True
            assert is_holiday(dt.date(2026, 3, 16)) is False

    def test_implicit_calendar_resets_after_scope(self):
        """``_HolidaysScope`` を抜けると既定カレンダーに戻る。"""
        with _HolidaysScope({dt.date(2026, 3, 9)}):
            pass
        # スコープ外では 1/5 は会社の年末年始休暇に含まれないので国民の祝日でも
        # 会社休日でもない（=False）
        assert is_holiday(dt.date(2026, 3, 9)) is False

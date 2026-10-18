"""src/sheets/schedule.py — スケジュール列と取得時刻の判定。

**このファイルは「スケジュール」シートに何があるか（`ScheduleRule`）と、その値を使った
判定ロジックを持つ**。Excel を読む・雛形を作る仕組みは `src.report_master` にある。
``load_schedule()`` はパスを引数で受け取る（``src.sheets.group_settings`` と同じ設計）。
"""

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from comken.core.dates import (
    is_holiday,
    is_workday,
    month_end,
    non_workdays_after,
    non_workdays_before,
    nth_workday,
)
from comken.core.timer import measure
from comken.exceptions import (
    DownloaderError,
    SheetNotFoundError,
    WorkdayNotFoundError,
)

from src.report_master import MasterRow, column

FREQUENCY_DAILY = "毎日"
FREQUENCY_WEEKLY = "毎週"
FREQUENCY_MONTHLY = "毎月"
FREQUENCY_BUSINESS_DAY = "毎営業日"
HOLIDAY_SKIP = "取得しない"
HOLIDAY_FETCH = "取得する"
HOLIDAY_BEFORE = "1営業日前"
HOLIDAY_AFTER = "1営業日後"
WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")

logger = logging.getLogger(__name__)

# 「第N営業日」表記（N は 1 以上の整数）。「曜日付き」などの表記は受け付けない
_NTH_BUSINESS_DAY_PATTERN = re.compile(r"^第(\d+)営業日$")

# レポート管理表と同じブック内のスケジュールシート名。このシートが無い管理表は空とみなす
# （``load_schedule()`` 参照）
SCHEDULE_SHEET_NAME = "スケジュール"


@dataclass(frozen=True, kw_only=True)
class ScheduleRule(MasterRow):
    """「スケジュール」シートの1行。1行 = 1つの取得ルール。

    列定義は `column()` に集約されている。``曜日`` / ``日付`` 列は自由記述（空欄を許す）
    なので、``weekday`` / ``day_of_month`` / ``month_end`` / ``nth_business_day`` の
    `@property` でパース結果だけを公開する。

    Attributes:
        schedule_key: 列「スケジュールキー」。このルールを一意に識別するキー。
            履歴の「スケジュールキー」列に記録され、同じ行を同日に何度も
            実行しないための dedup 判定にも使う。
        report_key: 列「レポートキー」。対象のレポートの管理番号
            （レポート管理表シートの ID と対応する）。
        frequency: 列「取得頻度」。`FREQUENCY_DAILY` / `FREQUENCY_WEEKLY` /
            `FREQUENCY_MONTHLY` / `FREQUENCY_BUSINESS_DAY` のいずれか。
        start_time: 列「取得開始時刻」。毎日・毎週・毎月・毎営業日の実行開始時刻
            （この時刻を過ぎたら取得してよい）。空欄可。
        desired_time: 列「取得時刻」。このレポートが何時までに欲しいかの目安
            （記録用）。判定には使わない。
        raw_weekday: 列「曜日」。`frequency` が毎週のときだけ使う
            （下の `weekday` property で 0=月〜6=日 に変換）。
        raw_day_of_month: 列「日付」。`frequency` が毎月のときだけ使う
            （1〜31 の数字 / `月末` / `第N営業日` のいずれかを下の
            `day_of_month` / `month_end` / `nth_business_day` property で
            分解する）。
        holiday_policy: 列「祝日対応」。`HOLIDAY_SKIP`（既定、祝日はスキップ）/
            `HOLIDAY_FETCH`（曜日/日付/月末/第N営業日が祝日でも取得）/
            `HOLIDAY_BEFORE`（対象日が祝日なら前営業日へ前倒し）/
            `HOLIDAY_AFTER`（対象日が祝日なら翌営業日へ繰り越し）の 4 値から選ぶ。
        enabled: 列「有効」。`○`/`×`。既定値なし（書き忘れはエラー）。
    """

    SHEET_NAME = SCHEDULE_SHEET_NAME

    schedule_key: str = column(
        "スケジュールキー",
        unique=True,
        help="このルールを一意に識別するキー。履歴の「スケジュールキー」列に記録され、"
        "同じスケジュール行を同日に何度も実行しない dedup 判定に使います",
    )
    report_key: str = column(
        "レポートキー",
        help="対象のレポートの管理番号（レポート管理表シートの ID と対応させる）",
    )
    frequency: str = column(
        "取得頻度",
        choices=(FREQUENCY_DAILY, FREQUENCY_WEEKLY, FREQUENCY_MONTHLY, FREQUENCY_BUSINESS_DAY),
        help="毎日 / 毎週 / 毎月 / 毎営業日 のいずれか。「毎営業日」は土日を除く平日のみ実行します",
    )
    start_time: dt.time | None = column(
        "取得開始時刻",
        default=None,
        help="この時刻を過ぎたら取得してよい開始時刻。"
        "「毎日」「毎週」「毎月」「毎営業日」のすべてに共通。空欄可",
    )
    desired_time: dt.time | None = column(
        "取得時刻",
        default=None,
        help="このレポートが何時までに欲しいかの目安（記録用）。"
        "取得の判定には使いません（判定に使うのは「取得開始時刻」）。空欄可",
    )
    # `choices` で読み込み時に「月〜日」に絞り込まれる（ドロップダウンは `column_specs()` 経由で
    # `apply_schedule_dropdowns()` が付与する）
    raw_weekday: str = column(
        "曜日",
        default="",
        choices=WEEKDAY_NAMES,
        help="frequency が「毎週」のときだけ選ぶ。月〜日のいずれか。空欄可",
    )
    raw_day_of_month: str = column(
        "日付",
        default="",
        help="frequency が「毎月」のときだけ書く。"
        "1〜31 の数字 / 「月末」 / 「第N営業日」（N は 1 以上の整数）のいずれか。"
        "空欄可",
    )
    holiday_policy: str = column(
        "祝日対応",
        default=HOLIDAY_SKIP,
        choices=(HOLIDAY_SKIP, HOLIDAY_FETCH, HOLIDAY_BEFORE, HOLIDAY_AFTER),
        help="祝日の扱いを「取得しない」「取得する」「1営業日前」「1営業日後」の"
        "4 値から選びます。「1営業日前」「1営業日後」は、対象日が祝日のときに"
        "代わりに取得する日（前営業日/翌営業日）を表します",
    )
    # 既定値を持たせない（書き忘れを「有効」と区別するため）
    enabled: bool = column(
        "有効",
        choices=("○", "×"),
        help="「○」か「×」と書いてください",
    )

    @property
    def weekday(self) -> int | None:
        """「曜日」列の値を 0=月〜6=日 の整数に変換する。空欄は None。

        読み込み時は ``choices=WEEKDAY_NAMES`` で絞り込まれるため、想定外の表記は
        ここに来る前に ``MasterRowValueError`` で弾かれる。``DownloaderError`` は
        Python から直接 ``ScheduleRule`` を組み立てたときの受け皿。

        Raises:
            DownloaderError: 想定外の文字列が書かれている場合。
        """
        if not self.raw_weekday:
            return None
        text = self.raw_weekday.strip()
        if text not in WEEKDAY_NAMES:
            raise DownloaderError(
                f"曜日が正しくありません: {self.raw_weekday}\n"
                "管理表の「曜日」列の値を 月 / 火 / 水 / 木 / 金 / 土 / 日 の"
                "いずれかに修正してください（「曜日」を付ける形式でも可）。"
                "\n対処: 管理表の「取得頻度」列を 毎日 / 毎週 / 毎月 / 毎営業日 の"
                "いずれかに、「曜日」列を月〜日のいずれかに修正してください"
                "（「曜日」接尾辞付きも可）。"
            )
        return WEEKDAY_NAMES.index(text)

    @property
    def day_of_month(self) -> int | None:
        """「日付」列が 1〜31 の数字で書かれたとき、その値。"""
        _, value, _ = self._parsed_day_of_month
        return value

    @property
    def month_end(self) -> bool:
        """「日付」列が「月末」のとき True。"""
        value, _, _ = self._parsed_day_of_month
        return value

    @property
    def nth_business_day(self) -> int | None:
        """「日付」列が「第N営業日」のとき、N。"""
        _, _, value = self._parsed_day_of_month
        return value

    def validate(self) -> tuple[str, str] | None:
        """行ごとの追加検証。頻度と「曜日」「日付」の組み合わせをここで検査する。

        列単体の ``choices`` では、「毎週なのに曜日が空」「毎週以外で曜日が書かれている」
        「毎月なのに日付が空」のような組み合わせは弾けない。``MasterRowValueError``
        （行番号・列名付き）に変換されるので、業務担当者は直す場所がメッセージで分かる。
        「日付」列の解釈不能値もここで検出する。

        Returns:
            問題がなければ ``None``。問題があれば ``(Excel の見出し, 直し方のメッセージ)``。
        """
        weekday_header = self.header("raw_weekday")
        day_header = self.header("raw_day_of_month")

        # 「日付」列の解釈チェック。想定外の値（例: 「来月」）はここで捕まえる
        if self.raw_day_of_month:
            try:
                _parse_day_of_month(self.raw_day_of_month)
            except ValueError:
                return (
                    day_header,
                    "「日付」は 1〜31 の数字、「月末」、または"
                    f"「第N営業日（N は 1 以上の整数）」のいずれかで書いてください"
                    f"（入力: {self.raw_day_of_month!r}）",
                )

        # 「取得頻度」と「曜日」「日付」の組み合わせ
        if self.frequency == FREQUENCY_WEEKLY:
            if not self.raw_weekday:
                return (
                    weekday_header,
                    "「取得頻度」が「毎週」のときは「曜日」を指定してください。"
                    "空欄だと毎回実行されるため、毎日取りに行く事故になります。",
                )
        elif self.raw_weekday:
            # 毎週以外で曜日が書かれている → 黙って曜日フィルタが効いていた事故を防ぐ
            return (
                weekday_header,
                f"「取得頻度」が「{self.frequency}」のときは「曜日」を指定できません。"
                "「曜日」は「毎週」のときだけ使います。空欄にしてください。",
            )

        if self.frequency == FREQUENCY_MONTHLY:
            if not self.raw_day_of_month:
                return (
                    day_header,
                    "「取得頻度」が「毎月」のときは「日付」を指定してください。"
                    "空欄だと毎回実行されるため、毎日取りに行く事故になります。",
                )
        elif self.raw_day_of_month:
            return (
                day_header,
                f"「取得頻度」が「{self.frequency}」のときは「日付」を指定できません。"
                "「日付」は「毎月」のときだけ使います。空欄にしてください。",
            )
        return None

    @property
    def _parsed_day_of_month(self) -> tuple[bool, int | None, int | None]:
        """「日付」列を ``(month_end, day_of_month, nth_business_day)`` に分解する。

        空欄は「指定なし」、数字 1〜31 は `day_of_month`、「月末」は `month_end=True`、
        `"第N営業日"` は `nth_business_day=N`。想定外の値は ``ValueError``。
        """
        return _parse_day_of_month(self.raw_day_of_month)

    def is_due(
        self,
        now: dt.datetime,
    ) -> bool:
        """指定時刻にこのスケジュールを実行すべきか判定する。

        祝日判定は ``comken.core.dates`` の ``is_holiday`` / ``is_workday`` /
        ``nth_workday`` を直接使う（既定の統一カレンダー 1 本だけが対象）。

        ``start_time is None`` のときは「時刻条件なし」で、日付条件が合えば常に ``True``
        （前日以前の確定済みデータのように、いつ取っても同じレポート用）。

        「毎営業日」は曜日フィルタ（土日を除く）のみで、祝日の除外は ``holiday_policy`` との
        組み合わせで実現する（「毎営業日」+「取得しない」で土日祝日を除く）。
        """
        if not self.enabled or not self._date_matches(now.date()):
            return False
        if self.frequency in {
            FREQUENCY_DAILY,
            FREQUENCY_WEEKLY,
            FREQUENCY_MONTHLY,
            FREQUENCY_BUSINESS_DAY,
        }:
            return self.start_time is None or now.time() >= self.start_time
        raise DownloaderError(
            f"対応していない取得頻度です: {self.frequency}\n"
            "管理表の「取得頻度」列の値を 毎日 / 毎週 / 毎月 / 毎営業日 の"
            "いずれかに修正してください。"
            "\n対処: 管理表の「取得頻度」列を 毎日 / 毎週 / 毎月 / 毎営業日 の"
            "いずれかに、「曜日」列を月〜日のいずれかに修正してください"
            "（「曜日」接尾辞付きも可）。"
        )

    def _raw_date_matches(
        self,
        date: dt.date,
    ) -> bool:
        """祝日を考慮せず、``曜日`` / ``日付`` / ``月末`` / ``第N営業日`` の
        条件だけで ``date`` が生の対象日として一致するかを返す。

        「1営業日前」「1営業日後」の判定で「対象日条件を満たす祝日」を探すヘルパー。
        「月の営業日数を超える」設定ミス（``WorkdayNotFoundError``）は、
        ``download_scheduled`` 全体を止めないよう、この日は対象外として ``False``。
        """
        if self.weekday is not None and date.weekday() != self.weekday:
            return False
        if self.frequency == FREQUENCY_BUSINESS_DAY and date.weekday() >= 5:
            # 土日は対象外。祝日の除外は `holiday_policy` 側
            return False
        if self.day_of_month is not None and date.day != self.day_of_month:
            return False
        if self.nth_business_day is not None:
            try:
                target = nth_workday(date.replace(day=1), self.nth_business_day)
            except WorkdayNotFoundError:
                logger.warning(
                    "スケジュール %s の「第%d営業日」指定が %s年%s月の営業日数を"
                    "超えています。この日は対象外として扱います。",
                    self.schedule_key,
                    self.nth_business_day,
                    date.year,
                    date.month,
                )
                return False
            if date != target:
                return False
        return not self.month_end or date == month_end(date)

    def _date_matches(
        self,
        date: dt.date,
    ) -> bool:
        """``date`` がこのスケジュールの「取得日」に当たるかを返す。

        判定は 2 段で行う:

        1. ``_raw_date_matches()`` で「曜日/日付/月末/第N営業日」の条件だけで
           一致するかを見る。一致する場合、``holiday_policy`` に応じて:
           - ``HOLIDAY_SKIP``（既定）:  ``date`` が祝日（国民の祝日＋会社休日）なら
             ``False``
           - ``HOLIDAY_FETCH``: ``True``
           - ``HOLIDAY_BEFORE`` / ``HOLIDAY_AFTER``: ``False``（対象日自体では取得
             せず、前後営業日への前倒し/繰り越し先に判定を委ねる）

        2. 生の対象日条件を満たさない場合、``HOLIDAY_BEFORE`` / ``HOLIDAY_AFTER``
           のときだけ「直近の祝日である対象日」からちょうど 1 営業日ぶん前/後の
           営業日かを ``_search_shifted_target()`` で確認する。``date`` 自身が
           非営業日なら即 ``False``（ずらし先になり得ないため）。
        """
        if self._raw_date_matches(date):
            if self.holiday_policy == HOLIDAY_SKIP and is_holiday(date):
                return False
            # BEFORE/AFTER は前後の営業日へ判定を委ねる。SKIP/FETCH は祝日判定済み
            return self.holiday_policy not in (HOLIDAY_BEFORE, HOLIDAY_AFTER)
        if self.holiday_policy not in (HOLIDAY_BEFORE, HOLIDAY_AFTER):
            return False
        if not is_workday(date):
            return False
        direction = "before" if self.holiday_policy == HOLIDAY_BEFORE else "after"
        return self._search_shifted_target(date, direction)

    def _search_shifted_target(
        self,
        date: dt.date,
        direction: str,
    ) -> bool:
        """``date`` が祝日に当たった対象日のちょうど 1 営業日ぶん前/後かを判定する。

        ``direction="before"`` のときは ``date`` の翌日から次の営業日に達するまで、
        ``"after"`` のときは前日から前の営業日に達するまでの非営業日区間（``date`` 自身を含まない）
        を見て、**その区間に祝日である対象日が 1 つでも含まれていれば ``True``**。
        区間は ``comken.core.dates`` の ``non_workdays_after`` / ``non_workdays_before`` が返す。
        ``date`` 自身が非営業日の場合は呼び出し元（``_date_matches``）で先に弾く。
        """
        holidays_in_a_row = (
            non_workdays_after(date) if direction == "before" else non_workdays_before(date)
        )
        return any(self._raw_date_matches(day) and is_holiday(day) for day in holidays_in_a_row)


def _parse_day_of_month(value: object) -> tuple[bool, int | None, int | None]:
    """「日付」列を ``(month_end, day_of_month, nth_business_day)`` に分解する。

    空欄は「指定なし」、数字 1〜31 は ``day_of_month``、「月末」は ``month_end=True``、
    ``"第N営業日"`` は ``nth_business_day=N``。想定外の値（例: ``"来月"``）は
    ``ValueError`` を投げる。

    Returns:
        3 要素のタプル。常にどれか 1 つだけが立ち、残りは「指定なし」になる。
    """
    if value in (None, ""):
        return False, None, None
    text = str(value).strip()
    if text == "月末":
        return True, None, None
    match = _NTH_BUSINESS_DAY_PATTERN.match(text)
    if match:
        return False, None, int(match.group(1))
    if not text.isdigit():
        raise ValueError(f"「日付」列の値を解釈できません: {value!r}")
    return False, int(text), None


@measure
def load_schedule(path: str | Path) -> list[ScheduleRule]:
    """スケジュール管理シートを読んで、``ScheduleRule`` のリストを返す。

    **シートが存在しない場合はエラーにせず空リストを返す**（「スケジュール」シートを
    まだ追加していない既存の管理表を壊さないため）。

    重複や必須列の空は ``MasterDuplicateValueError`` / ``MasterRowValueError``
    （行番号・列名・値入り）になる。存在しない ``レポートキー`` を指す行はここではエラーに
    しない（管理表との突き合わせは ``download_scheduled()`` の責務）。

    Args:
        path: 管理表（Excel）のパス。レポート管理表と同じブック内の
            「スケジュール」シートを読むので、管理表のパスをそのまま渡す。

    Returns:
        宣言順に並んだ ``ScheduleRule`` のリスト。

    Raises:
        MasterTableError: 宣言した見出しが表に無い場合。
        MasterRowValueError: 値が型・選択肢に合わない、または空にできない列が空の場合。
        MasterDuplicateValueError: スケジュールキーが重複している行がある場合。
        ComkenFileNotFoundError: ``path`` が存在しない場合。
    """
    source = Path(path)
    # シートが無い場合だけ空リスト。ファイル自体のエラーはそのまま伝える
    try:
        return ScheduleRule.load(source)
    except SheetNotFoundError:
        return []


__all__ = [
    "ScheduleRule",
    "SCHEDULE_SHEET_NAME",
    "load_schedule",
]

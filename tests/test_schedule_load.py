"""``src.sheets.schedule.load_schedule`` を検証する。

`ScheduleRule` の ``raw_*`` パースとプロパティ挙動は ``tests/test_schedule.py``
で担保する（既存テストを壊さない）。ここでは**「Excel から読んで ScheduleRule
のリストにする」**経路と、エラー時の挙動を確かめる。
"""

from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import (
    ComkenFileNotFoundError,
)
from comken.toolbox.excel import Excel

from src.exceptions import (
    MasterTableError,
)
from src.sheets.schedule import (
    SCHEDULE_SHEET_NAME,
    load_schedule,
)

# 新スキーマ: 「取得間隔（分）」列は廃止、「日付」列を `曜日` と `祝日対応` の間に
# 追加。順序はこのとおり（Excel の列順は自由だが、テストではこの順で作る）
SCHEDULE_HEADERS = [
    "スケジュールキー",
    "レポートキー",
    "取得頻度",
    "取得開始時刻",
    "取得時刻",
    "曜日",
    "日付",
    "祝日対応",
    "有効",
]


def make_master_with_schedule(
    path: Path,
    master_rows: list[list],
    schedule_rows: list[list] | None,
) -> Path:
    """レポート管理表と「スケジュール」シートを含むブックを作る。

    ``schedule_rows=None`` のときはスケジュールシート自体を作らない
    （後方互換ケース用）。
    """
    master_headers = [
        "ID",
        "グループ",
        "担当者",
        "概要",
        "Salesforce URL",
        "有効",
        "備考",
    ]
    master_table_rows = [dict(zip(master_headers, row, strict=True)) for row in master_rows]
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table(
            "管理表", Table(master_headers, master_table_rows)
        )
        if schedule_rows is not None:
            schedule_table_rows = [
                dict(zip(SCHEDULE_HEADERS, row, strict=True)) for row in schedule_rows
            ]
            book.create_data_sheet(SCHEDULE_SHEET_NAME).create_table(
                SCHEDULE_SHEET_NAME, Table(SCHEDULE_HEADERS, schedule_table_rows)
            )
    return path


class TestLoadSchedule:
    """Excel から ScheduleRule へ変換する経路の挙動。"""

    def test_reads_normal_rows_into_schedule_rules(self, tmp_path):
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                # 管理表本体は load_schedule() の検証対象ではないので空でもよいが、
                # 実際の運用を再現するため1行入れておく
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ],
            ],
            schedule_rows=[
                # スケジュールキー / レポートキー / 取得頻度 / 取得開始時刻 /
                # 取得時刻 / 曜日 / 日付 / 祝日対応 / 有効 の9列で書く
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                ["S002", "1002", "毎日", "10:30", "", "", "", "取得しない", "○"],
            ],
        )
        rules = load_schedule(master)
        assert [rule.schedule_key for rule in rules] == ["S001", "S002"]
        assert rules[0].weekday == 0  # 月曜
        assert rules[1].start_time is not None
        assert rules[1].start_time.hour == 10

    def test_blank_rows_are_skipped(self, tmp_path):
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                [None] * len(SCHEDULE_HEADERS),  # 空行は読み飛ばす
                ["S002", "1002", "毎日", "10:30", "", "", "", "取得しない", "○"],
            ],
        )
        rules = load_schedule(master)
        assert [rule.schedule_key for rule in rules] == ["S001", "S002"]

    def test_missing_schedule_sheet_returns_empty_list(self, tmp_path):
        """「スケジュール」シートが無い管理表はエラーにせず空リストを返す（後方互換）。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            # スケジュールシート自体を作らない
            schedule_rows=None,
        )
        assert load_schedule(master) == []

    def test_duplicate_schedule_key_raises(self, tmp_path):
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                ["S001", "1002", "毎週", "10:00", "", "火", "", "取得しない", "○"],  # 重複
            ],
        )
        with pytest.raises(MasterTableError) as e:
            load_schedule(master)
        # 業務担当者に「どの値が」「どの見出しで」重複したかが届く
        assert "スケジュールキー" in str(e.value)
        assert "S001" in str(e.value)

    def test_missing_required_value_raises_with_row_number(self, tmp_path):
        """必須列（スケジュールキー）が空のとき、行番号付き ``MasterTableError`` で抜ける。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                [
                    "",
                    "1001",
                    "毎週",
                    "09:00",
                    "",
                    "月",
                    "",
                    "取得しない",
                    "○",
                ],  # スケジュールキー空
            ],
        )
        with pytest.raises(MasterTableError) as e:
            load_schedule(master)
        # 見出しの次の行（offset=0, row_number=2）が指摘される
        assert "2 行目" in str(e.value)
        assert "スケジュールキー" in str(e.value)

    def test_invalid_frequency_raises(self, tmp_path):
        """choices に無い取得頻度は ``MasterTableError``（=許可された選択肢が並ぶ）。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                ["S001", "1001", "ときどき", "09:00", "", "", "", "取得しない", "○"],
            ],
        )
        with pytest.raises(MasterTableError) as e:
            load_schedule(master)
        assert "取得頻度" in str(e.value)

    def test_blank_enabled_raises(self, tmp_path):
        """「有効」列は既定値なし（書き忘れはエラー）に統一したため、空欄で止まる。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", ""],
            ],
        )
        with pytest.raises(MasterTableError) as e:
            load_schedule(master)
        assert "有効" in str(e.value)

    def test_day_of_month_is_parsed(self, tmp_path):
        """「日付」列の数字が ``day_of_month`` プロパティで取り出せる。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                ["S001", "1001", "毎月", "06:00", "", "", "15", "取得しない", "○"],
            ],
        )
        rules = load_schedule(master)
        assert rules[0].day_of_month == 15
        assert rules[0].month_end is False

    def test_missing_master_file_raises(self, tmp_path):
        """シート無しと「ファイル自体が無い」は別のエラー（後者はそのまま上位へ）。"""
        missing = tmp_path / "無い.xlsx"
        assert not missing.exists()
        with pytest.raises(ComkenFileNotFoundError):
            load_schedule(missing)


class TestScheduleValidation:
    """``ScheduleRule.validate()`` は「取得頻度」と「曜日」「日付」の組み合わせと、
    「日付」列の解釈不能値を読み込み時に ``MasterTableError`` で止める。"""

    @staticmethod
    def _row(**overrides: str) -> list[str]:
        base: list[str] = [
            "S001",
            "1001",
            "毎週",
            "09:00",
            "",
            "月",
            "",
            "取得しない",
            "○",
        ]
        columns = [
            "スケジュールキー",
            "レポートキー",
            "取得頻度",
            "取得開始時刻",
            "取得時刻",
            "曜日",
            "日付",
            "祝日対応",
            "有効",
        ]
        for key, value in overrides.items():
            base[columns.index(key)] = value
        return base

    def test_weekly_without_weekday_raises_with_row_and_column(self, tmp_path):
        """「毎週」なのに「曜日」が空 → 行番号・列名付きでエラー。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                self._row(曜日=""),  # 「毎週」なのに曜日が空
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "「曜日」" in message  # 列名が「曜日」になっている（業務担当者向け）
        assert "正しくありません" in message  # 「◯行目の「◯」が正しくありません」形式
        assert "(未指定)" not in message  # 列名が取れていない旧バグの形ではない
        # 列名位置にタプルの repr（`('`, `, '` など）が混入していないこと
        after_row = message.split("行目", 1)[1]
        column_label = after_row.split("」", 1)[0]
        assert "(" not in column_label

    def test_monthly_without_day_of_month_raises_with_row_and_column(self, tmp_path):
        """「毎月」なのに「日付」が空 → 行番号・列名付きでエラー。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                # 「毎月」だが「曜日」と「日付」の両方が空。「曜日」の方が
                # 先に検証で止まらないよう、ここでは「曜日」も明示的に空にする
                self._row(取得頻度="毎月", 曜日="", 日付=""),
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "「日付」" in message  # 列名が「日付」になっている
        assert "正しくありません" in message
        assert "(未指定)" not in message
        assert "(" not in message.split("行目", 1)[1].split("」", 1)[0]

    def test_non_weekly_with_weekday_raises(self, tmp_path):
        """「毎週」以外で「曜日」が埋まっている → 「毎週」のときだけ、と案内して止める。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                self._row(取得頻度="毎日", 曜日="月"),  # 「毎日」だが曜日が書かれている
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "「曜日」" in message
        assert "毎週" in message
        assert "(未指定)" not in message
        assert "(" not in message.split("行目", 1)[1].split("」", 1)[0]

    def test_non_monthly_with_day_of_month_raises(self, tmp_path):
        """「毎月」以外で「日付」が埋まっている → 「毎月」のときだけ、と案内して止める。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                self._row(取得頻度="毎週", 日付="15"),
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "「日付」" in message
        assert "毎月" in message
        assert "(未指定)" not in message
        assert "(" not in message.split("行目", 1)[1].split("」", 1)[0]

    def test_invalid_day_of_month_value_raises_with_row_and_column(self, tmp_path):
        """「日付」列の解釈不能値（例: 「来月」）も読み込み時にエラー。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                self._row(取得頻度="毎月", 日付="来月"),
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "「日付」" in message
        assert "来月" in message
        assert "(未指定)" not in message
        assert "(" not in message.split("行目", 1)[1].split("」", 1)[0]

    def test_invalid_time_value_raises_with_row_and_column(self, tmp_path):
        """時刻の不正値（範囲外）も読み込み時に ``MasterTableError`` で行番号・列名付き。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                self._row(取得開始時刻="25:00"),
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "取得開始時刻" in message
        assert "(未指定)" not in message

    def test_reports_all_invalid_rows_before_stopping(self, tmp_path):
        """1行目で止まらず、以降の行も順次チェックされる（例: 2行目で別の違反）。"""
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "営業事務グループ",
                    "山田",
                    "顧客一覧",
                    "https://example.com/a/view",
                    "○",
                    "",
                ]
            ],
            schedule_rows=[
                # 行2: 「毎週」だが曜日空（最初の違反）
                self._row(スケジュールキー="S001", 曜日=""),
                # 行3: 「日付」が解釈不能（「来月」）
                self._row(
                    スケジュールキー="S002",
                    レポートキー="1001",
                    取得頻度="毎月",
                    日付="来月",
                ),
            ],
        )
        # 1行目で ``MasterTableError`` が出れば、2行目の検査には進まない。
        # 「1件目で止める」のが読み込み時の自然な挙動（業務担当者は直して再実行する）
        with pytest.raises(MasterTableError) as caught:
            load_schedule(master)
        message = str(caught.value)
        assert "2 行目" in message
        assert "曜日" in message
        assert "(未指定)" not in message
        assert "(" not in message.split("行目", 1)[1].split("」", 1)[0]

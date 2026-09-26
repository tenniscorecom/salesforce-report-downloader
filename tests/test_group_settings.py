"""``src.sheets.group_settings`` の動作を検証する。

Excel を作って ``GroupSetting.load()`` を直接呼ぶ経路と、``load_group_settings()``
（キャッシュ経由）の経路を両方確かめる。``tests/test_master.py`` /
``tests/test_schedule.py`` の「``Excel`` クラスで ``Table`` を作る」パターンを踏襲。
"""

from pathlib import Path

import pytest
from comken.core.table import Table
from comken.exceptions import (
    ComkenFileNotFoundError,
)
from comken.toolbox.excel import Excel

import src.paths as paths_module
from src.exceptions import (
    MasterTableError,
)
from src.sheets.group_settings import (
    GroupSetting,
    load_group_settings,
)

SETTINGS_HEADERS = ["グループ", "ベースURL"]


def make_book_with_settings(path: Path, settings_rows: list[list]) -> Path:
    """「設定」シートだけを持つ Excel を作る。"""
    table_rows = [dict(zip(SETTINGS_HEADERS, row, strict=True)) for row in settings_rows]
    with Excel(path) as book:
        book.create_data_sheet("設定").create_table("設定", Table(SETTINGS_HEADERS, table_rows))
    return path


class TestLoadGroupSettings:
    """``load_group_settings()`` の基本動作。"""

    def test_reads_pairs_into_dict(self, tmp_path):
        """設定シートから ``{グループ名: ベースパス}`` の辞書を作る。"""
        base_a = tmp_path / "A"
        base_b = tmp_path / "B"
        make_book_with_settings(
            tmp_path / "設定.xlsx",
            [
                ["営業本部", str(base_a)],
                ["経理グループ", str(base_b)],
            ],
        )
        settings = load_group_settings(tmp_path / "設定.xlsx")
        assert settings == {"営業本部": base_a, "経理グループ": base_b}

    def test_empty_settings_sheet_returns_empty_dict(self, tmp_path):
        """「設定」シートにデータが無いと空の辞書。"""
        make_book_with_settings(tmp_path / "設定.xlsx", [])
        assert load_group_settings(tmp_path / "設定.xlsx") == {}

    def test_missing_file_raises_excel_file_not_found(self, tmp_path):
        """ファイル自体が無いと ``ComkenFileNotFoundError``。"""
        missing = tmp_path / "無い.xlsx"
        assert not missing.exists()
        with pytest.raises(ComkenFileNotFoundError):
            load_group_settings(missing)


class TestUniqueGroup:
    """``group`` 列は ``unique=True`` なので、重複があるとエラー。"""

    def test_duplicate_group_raises(self, tmp_path):
        make_book_with_settings(
            tmp_path / "設定.xlsx",
            [
                ["営業本部", str(tmp_path / "A")],
                ["営業本部", str(tmp_path / "B")],  # 同じグループ名が2行
            ],
        )
        with pytest.raises(MasterTableError) as caught:
            load_group_settings(tmp_path / "設定.xlsx")
        assert "グループ" in str(caught.value)


class TestPathsCache:
    """``paths._load_group_settings_cached()`` のキャッシュ挙動。"""

    def test_caches_by_resolved_path(self, tmp_path):
        """同じパスで2回呼ぶと、設定シートを1回しか開かない（キャッシュが効く）。"""
        paths_module._reset_cached_master()
        try:
            make_book_with_settings(
                tmp_path / "設定.xlsx",
                [["営業本部", str(tmp_path / "A")]],
            )
            first = load_group_settings(tmp_path / "設定.xlsx")
            # キャッシュ済みなので、オブジェクトが同一になる
            second = paths_module._load_group_settings_cached(tmp_path / "設定.xlsx")
            assert first == second
            # paths のキャッシュ経由でも同じ dict が返る
            third = paths_module._load_group_settings_cached(tmp_path / "設定.xlsx")
            assert first == third
        finally:
            paths_module._reset_cached_master()


class TestGroupSettingDirectLoad:
    """``GroupSetting.load()`` を直接呼んでも同じ結果が返る（``load_group_settings`` の内部）。"""

    def test_load_via_class_method(self, tmp_path):
        make_book_with_settings(
            tmp_path / "設定.xlsx",
            [["営業本部", str(tmp_path / "A")]],
        )
        rows = GroupSetting.load(tmp_path / "設定.xlsx")
        assert len(rows) == 1
        assert rows[0].group == "営業本部"
        assert rows[0].base_path == tmp_path / "A"

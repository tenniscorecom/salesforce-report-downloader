"""Salesforce レポートの保存先パス組み立て（`output_path()`）を検証する。

`output_path()` は設計上ネットワークを使わない。取得を実行する側
（`download_scheduled()`）はこの関数が組み立てたパスにファイルを置く契約を共有する。
ここでは「取得を実行する側が置くファイル」を `_simulate_download()` で直接シミュレート
し、`output_path()` の組み立てが期待どおりであることを確かめる（実際の取得実行側の
テストは `test_service.py` などにある）。

**`cached_report` / `cached_report_path` / `_latest_today_path` は 2026-09 に
廃止された。** ダウンローダー側で「本日のキャッシュを読み取る」用途は無くなったため、
その分のテスト（`TestLatestTodayPath` / `TestCachedReport`）は削除している。

**2026-09 にフォルダ階層を「ベースパス / 概要」の 2 階層に変更した。** 人が業務ごとに
探しやすくするため ``entry.summary`` を ``summary_folder_name()`` で安全化して
フォルダ名にする。テストはこの 2 階層構造を前提に組み立て直す。

`MASTER_PATH` / `HISTORY_PATH` は `src.paths` のモジュール変数で、テストでは
`monkeypatch.setattr` で tmp_path のパスへ差し替える。``_Paths`` ラッパー経由の
``output_path`` は呼び出し時点のモジュール変数を読むので、``src.paths`` 1 か所を
patch するだけで全箇所に反映される（``service.py`` 内のローカル束縛を経由しない）。
"""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

import src.paths as paths_module
from src.exceptions import (
    GroupNotRegisteredError,
)
from src.paths import output_path, summary_folder_name
from src.sheets.group_settings import load_group_settings
from src.sheets.master import load_master

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"
URL_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000FGHIJ/view"
ROWS = [{"名前": "山田", "金額": "100"}, {"名前": "鈴木", "金額": "200"}]

HEADERS = [
    "ID",
    "グループ",
    "担当者",
    "概要",
    "Salesforce URL",
    "有効",
    "備考",
]

GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]

# paths fixture で使う管理表の概要列の値（``summary_folder_name()`` で安全化される）
SUMMARY_1001 = "顧客一覧"
SUMMARY_1002 = "売上実績"
SUMMARY_1003 = "停止中"


@pytest.fixture(autouse=True)
def reset_master_cache():
    """管理表・グループ設定のプロセス内キャッシュをテストごとに破棄する。

    `_find()` は ``Path.resolve()`` 後の絶対パスをキーに管理表をキャッシュする
    ため、 ``tmp_path`` が違うテスト同士はキーが違っていて**普通はリークしない**。
    ただしモンキーパッチで `paths.MASTER_PATH` を差し替えた直後に古いキャッシュ
    を引きずらないよう、念のため明示的に破棄する。
    """
    paths_module._reset_cached_master()
    try:
        yield
    finally:
        paths_module._reset_cached_master()


def make_master(path: Path, rows: list[list], settings_rows: list[list] | None = None) -> Path:
    """管理表（Excel）を作る。設定シートも一緒に作るかは ``settings_rows`` で切り替える。"""
    df_master = pd.DataFrame(rows, columns=HEADERS)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_master.to_excel(writer, sheet_name="PY_管理表", index=False)
        if settings_rows is not None:
            df_settings = pd.DataFrame(settings_rows, columns=GROUP_SETTINGS_HEADERS)
            df_settings.to_excel(writer, sheet_name="PY_設定", index=False)
    return path


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """管理表・履歴・保存先をまとめて用意し、共有定数へ注入する。

    `src.paths.MASTER_PATH` と、comken 側の ``HISTORY_DB_PATH``（正本の
    SQLite）／ ``HISTORY_PATH``（人が見る CSV）を tmp_path 配下の値へ
    差し替える。``output_path`` / ``_Paths`` ラッパーは呼び出し時点の
    ``src.paths`` モジュール変数を読むので、1 か所の patch ですべての参照経路
    に反映される。

    ベースパス（設定シート）だけを作り、その配下に「担当者」「概要」の
    サブフォルダは作らない（``output_path()`` が指す概要フォルダは呼び出し側
    ``service._save()`` が保存時に ``mkdir`` で作る）。
    """
    base_path = tmp_path / "ベース"
    base_path.mkdir()
    master = make_master(
        tmp_path / "レポート管理表.xlsx",
        [
            [
                "1001",
                "営業本部",
                "山田",
                SUMMARY_1001,
                URL_A,
                "○",
                "",
            ],
            [
                "1002",
                "営業本部",
                "佐藤",
                SUMMARY_1002,
                URL_B,
                "○",
                "",
            ],
            [
                "1003",
                "営業本部",
                "山田",
                SUMMARY_1003,
                URL_B,
                "×",
                "",
            ],
        ],
        settings_rows=[
            ["営業本部", str(base_path)],
        ],
    )
    history_db_path = tmp_path / "ダウンロード履歴.sqlite3"
    history_path = tmp_path / "ダウンロード履歴.csv"
    monkeypatch.setattr(paths_module, "MASTER_PATH", master)
    monkeypatch.setattr(
        "comken.services.salesforce_downloader.paths.HISTORY_DB_PATH", history_db_path
    )
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path)
    return {
        "master_path": master,
        "history_db_path": history_db_path,
        "history_path": history_path,
        "base_path": base_path,
        # 概要のフォルダ名（``summary_folder_name()`` で安全化した後の値）。
        # ``output_path()`` の戻り値の組み立てに使う
        "summary_folder_1001": summary_folder_name(SUMMARY_1001),
        "summary_folder_1002": summary_folder_name(SUMMARY_1002),
    }


class TestOutputPath:
    """``output_path()`` は唯一の出力パスを組み立てる。"""

    def test_uses_schedule_run_time_when_provided(self, paths):
        """``schedule_run_time`` を渡したら stem に ``%Y%m%d_%H%M`` で埋め込む。

        フォルダは「ベースパス / 概要」の 2 階層、ファイル名は
        ``{管理番号}_{時刻:%Y%m%d_%H%M}.csv``。
        """
        entry = load_master(paths["master_path"])["1001"]
        run_time = dt.time(9, 0)
        fixed_now = dt.datetime(2026, 9, 18, 9, 5)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        path = output_path(entry, run_time, now=fixed_now)
        assert path == (
            paths["base_path"] / paths["summary_folder_1001"] / "1001_20260918_0900.csv"
        )

    def test_falls_back_to_now_when_schedule_run_time_is_none(self, paths):
        """スケジュール行が無いレポート（後方互換）は ``now`` をそのまま使う。"""
        entry = load_master(paths["master_path"])["1001"]
        fixed_now = dt.datetime(2026, 1, 7, 13, 30)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        path = output_path(entry, None, now=fixed_now)
        assert path == (
            paths["base_path"] / paths["summary_folder_1001"] / "1001_20260107_1330.csv"
        )

    def test_uses_clock_now_when_now_argument_is_omitted(self, paths, monkeypatch):
        """``now`` 引数も省略した場合は ``clock_now()`` の現在時刻を使う。"""
        from comken.core.dates import now as clock_now

        entry = load_master(paths["master_path"])["1001"]
        fixed_now = dt.datetime(2026, 5, 4, 7, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        monkeypatch.setattr(paths_module, "clock_now", lambda: fixed_now)
        path = output_path(entry)
        # `paths.py` が import 時に `from comken.core.dates import now as clock_now`
        # でローカル束縛を作るので、ローカル属性も同期する必要がある
        monkeypatch.setattr("src.paths.clock_now", lambda: fixed_now)
        _ = clock_now  # ローカル束縛を作っただけで未参照、という警告の抑止
        assert path == (
            paths["base_path"] / paths["summary_folder_1001"] / "1001_20260504_0700.csv"
        )

    def test_folder_is_base_path_plus_summary(self, paths):
        """フォルダは「ベースパス / 概要」の 2 階層。「担当者」は階層に影響しない。"""
        entry = load_master(paths["master_path"])["1001"]
        path = output_path(entry, schedule_run_time=dt.time(9, 0))
        # ベースパスの直下には「概要」フォルダが来る
        assert path.parent == paths["base_path"] / paths["summary_folder_1001"]
        # 「担当者」は階層に影響しない（パスに出ない）
        assert "山田" not in path.parts
        # 「概要」は **フォルダ名として** パスに出る（ファイル名には混ぜない）
        assert paths["summary_folder_1001"] in path.parts

    def test_unknown_group_raises_group_not_registered(self, tmp_path, monkeypatch):
        """``report_folder()`` 経由の ``GroupNotRegisteredError`` がそのまま上がる。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "未知グループ",
                    "山田",
                    SUMMARY_1001,
                    URL_A,
                    "○",
                    "",
                ]
            ],
            settings_rows=[["営業本部", str(base_path)]],
        )
        monkeypatch.setattr(paths_module, "MASTER_PATH", master)
        entry = load_master(master)["1001"]
        with pytest.raises(GroupNotRegisteredError) as caught:
            output_path(entry, schedule_run_time=dt.time(9, 0))
        assert "未知グループ" in str(caught.value)
        assert "営業本部" in str(caught.value)


class TestGroupSettingsLoading:
    """設定シート経由で出力先が決まる経路の確認。"""

    def test_load_group_settings_reads_pairs(self, paths):
        """設定シートの「グループ」「ベースURL」を辞書にできる。"""
        settings = load_group_settings(paths["master_path"])
        assert settings == {"営業本部": paths["base_path"]}

    def test_output_path_uses_report_folder(self, paths):
        """``output_path()`` が ``report_folder()`` を経由してベースパス配下に作る。

        ベースパスの直下に「概要」フォルダが来る（2 階層）。
        """
        entry = load_master(paths["master_path"])["1001"]
        path = output_path(entry, schedule_run_time=dt.time(9, 0))
        assert path.parent == paths["base_path"] / paths["summary_folder_1001"]
        assert path.name.startswith("1001_")
        assert path.name.endswith(".csv")


class TestReportFolder:
    """``report_folder()`` は設定シートのベースパスをそのまま返す。"""

    def test_returns_base_path_only(self, paths):
        """「ベースパス」のみ返す。「担当者」「概要」の階層は作らない。"""
        entry = load_master(paths["master_path"])["1001"]
        settings = load_group_settings(paths["master_path"])
        folder = paths_module.report_folder(entry, settings)
        assert folder == paths["base_path"]

    def test_unknown_group_raises_group_not_registered(self, paths):
        """設定シートに無い登録グループを書くと ``GroupNotRegisteredError``。"""
        entry = load_master(paths["master_path"])["1001"]
        with pytest.raises(GroupNotRegisteredError) as caught:
            paths_module.report_folder(entry, {"別グループ": paths["base_path"]})
        assert "営業本部" in str(caught.value)


class TestBaseFolder:
    """``base_folder()`` は ``MASTER_PATH`` 経由で設定シートを引いてベースパスを返す。"""

    def test_returns_base_path_from_settings_sheet(self, paths):
        """設定シートの「ベースURL」をそのまま返す。フォルダは作らない・検査しない。"""
        entry = load_master(paths["master_path"])["1001"]
        assert paths_module.base_folder(entry) == paths["base_path"]


class TestSummaryFolderName:
    """``summary_folder_name()`` は Windows のファイル名規則に従って安全化する。

    定期取得と SOQL レポートの両方がこの関数を使う（``src.soql_reports.runner``
    側に同じ目的の関数を 2 つ持たないため）。
    """

    def test_strips_forbidden_chars(self):
        """``\\ / : * ? " < > |`` を取り除く。"""
        result = summary_folder_name('a\\b/c:d*e?f"g<h>i|j')
        assert result == "abcdefghij"

    def test_truncates_to_limit(self):
        """30 文字より長い概要は 30 文字で切る。"""
        result = summary_folder_name("あ" * 40)
        assert len(result) == 30

    def test_falls_back_to_report_when_empty(self):
        """空文字、または使えない文字だけになった場合は「レポート」。"""
        assert summary_folder_name("") == "レポート"
        assert summary_folder_name("///") == "レポート"
        assert summary_folder_name("\\/?:*") == "レポート"

    def test_strips_trailing_dots_and_spaces(self):
        """Windows のフォルダ名で使えない「末尾の ``.`` と空白」を取り除く。

        先頭の空白はそのまま（trailing だけ落とす。``strip()`` ではない）。
        """
        assert summary_folder_name("顧客一覧...") == "顧客一覧"
        assert summary_folder_name("顧客一覧 .") == "顧客一覧"
        assert summary_folder_name("顧客一覧   ") == "顧客一覧"
        # 末尾にスペースが残っていなければ、先頭のスペースはそのまま
        assert summary_folder_name(" 顧客一覧") == " 顧客一覧"

    def test_falls_back_to_report_when_only_trailing_punctuation(self):
        """使える文字はあるが末尾の「``.`` と空白」を取り除いて空になる場合は「レポート」。"""
        assert summary_folder_name(".") == "レポート"
        assert summary_folder_name(" . ") == "レポート"
        assert summary_folder_name("...") == "レポート"


class TestMasterPathPatchEffect:
    """``src.paths.MASTER_PATH`` の monkeypatch が ``output_path`` 経路に
    効いていることの確認。

    ``paths`` fixture は ``src.paths.MASTER_PATH`` を tmp_path に差し替える。
    ここで ``paths`` fixture を使わずに既定値の ``MASTER_PATH`` を直接
    ``load_master`` に渡すと、デフォルトの ``\\\\server\\share\\...`` を
    読みに行って ``ComkenFileNotFoundError`` で失敗する。
    これが「``paths`` fixture での monkeypatch が **実際に** ``output_path``
    経路に効いている」証拠（差し替えないと、既定値の UNC パスを読みに行って
    失敗するため）。
    """

    def test_default_master_path_points_to_unc_and_is_unreadable(self):
        """``MASTER_PATH`` の既定値は実在しない UNC パス。差し替えないと
        ``load_master`` が ``ComkenFileNotFoundError`` で失敗する。

        ``paths`` fixture を使っていないので monkeypatch は効かず、
        既定値の ``\\server\\share\\...`` を読みに行ってエラーになる。
        ``output_path`` も同じモジュール変数を参照するので、``paths`` fixture
        の monkeypatch が ``output_path`` 経路にも効いていると判断できる。
        """
        from comken.exceptions import ComkenFileNotFoundError

        # 既定値の構造: SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME の UNC パス
        expected = paths_module.SALESFORCE_DOWNLOADER_FOLDER / paths_module.MASTER_FILENAME
        assert paths_module.MASTER_PATH == expected

        with pytest.raises(ComkenFileNotFoundError):
            load_master(paths_module.MASTER_PATH)

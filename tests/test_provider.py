"""Salesforce レポートの集約取得（読み取る側）を、Salesforce をモックして検証する。

`cached_report()` / `file_path_of()` は設計上ネットワークを使わない。
`download_scheduled()` で置かれたファイルを、`cached_report()` が
受け取れるかを確かめる。`service.py` を経由した書き置き（`download_scheduled`）
が必要なので、`download_scheduled` も import している（テスト専用）。

`MASTER_PATH` / `HISTORY_PATH` は `monkeypatch.setattr` で tmp_path のパスへ
差し替える。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from comken.core.table import Table
from comken.toolbox.excel import Excel

from comken_salesforce_downloader import (
    cached_report,
    cached_report_path,
    download_scheduled,
    file_path_of,
    load_master,
)
from comken_salesforce_downloader import provider as provider_module
from comken_salesforce_downloader import service as service_module
from comken_salesforce_downloader.exceptions import (
    CachedReportNotFoundError,
    CachedReportNotRegisteredError,
    ReportDisabledError,
    ReportNotRegisteredError,
)

URL_A = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view"
URL_B = "https://example--sandbox.sandbox.my.salesforce.com/lightning/r/Report/00O5g00000FGHIJ/view"
ROWS = [{"名前": "山田", "金額": "100"}, {"名前": "鈴木", "金額": "200"}]

HEADERS = ["ID", "概要", "Salesforce URL", "実行方式", "保存先", "有効", "備考"]


@pytest.fixture(autouse=True)
def _reset_master_cache():
    """管理表のプロセス内キャッシュをテストごとに破棄する。

    `_find()` は ``Path.resolve()`` 後の絶対パスをキーに管理表をキャッシュする
    ため、 ``tmp_path`` が違うテスト同士はキーが違っていて**普通はリークしない**。
    ただしモンキーパッチで `_paths.MASTER_PATH` を差し替えた直後に古いキャッシュ
    を引きずらないよう、念のため明示的に破棄する。
    """
    provider_module._reset_cached_master()
    try:
        yield
    finally:
        provider_module._reset_cached_master()


def make_master(path: Path, rows: list[list]) -> Path:
    """管理表（Excel）を作る。"""
    table_rows = [dict(zip(HEADERS, row, strict=True)) for row in rows]
    with Excel(path) as book:
        book.create_data_sheet("管理表").create_table("管理表", Table(HEADERS, table_rows))
    return path


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """管理表・履歴・保存先をまとめて用意し、共有定数へ注入する。

    `_paths.MASTER_PATH` / `_paths.HISTORY_PATH` を tmp_path 配下の値へ
    差し替える。`service.py` / `provider.py` は import 時に独自のローカル束縛を
    作るので、両方の属性も同期する。
    """
    folder = tmp_path / "保存先"
    folder.mkdir()
    master = make_master(
        tmp_path / "レポート管理表.xlsx",
        [
            ["1001", "顧客一覧", URL_A, "定期", str(folder), "○", ""],
            ["1002", "売上実績", URL_B, "個別", str(folder), "○", ""],
            ["1003", "停止中", URL_B, "定期", str(folder), "×", ""],
        ],
    )
    history_path = tmp_path / "ダウンロード履歴.csv"
    monkeypatch.setattr("comken_salesforce_downloader._paths.MASTER_PATH", master)
    monkeypatch.setattr("comken_salesforce_downloader._paths.HISTORY_PATH", history_path)
    monkeypatch.setattr(service_module, "MASTER_PATH", master)
    monkeypatch.setattr(service_module, "HISTORY_PATH", history_path)
    # `provider` もローカル束縛しているので同期する
    monkeypatch.setattr(provider_module, "MASTER_PATH", master)
    return {
        "master_path": master,
        "history_path": history_path,
        "folder": folder,
    }


def fake_salesforce(rows: list[dict] | None = None) -> MagicMock:
    """report.get() が rows を返す Salesforce クライアント。"""
    client = MagicMock()
    client.__enter__.return_value.report.get.return_value = ROWS if rows is None else rows
    site = MagicMock(return_value=client)
    return site


class TestFilePathOf:
    """file_path_of() は「管理番号_概要_日付.csv」を組み立てるだけの純関数。"""

    def test_file_name_has_key_and_summary_and_date(self, tmp_path):
        folder = tmp_path / "保存先"
        folder.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [["1001", "顧客一覧", URL_A, "定期", str(folder), "○", ""]],
        )
        entry = load_master(master)["1001"]
        name = file_path_of(entry).name
        assert name.startswith("1001_顧客一覧_")
        assert name.endswith(".csv")
        assert len(name.removesuffix(".csv").rsplit("_", 3)[-1]) == 6

    def test_forbidden_characters_in_summary_are_stripped(self, tmp_path):
        """ファイル名に使えない文字（\\/:*?\"<>|）は落とす。"""
        folder = tmp_path / "保存先"
        folder.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [["1001", "禁則: A/B?C*D", URL_A, "定期", str(folder), "○", ""]],
        )
        entry = load_master(master)["1001"]
        name = file_path_of(entry).name
        # 禁則文字が含まれないこと（_ に置換されず、除去される）
        for forbidden in '\\/:*?"<>|':
            assert forbidden not in name

    def test_summary_limit_caps_long_names(self, tmp_path):
        """概要が長い場合は 30 文字で切る。"""
        folder = tmp_path / "保存先"
        folder.mkdir()
        long_summary = "あ" * 50
        master = make_master(
            tmp_path / "管理表.xlsx",
            [["1001", long_summary, URL_A, "定期", str(folder), "○", ""]],
        )
        entry = load_master(master)["1001"]
        name = file_path_of(entry).name
        # 管理番号とサフィックスを除いた概要部分が 30 文字以下
        summary_in_name = name.split("_", 1)[1].rsplit("_", 3)[0]
        assert len(summary_in_name) <= 30


class TestCachedReport:
    """cached_report() は固定パスだけを確認し、Salesforceへ取りに行かない。"""

    def test_returns_the_file_downloaded_by_the_scheduled_run(self, paths):
        with patch("comken_salesforce_downloader.service.site_for", return_value=fake_salesforce()):
            download_scheduled("定期実行")
        table = cached_report("1001")
        assert cached_report_path("1001").is_file()
        assert table.read_rows() == ROWS

    def test_does_not_call_salesforce(self, paths):
        with patch("comken_salesforce_downloader.service.site_for", return_value=fake_salesforce()):
            download_scheduled("定期実行")
        site = fake_salesforce()
        with patch("comken_salesforce_downloader.service.site_for", return_value=site):
            cached_report("1001")
        site.assert_not_called()

    def test_same_day_run_replaces_cache_and_keeps_archives(self, paths):
        updated_rows = [{"名前": "最新", "金額": "300"}]
        with patch(
            "comken_salesforce_downloader.service.site_for",
            return_value=fake_salesforce(),
        ):
            download_scheduled()
        with patch(
            "comken_salesforce_downloader.service.site_for",
            return_value=fake_salesforce(updated_rows),
        ):
            download_scheduled()

        assert cached_report("1001").read_rows() == updated_rows
        assert len(list(paths["folder"].glob("1001_*.csv"))) == 3

    def test_on_demand_report_raises(self, paths):
        """管理表で「個別」のものは、定期取得済みとして受け取れない。"""
        with pytest.raises(CachedReportNotRegisteredError):
            cached_report("1002")

    def test_not_downloaded_yet_raises(self, paths):
        with pytest.raises(CachedReportNotFoundError) as caught:
            cached_report("1001")
        assert "1001_顧客一覧_" in str(caught.value)
        assert "python main.py" in str(caught.value)

    def test_manual_file_at_the_displayed_path_is_used_on_rerun(self, paths):
        """例外が示した場所へCSVを置けば、登録操作なしで同じ処理を再実行できる。"""
        with pytest.raises(CachedReportNotFoundError) as caught:
            cached_report("1001")

        # エラー文の最後から2行目が、利用者へ案内する正確な配置先。
        # 実際の復旧手順と同じように、そのパスへ手動取得したCSVを置く。
        cache_path = Path(str(caught.value).splitlines()[-2])
        cache_path.write_text("名前,金額\n手動配置,999\n", encoding="utf-8")

        assert cached_report("1001").read_rows() == [{"名前": "手動配置", "金額": "999"}]

    def test_missing_cache_raises_even_if_archive_exists(self, paths):
        with patch("comken_salesforce_downloader.service.site_for", return_value=fake_salesforce()):
            download_scheduled("定期実行")
        # 時刻付き保管ファイルは残し、時刻を含まない当日キャッシュだけを消す。
        cache_path = provider_module._daily_cache_path_of(load_master(paths["master_path"])["1001"])
        cache_path.unlink()
        with pytest.raises(CachedReportNotFoundError):
            cached_report("1001")

    def test_unregistered_key_raises(self, paths):
        with pytest.raises(ReportNotRegisteredError):
            cached_report("9999")

    def test_disabled_report_raises(self, paths):
        """無効なレポートは「定期」指定でも例外（取る前段で止める）。"""
        with pytest.raises(ReportDisabledError):
            cached_report("1003")


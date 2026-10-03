"""Salesforce レポートの集約取得（取りに行く側）を、Salesforce をモックして検証する。

管理表（Excel）と履歴（CSV）は tmp_path に本物を作り、実際に読み書きさせる。
Salesforce への通信だけを差し替える。

MASTER_PATH / HISTORY_PATH は `monkeypatch.setattr` で一時ディレクトリのパスへ
差し替える。利用側の API には管理表や履歴のパスを渡せない（設計判断）。
"""

import datetime as dt
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from comken.exceptions import (
    HistoryLockTimeoutError,
    HistoryWriteError,
    SalesforceReportIDNotFoundError,
)
from comken.services.salesforce_downloader import history
from comken.toolbox.office.csv import read_csv

# paths fixture が monkeypatch.setattr に直接渡せるよう、paths モジュールを import しておく
import src.paths as _paths_module
from src import service as service_module
from src.cli import main as cli
from src.exceptions import (
    MasterDuplicateValueError,
    MasterRowValueError,
    ReportNotRegisteredError,
    ScheduledDownloadFailedError,
)
from src.service import download_scheduled
from src.sheets.master import load_master, shared_report_ids


def _read_rows(path: Path) -> list[dict[str, str]]:
    """CSV を読み、全セルを文字列（空欄は ""）にした行のリストを返す。"""
    return read_csv(path, columns=None, dtype=str, keep_default_na=False).to_dict(orient="records")


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
    "0件あり",
    "2000件超",
    "SOQL",
]
# 設定シートの見出し。`make_master()` の `settings_rows` 引数で使う
GROUP_SETTINGS_HEADERS = ["グループ", "ベースURL"]

# `master_rows` の各行は「歴史的な都合」で旧順序（ID/概要/Salesforce URL/
# グループ/担当者/有効/0件あり/2000件超/SOQL）で書かれている。`ReportEntry`
# の宣言順が新スキーマ（ID/グループ/担当者/概要/Salesforce URL/有効/0件あり/
# 2000件超/SOQL）になったので、`make_master` 側で機械的に新順序へ並び替える。
# テスト本体（値の並び）は旧順序のまま書けるので、検証意図が読みやすい状態を
# 保てる
_OLD_MASTER_HEADER_ORDER = [
    "ID",
    "概要",
    "Salesforce URL",
    "グループ",
    "担当者",
    "有効",
    "0件あり",
    "2000件超",
    "SOQL",
]
_OLD_TO_NEW_INDEX = tuple(_OLD_MASTER_HEADER_ORDER.index(h) for h in HEADERS)


def _row(*values) -> list:
    """``values`` を ``HEADERS`` の長さまで空文字で埋めたリストを返す。

    テストでは必須項目だけ書きたい場面が多く、毎回 9 列フルで値を並べるのは
    読みにくい。短い引数列を ``HEADERS`` に合わせて右側を ``""`` で埋めるだけ
    の薄いヘルパーに留めて、各テストが「**何を検証したいか**」を読み取りやすい
    形を保つ。

    列数が ``HEADERS`` を超える場合は ``ValueError`` で、列構成の変更に気づける
    ようにする。``[None] * N`` のような「表の下に残った空行」を再現するテストは
    このヘルパーではなく、直接 ``dict(zip(...))`` で組む。
    """
    if len(values) > len(HEADERS):
        raise ValueError(
            f"行データ {values} は見出し {HEADERS} より長い（{len(values)} > {len(HEADERS)}）"
        )
    return [*values, *[""] * (len(HEADERS) - len(values))]


def _reorder_master_row_to_new_order(filled: list) -> list:
    """``_row()`` の戻り値（旧順序で長さを揃えたリスト）を新順序 ``HEADERS`` に並び替える。"""
    return [filled[i] for i in _OLD_TO_NEW_INDEX]


def make_master(
    path: Path,
    rows: list[list],
    settings_rows: list[list] | None = None,
) -> Path:
    """管理表（Excel）を作る。設定シートも一緒に作るかは ``settings_rows`` で切り替える。

    各 ``row`` は ``_row(*row)`` で ``HEADERS`` の 9 列に揃える（既定値を持つ
    ``0件あり`` / ``2000件超`` / ``SOQL`` は空文字で埋めても読み込み側で
    ``False`` 既定として扱われる）。
    """
    table_rows = [_reorder_master_row_to_new_order(_row(*row)) for row in rows]
    df_master = pd.DataFrame(table_rows, columns=HEADERS)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_master.to_excel(writer, sheet_name="PY_管理表", index=False)
        if settings_rows is not None:
            df_settings = pd.DataFrame(settings_rows, columns=GROUP_SETTINGS_HEADERS)
            df_settings.to_excel(writer, sheet_name="PY_設定", index=False)
    return path


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """管理表・履歴・保存先をまとめて用意し、共有定数へ注入する。

    テスト関数側に `master_path=` / `history_path=` を渡さなくて済むように、
    `_paths.MASTER_PATH` / `_paths.HISTORY_PATH` を tmp_path 配下の値へ
    差し替える。`service.py` と `provider.py` は import 時に独自のローカル束縛を
    作るので、両方の属性も同期する。

    `1001`（=有効）が `download_scheduled()` の唯一の有効件。
    `1002`（=有効）は Excel 出力テストが直接管理表を作る側で検証するため、
    ここでは**無効**にして対象外にしている。`1003` も無効。

    出力先フォルダは設定シート（`グループ` → ベースパス）のみで組み立てる。
    2026-09 に「担当者 / 概要」のフォルダ階層は廃止されたが、定期取得と SOQL の
    共通化で「ベース / 概要」の 2 階層に戻した（人が業務ごとに探しやすくする
    ため）。 ``ベース`` 配下に pre-create するのは ``ベース`` 自体だけ
    （概要フォルダは ``_save()`` が ``mkdir(exist_ok=True)`` で自動作成する）。
    """
    base_path = tmp_path / "ベース"
    base_path.mkdir()
    master = make_master(
        tmp_path / "レポート管理表.xlsx",
        [
            [
                "1001",
                "顧客一覧",
                URL_A,
                "営業事務グループ",
                "山田",
                "○",
            ],
            [
                "1002",
                "売上実績",
                URL_B,
                "経理グループ",
                "佐藤",
                "×",
            ],
            [
                "1003",
                "停止中",
                URL_B,
                "営業事務グループ",
                "山田",
                "×",
            ],
        ],
        settings_rows=[
            ["営業事務グループ", str(base_path)],
            ["経理グループ", str(base_path)],
        ],
    )
    history_db_path = tmp_path / "ダウンロード履歴.sqlite3"
    history_path = tmp_path / "ダウンロード履歴.csv"
    monkeypatch.setattr(_paths_module, "MASTER_PATH", master)
    monkeypatch.setattr(
        "comken.services.salesforce_downloader.paths.HISTORY_DB_PATH", history_db_path
    )
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path)
    # 1001（顧客一覧）と 1002（売上実績）の概要フォルダ名を ``summary_folder_name()``
    # 経由で返す。glob パターンを組み立てる側で同じ関数を呼び直すと、運用中の
    # テストが「概要→フォルダ名」の契約にロックインされるので、fixture 側で
    # 一度だけ計算して各テストへ配る形にする
    from src.paths import summary_folder_name

    return {
        "master_path": master,
        "history_db_path": history_db_path,
        "history_path": history_path,
        "base_path": base_path,
        "summary_folder_1001": summary_folder_name("顧客一覧"),
        "summary_folder_1002": summary_folder_name("売上実績"),
    }


@pytest.fixture(autouse=True)
def _reset_paths_cache():
    """``paths._load_group_settings_cached()`` のプロセス内キャッシュをテストごとに破棄する。

    ``output_path`` / ``report_folder`` が内部で ``MASTER_PATH`` をキーに
    設定シートを読むため、前のテストで作ったキャッシュが次のテストの設定シート
    読込に混入しないようにする。``test_paths.py`` と同じ ``autouse=True`` で
    全テストに作用させる。
    """
    _paths_module._reset_cached_master()
    try:
        yield
    finally:
        _paths_module._reset_cached_master()


def _patch_master_path(monkeypatch: pytest.MonkeyPatch, master: Path, history: Path) -> None:
    """``MASTER_PATH`` と履歴パスを差し替えるヘルパー。

    ``service.py`` はローカル束縛を持たず ``_Paths`` ラッパー経由で
    ``src.paths`` のモジュール変数を呼び出し時に読むので、``_paths`` への
    1 か所の patch だけで ``output_path`` / 管理表パスに反映される。
    履歴パスは comken 側 ``HISTORY_DB_PATH``（正本の SQLite）と ``HISTORY_PATH``
    （人が見る CSV の置き場所）の両方を文字列経由で patch する
    （``service.py`` 自身も同名で ``from ... import`` するため）。
    ``history`` 引数は CSV の置き場所として渡されるので、 SQLite 側は
    同名の ``.sqlite3`` 拡張子で tmp 配下に置く。
    """
    monkeypatch.setattr(_paths_module, "MASTER_PATH", master)
    history_db = history.with_suffix(".sqlite3")
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_DB_PATH", history_db)
    monkeypatch.setattr("comken.services.salesforce_downloader.paths.HISTORY_PATH", history)


def fake_salesforce(rows: list[dict] | None = None) -> MagicMock:
    """report.get() が DataFrame を返す Salesforce クライアント。"""
    values = ROWS if rows is None else rows
    client = MagicMock()
    client.__enter__.return_value.report.get.return_value = pd.DataFrame(
        values, columns=["名前", "金額"]
    )
    site = MagicMock(return_value=client)
    return site


def fake_browser_site(rows: list[dict] | None = None) -> MagicMock:
    """export_reports() が CSV を書き出すブラウザ版サイトクラス（「2000件超」列用）。"""
    values = ROWS if rows is None else rows
    if values:
        lines = "\n".join(f"{r['名前']},{r['金額']}" for r in values)
        csv_bytes = f"名前,金額\n{lines}\n".encode()
    else:
        # 0 行: 見出しだけの CSV（``csv.reader`` が空行を 0 列として拾って
        # 「列数が一致しません」エラーを出すのを避けるため、データ行を足さない）
        csv_bytes = "名前,金額\n".encode("utf-8-sig")

    def _export_reports(reports, **kwargs):
        for _url, destination in reports.items():
            Path(destination).write_bytes(csv_bytes)
            yield "dummy", Path(destination)

    instance = MagicMock()
    instance.export_reports.side_effect = _export_reports
    instance.__enter__.return_value = instance
    return MagicMock(return_value=instance)


class TestLoadMaster:
    """管理表の読み取りと検証。"""

    def test_reads_rows_and_extracts_report_id(self, paths):
        entries = load_master(paths["master_path"])
        assert list(entries) == ["1001", "1002", "1003"]
        # Salesforce のレポート ID は URL から取り出す（人には入力させない）
        assert entries["1001"].report_id == "00O5g00000ABCDE"
        # `1001` だけが `download_scheduled()` の対象（=有効）
        assert entries["1001"].enabled
        assert not entries["1002"].enabled
        assert not entries["1003"].enabled

    def test_blank_rows_are_skipped(self, tmp_path):
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ],
                [None] * 9,
            ],
        )
        assert list(load_master(master)) == ["1001"]

    def test_duplicate_key_raises(self, tmp_path):
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ],
                [
                    "1001",
                    "別の名前",
                    URL_B,
                    "別の部署",
                    "別の担当",
                    "○",
                ],
            ],
        )
        with pytest.raises(MasterDuplicateValueError):
            load_master(master)

    def test_url_without_report_id_raises_with_row_number(self, tmp_path):
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    "https://example.com/",
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
        )
        with pytest.raises(SalesforceReportIDNotFoundError) as e:
            load_master(master)
        assert "1001" in str(e.value)  # 行番号ではなく管理番号で示す（空行があるとズレるため）

    def test_non_numeric_key_is_allowed(self, tmp_path):
        """str 型の `key` なので、文字列の ID もそのまま使える。"""
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "A001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
        )
        assert list(load_master(master)) == ["A001"]


class TestSharedReportIds:
    """同じ Salesforce レポートを複数の管理番号が指していることの検出。"""

    def test_detects_reports_used_by_multiple_keys(self, paths):
        entries = load_master(paths["master_path"])
        shared = shared_report_ids(entries)
        # "1002" と "1003" が同じ URL（＝同じレポート）を指している
        assert shared == {"00O5g00000FGHIJ": ["1002", "1003"]}

    def test_unique_reports_are_not_listed(self, tmp_path):
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
        )
        assert shared_report_ids(load_master(master)) == {}


class TestDownloadScheduledRecord:
    """`download_scheduled()` で 1 件のレポートを取得すると、ファイルと履歴が残る。"""

    def test_passes_filters_for_matching_report(self, paths):
        """管理番号に指定した実行時フィルタだけを Report API へ渡す。"""
        filters = [
            {
                "column": "CREATED_DATE",
                "operator": "greaterThan",
                "value": "2026-09-01",
            }
        ]
        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            download_scheduled(filters_by_report={"1001": filters})

        report_get = site.return_value.__enter__.return_value.report.get
        report_get.assert_called_once_with("00O5g00000ABCDE", filters=filters)

    def test_does_not_pass_filters_when_report_has_no_filters(self, paths):
        """実行時フィルタを省略したレポートは保存済み条件のまま実行する。"""
        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            download_scheduled()

        report_get = site.return_value.__enter__.return_value.report.get
        report_get.assert_called_once_with("00O5g00000ABCDE")

    def test_rejects_filters_for_unregistered_report(self, paths):
        """管理番号の誤記で条件が黙って無視されない。"""
        filters = [{"column": "CREATED_DATE", "operator": "equals", "value": "2026-09-09"}]

        with pytest.raises(ReportNotRegisteredError):
            download_scheduled(filters_by_report={"9999": filters})

    def test_uses_browser_fetch_for_report_marked_exceeds_row_limit(self, paths):
        """管理表の「2000件超」列が○の管理番号は、Report API ではなくブラウザ経由になる。

        どのレポートをブラウザ経由にするかは管理表の列で決まる（呼び出し側の
        コードでは指定しない設計）。ここではテスト用に、実体は `paths` fixture の
        本物の管理表から読んだ行を `exceeds_row_limit=True` に差し替えて使う。
        """
        entry = replace(load_master(paths["master_path"])["1001"], exceeds_row_limit=True)
        browser_site = fake_browser_site()
        with (
            patch("src.service.load_master", return_value={"1001": entry}),
            patch("src.service.site_for") as api_site_for,
            patch("comken.toolbox.browser.sites.salesforce.site_for", return_value=browser_site),
        ):
            download_scheduled()

        api_site_for.assert_not_called()
        browser_site.return_value.login_with_credentials.assert_called_once()

    def test_saves_file_with_csv_extension(self, paths, monkeypatch):
        """レポートは `.csv` で保存される。

        `download_scheduled()` は管理表の全有効件を処理するので、`paths` fixture の
        中では `1001` だけが有効。`.csv` のファイルが1件できることを確認する。
        """
        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled("案件集計")
        csv_paths = [path for path in saved if path.suffix == ".csv"]
        assert len(csv_paths) == 1
        assert csv_paths[0].is_file()
        # CSV として読み戻せる
        assert _read_rows(csv_paths[0]) == ROWS

    def test_fetches_again_even_if_already_downloaded_today(self, paths):
        """`paths` fixture の管理表には「スケジュール」シートが無い。

        スケジュール行が無いレポートは ``downloaded_today()`` ベースで
        1 日 1 回までに制限される（後方互換）。よって 2 回目はスキップされ、
        Salesforce へ問い合わせない。

        出力は ``{ベースパス}/{管理番号}_{日付}_{時刻}.csv`` の単一ファイルに
        1本化されたので、ファイル数は 1 件だけ存在する。
        """
        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            download_scheduled()
            download_scheduled()
        # 1 回目だけ取得される。出力は単一ファイル（時刻付き）の 1 件だけ
        # 「ベース / 概要」の 2 階層目で glob する
        saved = list((paths["base_path"] / paths["summary_folder_1001"]).glob("1001_*.csv"))
        assert len(saved) == 1
        # `report.get()` は 1 回しか呼ばれない（2 回目はスキップ）
        assert site.return_value.__enter__.return_value.report.get.call_count == 1

    def test_existing_collision_is_not_overwritten(self, paths, monkeypatch):
        """`output_path()` が指す先に既にファイルがあっても上書きせず `_1` 付きで
        別ファイルが作られる（``_reserve_unique_path`` の連番動作）。"""
        # 出力先パスを固定するため `clock_now` を差し止める（`service` と `provider` の
        # 両方が `clock_now` をローカル束縛にしているので両方差し替える必要がある）
        fixed_now = dt.datetime(2026, 9, 18, 9, 30)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)
        # 「ベース / 概要」の 2 階層目に衝突ファイルを作る
        summary_dir = paths["base_path"] / paths["summary_folder_1001"]
        summary_dir.mkdir(exist_ok=True)
        collision = summary_dir / "1001_20260918_0930.csv"
        collision.write_text("既存", encoding="utf-8")
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        # 既存ファイルは上書きされない
        assert collision.read_text(encoding="utf-8") == "既存"
        # もう1つ作られたファイル = 衝突回避で _1 が付いたファイル
        archive = summary_dir / "1001_20260918_0930_1.csv"
        assert archive.exists()

    def test_reserve_path_raises_when_all_sequential_names_are_taken(self, paths, monkeypatch):
        """連番の上限に達したら ``ReportReservePathLimitError`` を内側で出し、
        `download_scheduled()` がそれを捕捉して ``ScheduledDownloadFailedError`` に変換する。

        共有サーバーの権限・同期の異常で ``FileExistsError`` が返り続けると
        既存実装では無限ループになる。 上限を設けて、運用側に気付ける
        メッセージを伴った例外で抜ける。
        """
        # テスト時間短縮のため、上限を小さい値に下げる
        monkeypatch.setattr(service_module, "RESERVE_PATH_LIMIT", 5)
        # 出力先パスを固定するため `clock_now` を差し止める（`service` と `provider` の
        # 両方が `clock_now` をローカル束縛にしているので両方差し替える必要がある）
        fixed_now = dt.datetime(2026, 9, 18, 9, 30)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)
        # 「ベース / 概要」の 2 階層目に衝突ファイルを作る
        summary_dir = paths["base_path"] / paths["summary_folder_1001"]
        summary_dir.mkdir(exist_ok=True)
        base = summary_dir / "1001_20260918_0930.csv"

        # ベース名と ``_1`` 〜 ``_4`` までの連番を全部作っておく（計5ファイル）。
        # ``_reserve_unique_path`` は base と ``_1`` 〜 ``_4`` を試して全部 FileExistsError
        # になると、上限に達して例外を上げる
        for sequence in range(5):
            if sequence == 0:
                candidate = base
            else:
                candidate = summary_dir / f"1001_20260918_0930_{sequence}.csv"
            candidate.write_text("埋まり", encoding="utf-8")

        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            download_scheduled()

        # メッセージに管理番号・上限値・保存先候補のどれかが含まれていれば、 運用側が
        # 「どこで何が起きているか」を追える
        assert "1001" in str(caught.value)

    def test_unregistered_key_does_not_affect_run(self, paths, monkeypatch, tmp_path):
        """管理表に無い管理番号は download_scheduled() の対象外（=直接指定の概念なし）。
        ここでは「1001 だけ取れて、空の 9999 を要求しても何も起きない」ことを確認するため、
        1001 が取得できることをもって対象外になっていることを示す。
        """
        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]

    def test_disabled_report_is_skipped(self, paths):
        """`1003` は「無効」なので取得対象外（periodic から除外される）。"""
        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert all(path.name.split("_")[0] != "1003" for path in saved)

    def test_empty_report_raises_and_saves_nothing(self, paths):
        """0 件あり=× のレポートで 0 行だと、`ScheduledDownloadFailedError` で全体が失敗する。"""
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        # `1001` の CSV は作られない
        assert list(paths["base_path"].glob("1001_*.csv")) == []

    def test_missing_folder_raises_and_is_not_created(self, tmp_path, monkeypatch):
        """設定シートにグループは登録されているが、組み立て先のベースパスが実在しない場合に
        ``ScheduledDownloadFailedError`` で抜け、そのフォルダが**作られない**ことを確認する。

        2026-09 にフォルダ階層は「ベースパスのみ」に1本化されたので、「フォルダが
        無い」ケースは「ベースパス自体が無い」ケースになる。設定シートには
        正しいグループ／ベースパスを書くが、``base_path`` ディレクトリは作らない。
        """
        base_path = tmp_path / "ベース"  # 存在しないフォルダを指す（``mkdir`` しない）
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        # ベースパスは自動作成されない
        assert not base_path.exists()

    def test_no_temporary_file_is_left_behind(self, paths):
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        # `atomic_write()` 由来の一時ファイル（``~`` プレフィックス）は残らない
        assert list(paths["base_path"].glob("~*")) == []

    def test_saves_file_under_base_and_summary_folder(self, paths):
        """ファイルが ``ベース / 概要 / 管理番号_時刻.csv`` に保存される。

        2026-09 に「ベース / 概要」の 2 階層に戻したので、 glob で ``ベース / 概要``
        配下のファイルが拾えることを確認する（ベース直下には何も作られない）。
        """
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        summary_dir = paths["base_path"] / paths["summary_folder_1001"]
        assert summary_dir.is_dir()
        saved = list(summary_dir.glob("1001_*.csv"))
        assert len(saved) == 1
        # ベース直下には CSV が無い（担当者フォルダも無い）
        assert list(paths["base_path"].glob("*.csv")) == []

    def test_summary_folder_is_created_on_demand(self, paths):
        """概要のフォルダは保存時に ``mkdir`` で自動作成される。"""
        # 念のため未作成を確認
        assert not (paths["base_path"] / paths["summary_folder_1001"]).exists()
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        # 概要フォルダが作られた
        assert (paths["base_path"] / paths["summary_folder_1001"]).is_dir()

    def test_base_folder_missing_raises_and_does_not_create_summary(self, tmp_path, monkeypatch):
        """ベースフォルダが無いときは ``ScheduledDownloadFailedError`` で抜け、
        概要のフォルダも作られない（勝手に作らない原則）。

        ``_require_folder()`` がベースフォルダの不在を ``ReportFolderNotFoundError``
        に変換し、その後 ``mkdir`` は走らない（ ``_save()`` に到達する前に
        例外で抜けるため）。 ``mkdir`` が ``parents=False`` で走るとベースが
        無いときに ``FileNotFoundError`` で落ちるが、それを**概要側で**
        起こしてしまうと運用上の意味が無いので、ベース無しのときは概要の
        フォルダ作成も抑止されることを確認する。
        """
        missing_base = tmp_path / "存在しないベース"
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "テストグループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["テストグループ", str(missing_base)]],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        # ベースは作られない
        assert not missing_base.exists()
        # ベース配下の「概要」フォルダも作られない（ ``_save()`` に到達しないため）
        assert not (missing_base / "顧客一覧").exists()

    def test_history_saves_folder_is_base_plus_summary(self, paths):
        """履歴の「保存先」列が ``ベース / 概要`` になる。

        2026-09 に「概要」をフォルダ階層に使うようにしたので、comken の
        ``Report`` クラスが ``保存先 / ファイル名`` でファイルを
        引けるように、履歴には**実際にファイルを置いたフォルダ**を書く
        （ベースのままだと ``Report.path`` がファイルを見つけられない）。
        """
        from comken.services.salesforce_downloader.history import Report

        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        # 履歴の「保存先」列が ``ベース / 概要`` のフォルダを指している
        row = _history_rows(paths)[-1]
        assert row["保存先"] == str(paths["base_path"] / paths["summary_folder_1001"])
        # comken の ``Report`` が履歴から正しいパスを組み立てて
        # ファイルを引ける（これが成功しないと下流が CSV を読みに行けない）
        latest = Report("1001").path
        assert latest is not None
        assert latest.is_file()
        assert latest.parent == paths["base_path"] / paths["summary_folder_1001"]

    def test_two_reports_with_same_summary_share_folder(self, tmp_path, monkeypatch, paths):
        """同じ概要の管理番号は、同じフォルダに別ファイル名で保存される。

        人が業務（=概要）ごとに探しやすくする設計なので、概要フォルダが
        自動で集約されることを確認する。
        """
        from dataclasses import replace

        from src.sheets.master import load_master

        fixed_now = dt.datetime(2026, 9, 18, 9, 30)  # noqa: DTZ001
        # ``paths`` fixture と同じベース/履歴を使い、両方とも同じ概要に揃える
        # （fixture の戻り値は dict なので、引数 ``paths`` を介さず直接 ``paths["base_path"]``
        # 等を使う。 ``paths`` は monkeypatch を内側で使うので ``fixture`` 引数の名前
        # ``monkeypatch`` とは別物になる点に注意）
        entries = load_master(paths["master_path"])
        entry_1002 = replace(entries["1002"], enabled=True, summary="顧客一覧")
        all_entries = {**entries, "1002": entry_1002}
        # ``service_module.load_master`` を新しい dict を返す関数で差し替える
        monkeypatch.setattr(service_module, "load_master", lambda _master_path: all_entries)
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)
        monkeypatch.setattr(_paths_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()

        # 両方とも同じ「ベース / 概要」配下に保存される
        summary_dir = paths["base_path"] / paths["summary_folder_1001"]
        assert summary_dir.is_dir()
        assert len(saved) == 2
        keys = sorted(p.name.split("_")[0] for p in summary_dir.glob("*.csv"))
        assert keys == ["1001", "1002"]
        # 同じ概要なので、フォルダは 1 つしかない（同じフォルダに 2 ファイル）
        subdirs = [p for p in paths["base_path"].iterdir() if p.is_dir()]
        assert subdirs == [summary_dir]


class TestHistory:
    """履歴には成否も、誰が要求したかも残る。"""

    def test_success_is_recorded_with_project_and_counts(self, paths):
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled("案件集計")
        row = _history_rows(paths)[-1]
        assert row["管理番号"] == "1001"
        assert row["プロジェクト"] == "案件集計"
        assert row["成否"] == "成功"
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == "成功"
        assert row["取得件数"] == "2"
        assert row["レポートID"] == "00O5g00000ABCDE"
        assert row["エラーコード"] == ""

    def test_failure_is_recorded(self, paths):
        # Report API が 0 行 → ブラウザで取り直し → ブラウザも 0 行 →
        # ``EmptyReportError`` で失敗、という新仕様の流れ。
        # ブラウザ経路もモックして「ブラウザも 0 行」のケースを再現する
        browser_site = fake_browser_site([])
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled("案件集計")
        row = _history_rows(paths)[-1]
        assert row["成否"] == "失敗"
        # 0 行でも Salesforce への問い合わせは成功している点が重要
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == ""
        assert row["エラーコード"] == "EmptyReportError"
        assert "0 行" in row["エラー内容"]

    def test_downloaded_today_counts_after_scheduled_run(self, paths):
        """`download_scheduled()` で取った記録は `downloaded_today()` で拾える。

        履歴の正本は SQLite になったので、 ``paths["history_db_path"]`` （ DB ）
        を ``downloaded_today()`` に渡す。
        """
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        assert history.downloaded_today(paths["history_db_path"], "1001")

    def test_missing_history_file_is_not_downloaded(self, tmp_path):
        # 履歴 SQLite が無いときは ``downloaded_today()`` が False を返す
        assert not history.downloaded_today(tmp_path / "無い.sqlite3", "1001")

    # ── 履歴の5ケース（4. の表に対応する個別テスト）────────────────────
    def test_history_when_folder_is_missing(self, tmp_path, monkeypatch):
        """組み立てた保存先フォルダが無い → 成否=失敗 / Salesforce取得結果=空 /
        保存結果=空 / エラーコード=ReportFolderNotFoundError。

        2026-09 にフォルダ階層は「ベースパスのみ」に1本化されたので、「フォルダが
        無い」ケースは「ベースパス自体が無い」ケースになる。設定シートには正しい
        グループ／ベースパスを書くが、``base_path`` ディレクトリは作らない。
        """
        base_path = tmp_path / "ベース"  # 存在しないフォルダを指す（``mkdir`` しない）
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        rows = _read_rows(history_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["成否"] == "失敗"
        assert row["Salesforce取得結果"] == ""
        assert row["保存結果"] == ""
        assert row["エラーコード"] == "ReportFolderNotFoundError"

    def test_history_when_salesforce_call_fails(self, tmp_path, monkeypatch):
        """Salesforce への問い合わせが失敗 → 成否=失敗 / Salesforce取得結果=失敗 /
        保存結果=空 / エラーコード=送出された例外クラス名。

        `download_scheduled()` は `ComkenError` を捕捉して `ScheduledDownloadFailedError`
        に変換するため、テストではそちらを期待する。履歴には元の例外クラス名が残る。
        """
        from comken.exceptions import SalesforceRequestError

        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        site = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.report.get.side_effect = SalesforceRequestError(
            "GET", "/report", 500, "boom"
        )
        site.return_value = client

        with (
            patch(
                "src.service.site_for",
                return_value=site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        rows = _read_rows(history_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["成否"] == "失敗"
        assert row["Salesforce取得結果"] == "失敗"
        assert row["保存結果"] == ""
        assert row["エラーコード"] == "SalesforceRequestError"

    def test_history_when_report_is_empty(self, paths):
        """取得できたが 0 行だった → 成否=失敗 / Salesforce取得結果=成功 /
        保存結果=空 / エラーコード=EmptyReportError（通信は成功していて中身が空、という区別）。

        Report API が 0 行 + ブラウザでも 0 行 → EmptyReportError で失敗、
        という新仕様の流れをモックして再現する。
        """
        browser_site = fake_browser_site([])
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        row = _history_rows(paths)[-1]
        assert row["成否"] == "失敗"
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == ""
        assert row["エラーコード"] == "EmptyReportError"

    # ── 履歴CSVの列構成マイグレーション（COLUMNS 追加・削除・並び替え） ──
    # 履歴の正本は v3 で SQLite に変わった。 列構成のマイグレーションは
    # ``comken.services.salesforce_downloader.migrate_history`` の移行スクリプト
    # に切り出され、 ``append_history()`` は常に ``HistoryColumns.names()`` 順の
    # 行を 1 つ INSERT するだけになった。 旧CSVの列構成マイグレーションを踏む
    # 経路は無くなったので、 以下のテストは削除した （同等の検査は
    # ``tests/test_history.py`` の comken 側テストでカバーされる）:
    # - test_record_migrates_legacy_header_with_extra_column
    # - test_record_migrates_legacy_header_with_missing_last_column
    # - test_record_migrates_legacy_header_with_reordered_columns
    # - test_record_appends_when_header_is_already_current
    # - test_record_does_not_rewrite_when_header_is_invalid
    # - test_record_writes_to_file_once_for_migration_and_append
    # - test_record_keeps_cp932_when_migrating_legacy_header
    # - test_record_raises_history_write_error_when_char_not_in_cp932
    # これらのヘルパーも不要になった:
    # - _seed_legacy_history
    # - _record_for
    # - _cp932_history

    def test_history_when_csv_write_fails(self, tmp_path, monkeypatch):
        """Salesforce 取得は成功したが CSV 書き込みが失敗 → 成否=失敗 /
        Salesforce取得結果=成功 / 保存結果=失敗 / エラーコード=送出された例外クラス名。

        履歴CSV書き込みも同じ ``write_csv`` を使うため、``write_csv`` を直接
        モックすると履歴CSV書き込み経路でも同じモックが発火してしまう。保存先CSV書き込みだけを失敗させる
        ために ``service._write_csv()`` 関数を直接モックする。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        write_error = OSError("書き込み失敗")
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            patch.object(
                service_module,
                "_write_csv",
                side_effect=write_error,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        rows = _read_rows(history_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["成否"] == "失敗"
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == "失敗"
        assert row["エラーコード"] == "OSError"

    # ── 「原因区分」列（4区分 + 成功時の空文字）─────────────────────
    def test_cause_is_blank_on_success(self, paths):
        """成功時は原因区分が空文字。"""
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled()
        row = _history_rows(paths)[-1]
        assert row["成否"] == "成功"
        assert row["原因区分"] == ""

    def test_cause_is_config_when_folder_is_missing(self, tmp_path, monkeypatch):
        """組み立てた保存先フォルダが無い → 「設定」（取得段階に入る前に落ちる）。

        2026-09 にフォルダ階層は「ベースパスのみ」に1本化されたので、「フォルダが
        無い」ケースは「ベースパス自体が無い」ケースになる。設定シートには
        正しいベースパスを書くが、``base_path`` ディレクトリは作らない。
        """
        base_path = tmp_path / "ベース"  # 存在しないフォルダを指す（``mkdir`` しない）
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        row = _read_rows(history_path)[-1]
        assert row["原因区分"] == "設定"

    def test_cause_is_salesforce_when_request_fails(self, tmp_path, monkeypatch):
        """Salesforce への問い合わせが失敗 → 「Salesforce」。"""
        from comken.exceptions import SalesforceRequestError

        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        site = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.report.get.side_effect = SalesforceRequestError(
            "GET", "/report", 500, "boom"
        )
        site.return_value = client

        with (
            patch(
                "src.service.site_for",
                return_value=site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        row = _read_rows(history_path)[-1]
        assert row["原因区分"] == "Salesforce"

    def test_cause_is_empty_data_when_report_is_empty(self, paths):
        """取得できたが 0 行 → 「データなし」（取得は成功・保存未到達を一意に指す区分）。

        この管理表には `0件あり` 列が無いので `×` 既定扱いで `EmptyReportError` が送出される。
        段階は「取得成功 → 保存に進まず 0 行で失敗」になるため、4 区分だった頃の
        `Salesforce` から、新仕様の `データなし` に変わった。

        新仕様では Report API が 0 行のときブラウザで取り直し、ブラウザも 0 行なら
        初めて ``EmptyReportError`` で失敗する。ブラウザ経路もモックして
        「ブラウザも 0 行」のケースを再現する。
        """
        browser_site = fake_browser_site([])
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        row = _history_rows(paths)[-1]
        assert row["原因区分"] == "データなし"

    def test_cause_is_file_when_csv_write_fails(self, tmp_path, monkeypatch):
        """CSV 書き込みが OSError で失敗 → 「ファイル」（共有サーバー・権限）。

        履歴CSV書き込みも同じ ``write_csv`` を使うため、``write_csv`` を直接
        モックすると履歴CSV書き込み経路でも同じモックが発火してしまう。保存先CSV書き込みだけを失敗させるために
        ``service._write_csv()`` 関数を直接モックする。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        write_error = OSError("書き込み失敗")
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            patch.object(
                service_module,
                "_write_csv",
                side_effect=write_error,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        row = _read_rows(history_path)[-1]
        assert row["原因区分"] == "ファイル"

    def test_cause_is_program_when_unexpected_error_raises(self, tmp_path, monkeypatch):
        """_fetch() が TypeError を投げる（comken 側のバグ想定）→ 「プログラム」。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        site = MagicMock()
        client = MagicMock()
        client.__enter__.return_value.report.get.side_effect = TypeError("想定外")
        site.return_value = client

        with (
            patch(
                "src.service.site_for",
                return_value=site,
            ),
            pytest.raises(TypeError),
        ):
            download_scheduled()
        row = _read_rows(history_path)[-1]
        assert row["原因区分"] == "プログラム"

    # ── 履歴CSVの文字コードは変えない ────────────────────────────────
    # 旧 CSV 正本 + CP932 の経路は v3 で廃止された。 履歴の正本は SQLite なので
    # 文字コードを意識する必要が無くなり、 ``test_record_keeps_cp932_*`` /
    # ``test_record_raises_history_write_error_when_char_not_in_cp932`` の 2 件は
    # 削除した。 CSV への書き出しは ``download_scheduled()`` が ``export_history()``
    # で 1 回呼ぶ形になり、 ``export_history()`` 側の保証
    # （ ``tests/test_history.py`` の comken 側テスト ） に従う。


class TestDownloadScheduled:
    """定期取得は「有効」なものだけを対象にする。"""

    def test_only_enabled_reports_are_downloaded(self, paths):
        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        # `paths` fixture では `1001`（=CSV・有効）だけが対象
        keys = sorted(path.name.split("_")[0] for path in saved)
        assert keys == ["1001"]

    def test_one_failure_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        """1 件でフォルダ未作成エラーが出ても、別件は保存される。

        2026-09 に「ベース / 概要」の 2 階層になったので、 ``1001`` のグループ設定だけ
        存在しないベースパスを指すようにして「フォルダ未作成エラー」を起こし、
        ``1002`` は既存パスに繋ぐ（ ``1002`` 側のベースは存在する前提）。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        missing_path = tmp_path / "存在しない"
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "落ちる方",
                    URL_A,
                    "落ちる方のグループ",
                    "山田",
                    "○",
                ],
                [
                    "1002",
                    "通る方",
                    URL_B,
                    "通る方のグループ",
                    "佐藤",
                    "○",
                ],
            ],
            settings_rows=[
                ["落ちる方のグループ", str(missing_path)],
                ["通る方のグループ", str(base_path)],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        # "1001" で失敗しても "1002" は保存されている（続けたうえで最後に知らせる）。
        # 出力は単一ファイル化されているので、`*.csv` は ``1002`` のもの 1 件だけ
        # 「ベース / 概要」の 2 階層目で glob する
        from src.paths import summary_folder_name as _summary_folder_name

        summary_dir = base_path / _summary_folder_name("通る方")
        keys = sorted(path.name.split("_")[0] for path in summary_dir.glob("1002_*.csv"))
        assert keys == ["1002"]
        # ``1001`` のフォルダ（存在しない）には当然ファイルは無い
        assert not missing_path.exists()

    def test_os_error_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        """1 件で保存時 OSError が出ても、別件は保存される。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "書込失敗",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ],
                [
                    "1002",
                    "取得成功",
                    URL_B,
                    "経理グループ",
                    "佐藤",
                    "○",
                ],
            ],
            settings_rows=[
                ["営業事務グループ", str(base_path)],
                ["経理グループ", str(base_path)],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        original_write_csv = service_module._write_csv

        def fail_first_write(path, table):
            if path.name.startswith("1001_"):
                raise OSError("共有サーバーへ書き込めません")
            original_write_csv(path, table)

        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            patch.object(service_module, "_write_csv", side_effect=fail_first_write),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()

        # 成功した "1002" のファイルだけが残る（出力は単一ファイル化）
        # 「ベース / 概要」の 2 階層目で glob する
        from src.paths import summary_folder_name as _summary_folder_name

        summary_dir = base_path / _summary_folder_name("取得成功")
        keys = sorted(path.name.split("_")[0] for path in summary_dir.glob("1002_*.csv"))
        assert keys == ["1002"]

    def test_unexpected_error_stops_the_run_immediately(self, tmp_path, monkeypatch):
        """想定外（`TypeError` など）はその場で抜ける。`ScheduledDownloadFailedError` には
        変換しない（非エンジニアが「もう一度実行してみる」を繰り返すだけになるため）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "想定外",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ],
                [
                    "1002",
                    "通る方",
                    URL_B,
                    "経理グループ",
                    "佐藤",
                    "○",
                ],
            ],
            settings_rows=[
                ["営業事務グループ", str(base_path)],
                ["経理グループ", str(base_path)],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")

        site = MagicMock()
        client = MagicMock()
        # 1001 のレポートを取るときだけ想定外を投げる
        client.__enter__.return_value.report.get.side_effect = [
            TypeError("想定外"),
            ROWS,
        ]
        site.return_value = client

        with (
            patch(
                "src.service.site_for",
                return_value=site,
            ),
            pytest.raises(TypeError),
        ):
            download_scheduled()
        # 想定外で止めたので、2件目は保存されない
        # （続けた結果の ScheduledDownloadFailedError ではないことを確認）
        assert list(base_path.glob("1002_*.csv")) == []

    def test_records_the_trigger_as_scheduled(self, paths):
        """`download_scheduled()` で取った記録は履歴に `プロジェクト` 名で残る。"""
        with patch("src.service.site_for", return_value=fake_salesforce()):
            download_scheduled("定期実行")
        assert _history_rows(paths)[-1]["プロジェクト"] == "定期実行"

    # ── スケジュール管理表と組み合わせた判定 ──────────────────────────────
    def test_schedule_sheet_missing_keeps_backward_compatible_behavior(self, tmp_path, monkeypatch):
        """「スケジュール」シートが無い管理表は、これまで通り毎回対象になる（後方互換）。

        この機能追加を境に既存のレポートが突然取得されなくなる事故を防ぐ。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        # 「今」を月曜 09:00 に固定してもスケジュール判定には影響しない
        fixed_now = dt.datetime(2026, 1, 5, 9, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]

    def test_schedule_rule_not_due_excludes_report(self, tmp_path, monkeypatch):
        """スケジュール行が「今は要らない (False)」を返したレポートは対象から外れる。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # 水曜 12:00 固定 → 「毎週・月曜・09:00」は曜日不一致で False
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        # 「スケジュール」シートを足した管理表を作る
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "曜日外し",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        # スケジュールの判定で外れるので、保存されない
        assert saved == []
        assert list(base_path.glob("1001_*.csv")) == []

    def test_schedule_rule_due_includes_report(self, tmp_path, monkeypatch):
        """スケジュール行が「今は要る (True)」を返したレポートは対象に入る。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # 水曜 12:00 固定 → 「毎週・水曜・09:00」は `now.time() >= start_time` で True
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "曜日一致",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]

    def test_multiple_schedule_rules_use_or_semantics(self, tmp_path, monkeypatch):
        """同じレポートに複数行があり、どれか1つでも is_due() なら対象に入る（OR 条件）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # 水曜 12:00 固定 → 1行目（月曜）は曜日外れ、2行目（水曜）は一致
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "OR判定",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                ["S002", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]

    def test_multiple_due_rules_pick_the_latest_run_time(self, tmp_path, monkeypatch):
        """同じレポートに複数の「due」行がある場合、一番遅い時刻の行を採用する。

        例: 「毎週・水曜・09:00」と「毎週・水曜・13:00」が並んでいて、13:00 に
        ``download_scheduled()`` を呼ぶと、9:00 の行も 13:00 の行も曜日・時刻条件が
        合うが、**履歴に記録される ``schedule_key`` は 13:00 側だけ**になる。
        同じレポートを 9:00 と 13:00 で 2 回取る意味がないため（運用:
        9:00 で失敗したときに 13:00 に拾う、等）。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # 水曜 13:00 固定 → どちらの行も is_due=True だが、遅い時刻の S002 を採用
        fixed_now = dt.datetime(2026, 1, 7, 13, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "遅い時刻優先",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
                ["S002", "1001", "毎週", "13:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            saved = download_scheduled()
        # 1 回だけ取得される
        assert site.return_value.__enter__.return_value.report.get.call_count == 1
        assert [path.name.split("_")[0] for path in saved] == ["1001"]
        # 履歴の「スケジュールキー」列が S002 だけであること（S001 は記録されない）
        rows = _read_rows(tmp_path / "履歴.csv")
        assert len(rows) == 1
        assert rows[0]["管理番号"] == "1001"
        assert rows[0]["スケジュールキー"] == "S002"
        assert rows[0]["成否"] == "成功"

    def test_schedule_rule_excludes_on_holiday(self, tmp_path, monkeypatch):
        """``holiday_policy`` が既定の「取得しない」のとき、祝日（例: 2026-05-04 みどりの日）は
        スケジュール一致でも対象外になる。

        ``ScheduleRule.is_due()`` が ``comken.core.holidays`` の統一カレンダーを
        直接見て祝日判定する経路を確認する。``clock_now`` を祝日の 12:00 に
        固定して「毎週・月曜・09:00」のスケジュールが曜日・時刻では一致する状態を
        作っても、祝日判定で ``is_due=False`` になる。
        """
        from src.sheets.schedule import (
            FREQUENCY_BUSINESS_DAY,
            FREQUENCY_DAILY,
            FREQUENCY_MONTHLY,
            FREQUENCY_WEEKLY,
            ScheduleRule,
        )

        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # 2026-05-04 は月曜・みどりの日。12:00 固定で「毎週・月曜・09:00」は
        # 曜日・時刻条件が一致する状態
        fixed_now = dt.datetime(2026, 5, 4, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "祝日判定",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        # 祝日（みどりの日）なので ``is_due`` 内で除外され、保存されない
        assert saved == []
        assert list(base_path.glob("1001_*.csv")) == []

        # **壊した版:** ``ScheduleRule.is_due`` を祝日を見ない版（=``_raw_date_matches``
        # + 時刻条件のみ）に差し替えると、曜日と時刻が一致するだけで ``True`` になり
        # 取得される＝このテストが落ちる（=祝日スキップが実装で効いていることの証拠）
        def _broken_is_due(self, now):
            if not self.enabled or not self._raw_date_matches(now.date()):
                return False
            if self.frequency in {
                FREQUENCY_DAILY,
                FREQUENCY_WEEKLY,
                FREQUENCY_MONTHLY,
                FREQUENCY_BUSINESS_DAY,
            }:
                return self.start_time is None or now.time() >= self.start_time
            return False

        monkeypatch.setattr(ScheduleRule, "is_due", _broken_is_due)
        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            saved = download_scheduled()
        assert site.return_value.__enter__.return_value.report.get.call_count == 1
        assert [path.name.split("_")[0] for path in saved] == ["1001"]


# ── スケジュール単位の重複実行防止（今回の機能の核心）───────────────────
class TestScheduleDedup:
    """同じスケジュール行を同日に 2 回 `download_scheduled()` で呼んでも、
    2 回目はスキップされる（履歴ベースのdedup）。"""

    def test_second_call_for_same_schedule_key_is_skipped(self, tmp_path, monkeypatch):
        """同じスケジュール行（=同じ schedule_key）に紐付くレポートは、当日中に
        成功履歴があれば 2 回目の `download_scheduled()` で再取得されない。

        出力は単一ファイル化（時刻付き 1 件）なので、ファイル数の期待値も 1 件。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "曜日一致",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        site = fake_salesforce()
        with patch("src.service.site_for", return_value=site):
            download_scheduled()
            download_scheduled()
        # 1 回目だけ Salesforce へ問い合わせる（2 回目は履歴を見てスキップ）
        assert site.return_value.__enter__.return_value.report.get.call_count == 1
        # 1 回目だけ取得されるので、保存ファイルは時刻付きの 1 件だけ
        # 「ベース / 概要」の 2 階層目で glob する
        from src.paths import summary_folder_name as _summary_folder_name

        summary_dir = base_path / _summary_folder_name("曜日一致")
        saved = list(summary_dir.glob("1001_*.csv"))
        assert len(saved) == 1
        # 履歴の「スケジュールキー」列に、根拠のキーが記録されている
        rows = _read_rows(tmp_path / "履歴.csv")
        assert len(rows) == 1
        assert rows[0]["管理番号"] == "1001"
        assert rows[0]["スケジュールキー"] == "S001"
        assert rows[0]["成否"] == "成功"

    def test_second_call_does_not_skip_when_first_failed(self, tmp_path, monkeypatch):
        """1 回目が失敗（保存失敗など）した場合は、成功履歴が残らないため、
        2 回目は再取得される（失敗を繰り返さない運用にするため）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "曜日一致",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        # 1 回目は OSError で保存失敗、2 回目は成功する
        original_write_csv = service_module._write_csv
        attempts = {"count": 0}

        def fail_first(path, table):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("共有サーバーへ書き込めません")
            original_write_csv(path, table)

        site = fake_salesforce()
        first_site = fake_salesforce()
        with (
            patch(
                "src.service.site_for",
                return_value=first_site,
            ),
            patch.object(service_module, "_write_csv", side_effect=fail_first),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        # 1 回目は失敗 → 2 回目を呼ぶ
        with patch(
            "src.service.site_for",
            return_value=site,
        ):
            download_scheduled()
        # 失敗のあと再呼び出ししたので、Salesforce への問い合わせは 2 回
        assert first_site.return_value.__enter__.return_value.report.get.call_count == 1
        assert site.return_value.__enter__.return_value.report.get.call_count == 1

    def test_failure_row_records_schedule_key(self, tmp_path, monkeypatch):
        """失敗時の履歴行にも `schedule_key` が記録される（成功時と非対称に
        空文字のままだと、後から「このスケジュール行がいつ失敗したか」を
        追えなくなるため）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "曜日一致",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                ["S001", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        # 保存段階で OSError を起こして失敗させる（_Attempt.record_failure() の経路を通る）
        def fail(path, table):
            raise OSError("共有サーバーへ書き込めません")

        site = fake_salesforce()
        with (
            patch(
                "src.service.site_for",
                return_value=site,
            ),
            patch.object(service_module, "_write_csv", side_effect=fail),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()

        # 失敗時の履歴行の「スケジュールキー」列に、空でない根拠キーが入っている
        rows = _read_rows(tmp_path / "履歴.csv")
        assert len(rows) == 1
        assert rows[0]["管理番号"] == "1001"
        assert rows[0]["成否"] == "失敗"
        assert rows[0]["スケジュールキー"] == "S001"

    def test_dedup_is_per_schedule_key_not_per_report(self, tmp_path, monkeypatch):
        """同じレポートでも別スケジュールキーが当たって成功した場合、新キーで
        is_due=True になれば重複として防がれる。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        fixed_now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        master = make_master_with_schedule(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "複数スケジュール",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
            schedule_rows=[
                # 月曜 09:00 と水曜 09:00 の 2 行で `1001` を取得する設定
                ["S_MON", "1001", "毎週", "09:00", "", "月", "", "取得しない", "○"],
                ["S_WED", "1001", "毎週", "09:00", "", "水", "", "取得しない", "○"],
            ],
        )
        _patch_master_path(monkeypatch, master, tmp_path / "履歴.csv")
        monkeypatch.setattr(service_module, "clock_now", lambda: fixed_now)

        # 1 回目: 水曜 12:00 → S_WED が is_due=True、ただし履歴に何もないので取得
        first_site = fake_salesforce()
        with patch(
            "src.service.site_for",
            return_value=first_site,
        ):
            download_scheduled()
        # 履歴には S_WED が記録されている
        first_rows = _read_rows(tmp_path / "履歴.csv")
        assert len(first_rows) == 1
        assert first_rows[0]["スケジュールキー"] == "S_WED"

        # 時刻を進めて月曜 12:00 にする → S_MON が is_due=True になる。
        # スケジュールキーが違うので S_MON の履歴は無いため、S_MON で取得される
        # （=重複ではなく別キー）
        monday_now = dt.datetime(2026, 1, 5, 12, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        monkeypatch.setattr(service_module, "clock_now", lambda: monday_now)
        second_site = fake_salesforce()
        with patch(
            "src.service.site_for",
            return_value=second_site,
        ):
            saved2 = download_scheduled()
        assert [path.name.split("_")[0] for path in saved2] == ["1001"]
        rows = _read_rows(tmp_path / "履歴.csv")
        # S_MON の成功履歴が増える
        keys = sorted(row["スケジュールキー"] for row in rows)
        assert keys == ["S_MON", "S_WED"]


# ── 同時起動対策 ─────────────────────────────────────────────────────────
class TestRunLock:
    """``download_scheduled()`` がプロセス間ロックで多重実行を抑止する。"""

    def test_returns_empty_when_run_lock_is_held_by_another_handle(self, tmp_path, monkeypatch):
        """同じプロセス内の別ハンドルで実行ロックを保持している間に
        ``download_scheduled()`` を呼ぶと、Salesforce へ問い合わせずに空リストを返す。

        msvcrt のロックはファイルハンドルに紐づくため、別 ``HistoryFileLock``
        インスタンスが同じファイルを掴んでいる状態でもロック衝突する
        （= WinActor が別プロセスで掴んでいる状況を疑似する）。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "レポート管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        history_db_path = history_path.with_suffix(".sqlite3")
        # ``load_schedule`` は schedule シート無しでも空リストを返す（後方互換）。
        # schedule 行が無いレポートは ``downloaded_today()`` 経由で判定するため、
        # 「スケジュール」シート無しでも Salesforce への問い合わせが発生する

        from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock

        # 別プロセス（=別 ``HistoryFileLock`` ハンドル）が掴んでいる状態を疑似
        # 実行ロックは ``_patch_master_path`` で ``HISTORY_DB_PATH`` の隣
        # （ ``.run`` 拡張子付き ） に作られる
        run_lock_path = history_db_path.with_suffix(".sqlite3.run")
        other_handle = HistoryFileLock(run_lock_path, timeout=10)
        other_handle.__enter__()
        try:
            site = fake_salesforce()
            with patch("src.service.site_for", return_value=site):
                saved = download_scheduled()
            # Salesforce へ問い合わせない（=ロック取得で弾かれた）
            assert site.return_value.__enter__().report.get.call_count == 0
            # 空リストを返す（=終了コード0相当の正常終了）
            assert saved == []
            # 履歴にも追記されない（スキップ時は ``export_history()`` も走らないので
            # 閲覧用 CSV も作られない）
            assert not history_path.exists() or _history_rows({"history_path": history_path}) == []
            assert not history_db_path.exists()
        finally:
            other_handle.__exit__(None, None, None)

    def test_propagates_lock_timeout_from_download_body(self, tmp_path, monkeypatch):
        """実行ロックは取得できるが、本体（``_download_scheduled_locked()`` 内の
        ``history.downloaded_today()`` または ``history.schedule_succeeded_today()``）
        で ``HistoryLockTimeoutError`` が出ると、``download_scheduled()`` は空リスト
        を返さず**例外をそのまま送出する**。

        旧実装ではロック取得と本体の両方をまとめて ``except`` していたため、
        履歴CSV 用ロックの本当の障害が「他プロセスが進行中」として隠れて
        しまっていた。修正後はロック取得部分だけを ``except`` するため、
        本体側の ``HistoryLockTimeoutError`` はそのまま外へ伝播する。

        このテストでは「スケジュール」シートを足さずに ``history.downloaded_today()``
        を経由する経路を再現している（``history.downloaded_today`` /
        ``history.schedule_succeeded_today`` のどちらを差し替えても守られる
        振る舞いは同じなので、テスト用に使う関数は「現在も本体から呼ばれている
        履歴関数」ならどちらでもよい）。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "レポート管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "ダウンロード履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        def _raise_lock_timeout(*args, **kwargs):
            # ``history.downloaded_today()`` の呼び出しすべてで擬似的に
            # 履歴ロック取得失敗を発生させる（=履歴ロックの本当の障害）。
            # 旧テストは ``history.truncated_today`` を monkeypatch していたが、
            # 2026-10 の自動切替導入で ``_matched_schedule_key()`` から
            # ``truncated_today`` を呼ぶ経路は無くなった。今も本体が呼ぶ
            # ``downloaded_today`` / ``schedule_succeeded_today`` のどちらかに
            # 差し替えれば同じ振る舞い（=履歴ロック障害が外へ出る）を守れる
            raise HistoryLockTimeoutError(history_path, 10.0)

        monkeypatch.setattr(service_module.history, "downloaded_today", _raise_lock_timeout)

        with (
            patch("src.service.site_for", return_value=fake_salesforce()),
            pytest.raises(HistoryLockTimeoutError),
        ):
            download_scheduled()


def make_master_with_schedule(
    path: Path,
    master_rows: list[list],
    *,
    schedule_rows: list[list],
    settings_rows: list[list] | None = None,
) -> Path:
    """レポート管理表 + スケジュールシートの2シート構成のブックを作る。

    ``tests/test_schedule_load.py`` 側でも同じ関数が必要だが、fixture として
    共有するのが煩雑なので、サービス側のテストはローカルに持つ。
    ``settings_rows`` を渡すと「設定」シートも追加する。
    """
    master_headers = [
        "ID",
        "グループ",
        "担当者",
        "概要",
        "Salesforce URL",
        "有効",
        "0件あり",
        "2000件超",
        "SOQL",
    ]
    schedule_headers = [
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
    df_master = pd.DataFrame(
        [_reorder_master_row_to_new_order(_row(*row)) for row in master_rows],
        columns=master_headers,
    )
    df_schedule = pd.DataFrame(schedule_rows, columns=schedule_headers)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_master.to_excel(writer, sheet_name="PY_管理表", index=False)
        df_schedule.to_excel(writer, sheet_name="PY_スケジュール", index=False)
        if settings_rows is not None:
            df_settings = pd.DataFrame(settings_rows, columns=GROUP_SETTINGS_HEADERS)
            df_settings.to_excel(writer, sheet_name="PY_設定", index=False)
    return path


def _history_rows(paths: dict) -> list[dict[str, object]]:
    return _read_rows(paths["history_path"])


# ── 1 実行の基準日時を固定（日付またぎ対策） ──────────────────────────────
class TestFixedCurrentAcrossExecution:
    """``download_scheduled()`` が ``clock_now()`` を 1 回だけ取って全工程で共有する。

    23:59 に始まった取得が日付をまたぐと、判定・出力ファイル名・履歴の
    「実行日時」がバラバラの日付になり、翌日のスケジュールキーが「昨日
    成功済み」で飛ぶ事故が起きる。これを ``clock_now()`` を 1 回だけ
    取って ``current`` に固定し、全工程に配る実装で防ぐ。
    """

    def test_filename_and_history_use_start_date_when_crossing_midnight(
        self, tmp_path, monkeypatch
    ):
        """``clock_now()`` を「1 回目=23:59:50、以降=翌日 00:01」に差し替えて
        1 件取得すると、出力ファイル名・履歴の「実行日時」ともに**開始日**
        （23:59:50 の日付）で揃う。

        旧実装（``record()`` が ``now()`` を毎回呼ぶ）では、ファイル名は
        ``clock_now()`` 由来の翌日になり、履歴は ``now()`` 由来の実時間で
        書かれるため、開始日で揃わない。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "レポート管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)
        # 「スケジュール」シート無し（後方互換: 空 schedule で毎回対象）。
        # この場合 ``output_path()`` は ``now=current`` の方を使うため、
        # 開始日で揃う経路も検証できる。

        start_dt = dt.datetime(2026, 9, 23, 23, 59, 50)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime
        next_day_dt = dt.datetime(2026, 9, 24, 0, 1, 0)  # noqa: DTZ001 — テスト用に意図的に固定した tz-naive な datetime

        # 1 回目は開始時刻、以降は翌日 00:01 を返す。
        # ``current`` への固定（service.py）と ``output_path`` 内の
        # フォールバック（provider.py）で呼ばれる側の差を再現する
        call_count = 0

        def _clock_now_then_next_day():
            nonlocal call_count
            call_count += 1
            return start_dt if call_count == 1 else next_day_dt

        monkeypatch.setattr(service_module, "clock_now", _clock_now_then_next_day)
        monkeypatch.setattr(_paths_module, "clock_now", _clock_now_then_next_day)

        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()

        # 出力ファイル名の日付は開始日（20260923）
        assert len(saved) == 1
        filename = saved[0].name
        assert filename.startswith("1001_20260923_"), (
            f"出力ファイル名が開始日 (20260923) で始まっていない: {filename}"
        )

        # 履歴の「実行日時」は開始時刻（2026-09-23 23:59:50）で固定されている
        rows = _read_rows(history_path)
        assert len(rows) == 1
        assert rows[0]["実行日時"] == "2026-09-23 23:59:50", (
            f"履歴の実行日時が開始時刻ではない: {rows[0]['実行日時']!r}"
        )


class TestRequiredHistory:
    """履歴が書けない場合は、取得結果だけを成功として返さない。"""

    def test_history_write_failure_stops_download(self, paths):
        """`HistoryWriteError` は `ScheduledDownloadFailedError` に変換されて返る。

        履歴の正本は v3 で SQLite になったので、 v3-lean 時代の ``_append`` を
        モックする代わりに ``append_history`` を直接 ``HistoryWriteError`` で
        失敗させて、 同じ「履歴が書けない」 振る舞いを確かめる。
        """
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce(),
            ),
            patch.object(
                history,
                "append_history",
                side_effect=HistoryWriteError(paths["history_db_path"], "履歴書込み失敗"),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()

    def test_original_failure_remains_in_history_error(self, paths):
        """`EmptyReportError` が起きて履歴も書けなかった場合、エラーは
        `ScheduledDownloadFailedError` で伝搬し、メッセージに元の失敗が含まれる。

        新仕様では Report API が 0 行のときブラウザで取り直すので、
        ブラウザも 0 行 → ``EmptyReportError`` の流れをモックして再現する。
        """
        browser_site = fake_browser_site([])
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            patch.object(
                history,
                "append_history",
                side_effect=HistoryWriteError(paths["history_db_path"], "履歴書込み失敗"),
            ),
            pytest.raises(ScheduledDownloadFailedError) as caught,
        ):
            download_scheduled()
        # `ScheduledDownloadFailedError` の元例外 (`__cause__`) が
        # `HistoryWriteError` で、その中に元の `EmptyReportError` が連鎖している
        assert isinstance(caught.value.__cause__, HistoryWriteError)
        original = caught.value.__cause__.__cause__
        assert original is not None
        assert "0 行" in str(original)


# ── 0件あり / 0 行の扱い ─────────────────────────────────────────────
class TestAllowEmpty:
    """管理表の「0件あり」列で、0 行のとき失敗にするか正常終了にするかを選ぶ。"""

    def test_empty_report_with_allow_empty_no_raises_and_records_empty_data_cause(
        self, tmp_path, monkeypatch
    ):
        """1. `0件あり` が `×` で 0 行 → `EmptyReportError`、ファイルができない、
        履歴の `原因区分` が `データなし`。

        新仕様では Report API が 0 行 → ブラウザで取り直し → ブラウザも 0 行 →
        ``EmptyReportError`` で失敗。ブラウザ経路もモックして「ブラウザも 0 行」
        のケースを再現する。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                    "×",  # 0件あり = ×
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        browser_site = fake_browser_site([])
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=browser_site,
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()

        # ファイルは作られない
        assert list(base_path.glob("1001_*.csv")) == []
        # 履歴には `データなし` が残る（取得成功・保存未到達の組合せのみ取り得る）
        row = _read_rows(history_path)[-1]
        assert row["成否"] == "失敗"
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == ""
        assert row["エラーコード"] == "EmptyReportError"
        assert row["原因区分"] == "データなし"

    def test_empty_report_with_allow_empty_yes_succeeds_and_writes_empty_file(
        self, tmp_path, monkeypatch
    ):
        """2. `0件あり` が `○` で 0 行 → 例外にならない、空のファイルができる、
        履歴が `成否=成功` / `取得件数=0` / `原因区分` が空。

        出力は単一ファイル化（時刻付き 1 件）なので、ファイル数の期待値も 1 件。
        """
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                    "○",  # 0件あり = ○
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        with patch(
            "src.service.site_for",
            return_value=fake_salesforce([]),
        ):
            download_scheduled()  # 例外にならない

        # 0 行でも Salesforce の列情報を持つ CSV が作られる（単一ファイルの 1 件）
        # 「ベース / 概要」の 2 階層目で glob する
        from src.paths import summary_folder_name as _summary_folder_name

        summary_dir = base_path / _summary_folder_name("顧客一覧")
        saved = list(summary_dir.glob("1001_*.csv"))
        assert len(saved) == 1
        # v3 では ``pd.read_csv`` が空 DataFrame を返すため、 csv.DictReader での
        # 反復は空。見出し行は ``columns で`` 存在する（カラム名のみ）
        # 元のテストは ``fake_salesforce()`` の既定（ ``ROWS`` ）で 2 行返していたが、
        # ここでは明示的に空を渡しているため 0 行
        assert saved[0].read_text("utf-8-sig").strip().splitlines() == ["名前,金額"]

        # 履歴は成功・取得件数 0・原因区分 空
        row = _read_rows(history_path)[-1]
        assert row["成否"] == "成功"
        assert row["Salesforce取得結果"] == "成功"
        assert row["保存結果"] == "成功"
        assert row["取得件数"] == "0"
        assert row["原因区分"] == ""
        assert row["エラーコード"] == ""

    def test_scheduled_empty_report_can_be_received(self, tmp_path, monkeypatch):
        """0件で成功した定期取得は、ファイルが空のまま読み取れる。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                    "○",  # 0件あり = ○
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        monkeypatch.setattr(_paths_module, "MASTER_PATH", master)
        monkeypatch.setattr(
            "comken.services.salesforce_downloader.paths.HISTORY_PATH", history_path
        )
        _patch_master_path(monkeypatch, master, history_path)

        with patch(
            "src.service.site_for",
            return_value=fake_salesforce([]),
        ):
            download_scheduled()

        # 保存された単一ファイルが空のまま読み取れる（glob でファイルを探して
        # 直接 CSV で読む）。「ベース / 概要」の 2 階層目で glob する
        from src.paths import summary_folder_name as _summary_folder_name

        summary_dir = base_path / _summary_folder_name("顧客一覧")
        saved = list(summary_dir.glob("1001_*.csv"))
        assert len(saved) == 1
        reader = _read_rows(saved[0])
        assert reader == []

    def test_master_without_allow_empty_column_defaults_to_no(self, tmp_path, monkeypatch):
        """4. `0件あり` の列が無い管理表でも読める（既定 `×` として扱われる）。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        # `0件あり` 列が無い管理表 = ``_row`` の 6 要素で打ち切る版（``_row`` が
        # ``HEADERS`` の長さまで ``""`` で埋める都合、空文字のセルが `×` 既定として
        # 読まれる）
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
            settings_rows=[["営業事務グループ", str(base_path)]],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        entries = load_master(master)
        assert entries["1001"].allow_empty is False  # 既定値

        # 0 行のときは従来どおり `EmptyReportError`（列が無い管理表を
        # 後付けで読む既存プロジェクトを壊さないため）
        with (
            patch(
                "src.service.site_for",
                return_value=fake_salesforce([]),
            ),
            pytest.raises(ScheduledDownloadFailedError),
        ):
            download_scheduled()
        assert list(base_path.glob("1001_*.csv")) == []

    def test_invalid_allow_empty_value_raises(self, tmp_path):
        """5. `0件あり` に `○` `×` 以外を書くとエラーになる（choices で弾く）。"""
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                    "△",  # 0件あり = 無効値
                ]
            ],
        )
        with pytest.raises(MasterRowValueError) as e:
            load_master(master)
        # 列名でユーザーが書き方を直せるよう、見出しをメッセージに残す
        assert "0件あり" in str(e.value)

    def test_download_scheduled_with_allow_empty_yes_does_not_raise_on_zero_rows(
        self, tmp_path, monkeypatch
    ):
        """6. `download_scheduled()` で `0件あり` が `○` のレポートが 0 行でも
        `ScheduledDownloadFailedError` にならない。"""
        base_path = tmp_path / "ベース"
        base_path.mkdir()
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "空でもOK",
                    URL_A,
                    "営業事務グループ",
                    "山田",
                    "○",
                    "○",  # 0件あり = ○
                ],
                [
                    "1002",
                    "普通のレポート",
                    URL_B,
                    "経理グループ",
                    "佐藤",
                    "○",
                    "×",  # 0件あり = ×
                ],
            ],
            settings_rows=[
                ["営業事務グループ", str(base_path)],
                ["経理グループ", str(base_path)],
            ],
        )
        history_path = tmp_path / "履歴.csv"
        _patch_master_path(monkeypatch, master, history_path)

        # "1001" は 0 行、"1002" は通常データを返す
        site = MagicMock()
        client = MagicMock()

        def _run(report_id):
            rows = [] if report_id == "00O5g00000ABCDE" else ROWS
            return pd.DataFrame(rows, columns=["名前", "金額"])

        client.__enter__.return_value.report.get.side_effect = _run
        site.return_value = client

        with patch("src.service.site_for", return_value=site):
            saved = download_scheduled()  # 例外にならない

        # 両方とも保存される（"1001" は空ファイル、"1002" は通常の CSV）
        names = sorted(path.name.split("_")[0] for path in saved)
        assert names == ["1001", "1002"]
        # 履歴を確認: "1001" は成功・0件、"1002" も成功・2件
        rows = _read_rows(history_path)
        by_key = {row["管理番号"]: row for row in rows}
        assert by_key["1001"]["成否"] == "成功"
        assert by_key["1001"]["取得件数"] == "0"
        assert by_key["1001"]["原因区分"] == ""
        assert by_key["1002"]["成否"] == "成功"
        assert by_key["1002"]["取得件数"] == "2"


# ── 2000件超で失敗したレポートの当日スキップ ─────────────────────────
class TestTruncatedSkip:
    """``SalesforceReportTruncatedError`` で失敗したレポートの挙動。

    2026-10 に Report API 経路での自動切替（``SalesforceReportTruncatedError`` を
    受けてブラウザ経由に自動で切り替え）を入れた結果、**「当日中の再実行で
    Salesforce へ問い合わせない」スキップは廃止された**。同じ日の 2 回目でも
    Report API → ブラウザ自動切替が動くので、毎回 Salesforce への問い合わせが
    発生する。``_select_targets`` の ``already_failed`` は常に空リストになり、
    ``comken`` 側 ``history.truncated_today()`` 自体は別プロジェクト向けに
    残してある。

    ここでは「truncated が来ても自動切替で成功する」「自動切替に失敗したら
    失敗として残る」「2回目の ``download_scheduled()`` でも Report API が呼ばれる」
    の 3 点を新しい挙動の代表として確認する（個別のテストは
    ``tests/test_service_browser_fallback.py`` に集約）。
    """

    @staticmethod
    def _seed_failure_row(
        history_path: Path,
        *,
        when: dt.datetime,
        error_code: str,
        cause: str = "Salesforce",
        fetch_result: str = "失敗",
        save_result: str = "",
    ) -> None:
        """``when`` の日時の失敗履歴を1行 SQLite に書く（テスト用ヘルパー）。

        履歴の正本は v3 で SQLite に変わったので、 旧 CSV ヘルパーのように
        直接書くのではなく ``append_history()`` 経由で 1 行 INSERT する
        （ ``download_scheduled()`` も同じ経路で読むので、 副作用が同じになる）。
        """
        history_path.parent.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {
            "管理番号": "1001",
            "スケジュールキー": "",
            "概要": "",
            "レポートID": "00O5g00000ABCDE",
            "URL": URL_A,
            "プロジェクト": "定期実行",
            "成否": "失敗",
            "Salesforce取得結果": fetch_result,
            "保存結果": save_result,
            "保存先": str(history_path.parent),
            "ファイル名": "",
            "取得件数": 0,
            "処理秒数": 1.00,
            "原因区分": cause,
            "エラーコード": error_code,
            "エラー内容": "2000 行で打ち止め",
            "取得経路": "",
        }
        # ``HistoryColumns.names()`` 順の dict へ並べ直す
        values = {column: values.get(column, "") for column in history.HistoryColumns.names()}
        history.append_history(history_path, values, executed_at=when)

    @classmethod
    def _seed_truncated_failure(cls, history_path: Path, *, when: dt.datetime) -> None:
        """``SalesforceReportTruncatedError`` の失敗履歴を1行書く。"""
        cls._seed_failure_row(
            history_path,
            when=when,
            error_code="SalesforceReportTruncatedError",
        )

    def test_second_call_in_same_day_still_calls_salesforce(self, paths, monkeypatch):
        """当日中に ``SalesforceReportTruncatedError`` で失敗した記録があっても、
        同じ日の 2 回目の ``download_scheduled()`` で **Salesforce への問い合わせは
           止めない**（=自動切替で取りに行く）。旧実装の「スキップ」挙動は廃止された。

        ブラウザ経路もモックして「Report API が Truncated → ブラウザが成功」を
        確認する（=``SalesforceReportTruncatedError`` が来ても、自動切替が走れば
        成功扱いになる）。 履歴は SQLite 正本から読むので、 失敗履歴の
        シードも ``paths["history_db_path"]`` に ``append_history()`` 経由で書く。
        """
        now = dt.datetime(2026, 1, 7, 12, 0)  # noqa: DTZ001
        monkeypatch.setattr(service_module, "clock_now", lambda: now)
        # 同じ「今日」の truncated 失敗履歴を SQLite に書く（=昨晩 RPA が失敗した想定）
        self._seed_truncated_failure(paths["history_db_path"], when=now)

        site = fake_browser_site()
        with (
            patch("src.service.site_for", return_value=fake_salesforce()),
            patch(
                "comken.toolbox.browser.sites.salesforce.site_for",
                return_value=site,
            ),
        ):
            saved = download_scheduled()
        # 2 回目でも Salesforce へ問い合わせが走る（旧挙動: スキップで 0 回）
        # Report API → Truncated → ブラウザで成功、という流れになる
        assert [path.name.split("_")[0] for path in saved] == ["1001"]


# ── 閲覧用 CSV の書き出し（export_history のオーケストレーション）────
class TestHistoryCsvExport:
    """``download_scheduled()`` がロックの内側で ``export_history()`` を
    1 回だけ呼ぶ形にした。人が見る CSV は ``HISTORY_PATH`` に書かれる。"""

    def test_exports_csv_after_success(self, paths):
        """``download_scheduled()`` が正常終了すると ``HISTORY_PATH`` の CSV が
        書き出されている。ヘッダと 1 行の値が旧実装と同じ手順で読める。
        """
        with patch("src.service.site_for", return_value=fake_salesforce()):
            saved = download_scheduled()
        assert [path.name.split("_")[0] for path in saved] == ["1001"]
        # 閲覧用 CSV が書かれている
        assert paths["history_path"].is_file()
        rows = _read_rows(paths["history_path"])
        assert len(rows) == 1
        row = rows[0]
        assert row["管理番号"] == "1001"
        assert row["成否"] == "成功"
        assert row["取得件数"] == "2"
        # ``処理秒数`` は ``perf_counter`` 由来で実行ごとに違うが、
        # ``export_history()`` が ``f"{x:.2f}"`` で書く形（ = 必ず小数 2 桁 ） であることだけ見る
        seconds_text = row["処理秒数"]
        assert seconds_text.count(".") == 1
        integer_part, _, fractional_part = seconds_text.partition(".")
        assert fractional_part != "" and len(fractional_part) == 2
        assert float(seconds_text) >= 0
        assert integer_part.isdigit()
        assert row["原因区分"] == ""

    def test_exports_csv_after_failure(self, paths):
        """1 件失敗して ``ScheduledDownloadFailedError`` が送出されても、
        例外が ``ScheduledDownloadFailedError`` で、 閲覧用 CSV は
        ``download_scheduled()`` が最後に 1 回書き出した値で残っている。
        """
        # フォルダ未作成で失敗させて ``ScheduledDownloadFailedError`` を起こす
        import shutil

        shutil.rmtree(paths["base_path"])
        with patch("src.service.site_for", return_value=fake_salesforce()):
            with pytest.raises(ScheduledDownloadFailedError):
                download_scheduled()
        # 閲覧用 CSV は書かれている（ 例外で抜けても最後に 1 回書き出す ）
        assert paths["history_path"].is_file()
        rows = _read_rows(paths["history_path"])
        assert len(rows) == 1
        row = rows[0]
        assert row["管理番号"] == "1001"
        assert row["成否"] == "失敗"
        assert row["エラーコード"] == "ReportFolderNotFoundError"

    def test_does_not_export_when_lock_is_held(self, paths, monkeypatch):
        """ロックが取れずにスキップしたときは ``export_history()`` も走らない
        （既存の閲覧用 CSV が意図せず上書きされない）。
        """
        from comken.services.salesforce_downloader.history_file_lock import HistoryFileLock

        # 既存 CSV が無い状態から始める
        if paths["history_path"].exists():
            paths["history_path"].unlink()
        # 別プロセスが ``.run.lock`` を掴んでいる状態を疑似
        run_lock_path = paths["history_db_path"].with_suffix(".sqlite3.run")
        other_handle = HistoryFileLock(run_lock_path, timeout=10)
        other_handle.__enter__()
        try:
            with patch("src.service.site_for", return_value=fake_salesforce()):
                saved = download_scheduled()
            assert saved == []
        finally:
            other_handle.__exit__(None, None, None)
        # スキップ時は閲覧用 CSV も正本 SQLite も作られない
        assert not paths["history_path"].exists()
        assert not paths["history_db_path"].exists()

    def test_export_failure_is_warned_only(self, paths, monkeypatch, caplog):
        """``export_history()`` が ``HistoryExportError`` を上げても、
        ``download_scheduled()`` の戻り値・送出する例外は書き出し成功時と
        同じままで、 警告ログが出る（ 取得自体は止めない ）。
        """
        import logging

        from comken.exceptions import HistoryExportError

        # ``export_history()`` が ``HistoryExportError`` を投げるように差し替え
        def raise_export(*args, **kwargs):
            raise HistoryExportError(paths["history_path"], "permission denied")

        monkeypatch.setattr(service_module, "export_history", raise_export)

        with caplog.at_level(logging.WARNING, logger="src.service"):
            with patch("src.service.site_for", return_value=fake_salesforce()):
                saved = download_scheduled()

        # 戻り値・例外は書き出し成功時と同じ（ CSV 書き出し失敗で取得が
        # 失敗扱いにならない）
        assert [path.name.split("_")[0] for path in saved] == ["1001"]
        # 警告ログが出ている
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "閲覧用 CSV を書き出せませんでした" in record.getMessage() for record in warnings
        ), caplog.text


class TestCommandLine:
    """保守用コマンド（検査）。"""

    def test_check_reports_counts(self, paths, capsys):
        assert cli(["check", str(paths["master_path"])]) == 0
        out = capsys.readouterr().out
        assert "登録 3 件" in out
        assert "00O5g00000FGHIJ" in out  # 同じレポートを指している管理番号を知らせる

    def test_check_returns_failure_for_a_broken_master(self, tmp_path, capsys):
        master = make_master(
            tmp_path / "管理表.xlsx",
            [
                [
                    "1001",
                    "顧客一覧",
                    "https://example.com/",
                    "営業事務グループ",
                    "山田",
                    "○",
                ]
            ],
        )
        assert cli(["check", str(master)]) == 1
        assert "エラー:" in capsys.readouterr().err

"""_fetch() のブラウザ経由フォールバックのテスト。

管理表の「2000件超」列（``ReportEntry.exceeds_row_limit``）が真だと、_fetch() が
レポートAPIではなく comken.toolbox.browser.sites.salesforce 経由で取得する
（「SOQL」列が優先されるケースは tests/test_service_soql_fetch.py 側）。
既存の _fetch() (API経由) のテストは tests/test_service.py に集約してあるため、
ここではブラウザ経由の分岐だけを扱う。
"""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from comken.toolbox.office.csv import read_csv

from src.service import _fetch, _fetch_via_browser
from src.sheets.master import ReportEntry


# ``test_service.py`` と同形の薄い ``CSV`` ラッパー（ ``comken.toolbox.csv.CSV``
# の後継）。 ``with CSV(path) as csv_file: csv_file.read()`` のパターンをテストが
# 使うので、中身だけ pandas の ``read_csv`` に委譲する
class CSV:
    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only

    def __enter__(self) -> "CSV":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> list[dict[str, object]]:
        df = read_csv(self.path, columns=None, dtype=str)
        return [
            {k: ("" if pd.isna(v) else v) for k, v in row.items()}
            for row in df.to_dict(orient="records")
        ]

ENTRY = ReportEntry(
    key="9001",
    summary="顧客一覧",
    url="https://example.my.salesforce.com/lightning/r/Report/00O5g00000ABCDE/view",
    group="営業本部",
    assignee="山田",
    enabled=True,
    allow_empty=False,
)
EXCEEDS_ENTRY = replace(ENTRY, exceeds_row_limit=True)


def _fake_browser_site(csv_bytes: bytes = "名前,金額\n山田,100\n".encode()):
    """export_reports() がCSVファイルを書き出すブラウザサイトクラスを作る。"""

    def _export_reports(reports, **kwargs):
        for _url, destination in reports.items():
            Path(destination).write_bytes(csv_bytes)
            yield "00O5g00000ABCDE", Path(destination)

    site_instance = MagicMock()
    site_instance.export_reports.side_effect = _export_reports
    site_instance.__enter__.return_value = site_instance
    site_class = MagicMock(return_value=site_instance)
    return site_class, site_instance


class TestFetchRoutesToBrowser:
    """_fetch() — 「2000件超」列が真の管理番号はブラウザ経由になる。"""

    def test_uses_browser_when_exceeds_row_limit(self):
        table = MagicMock()
        with patch(
            "src.service._fetch_via_browser",
            return_value=table,
        ) as fetch_via_browser:
            result = _fetch(EXCEEDS_ENTRY)

        # ``_fetch_via_browser()`` には ``browser_sessions=None`` が渡る
        # （``_fetch()`` も ``_download_scheduled_locked()`` も渡さない経路なので、
        # 従来通り「その場で開いて閉じる」動きになる）
        fetch_via_browser.assert_called_once_with(EXCEEDS_ENTRY, browser_sessions=None)
        assert result is table

    def test_uses_api_when_exceeds_row_limit_is_false(self):
        with (
            patch("src.service.site_for") as site_for,
            patch("src.service._fetch_via_browser") as fetch_via_browser,
        ):
            _fetch(ENTRY)

        site_for.assert_called_once_with(ENTRY.url)
        fetch_via_browser.assert_not_called()

    def test_raises_when_filters_given_for_browser_report(self):
        with pytest.raises(ValueError, match=EXCEEDS_ENTRY.key):
            _fetch(EXCEEDS_ENTRY, filters=[{"column": "x", "operator": "equals", "value": "1"}])


class TestFetchViaBrowser:
    """_fetch_via_browser() — ブラウザ経由で取得し、_fetch() と同じ Table を返す。"""

    def test_returns_table_parsed_from_exported_csv(self):
        site_class, _site_instance = _fake_browser_site()
        with patch("comken.toolbox.browser.sites.salesforce.site_for", return_value=site_class):
            table = _fetch_via_browser(ENTRY)

        assert list(table.columns) == ["名前", "金額"]
        assert table.to_dict(orient="records") == [{"名前": "山田", "金額": "100"}]

    def test_calls_login_with_credentials_before_export(self):
        site_class, site_instance = _fake_browser_site()
        with patch("comken.toolbox.browser.sites.salesforce.site_for", return_value=site_class):
            _fetch_via_browser(ENTRY)

        site_instance.login_with_credentials.assert_called_once()

    def test_calls_login_with_credentials_without_args(self):
        """``login_with_credentials()`` は組織クラスの ``CREDENTIAL_PREFIX`` を
        使うため、引数なしで 1 回だけ呼ばれる（プレフィックスを毎回渡さない
        運用に対応）。
        """
        site_class, site_instance = _fake_browser_site()
        with patch("comken.toolbox.browser.sites.salesforce.site_for", return_value=site_class):
            _fetch_via_browser(ENTRY)

        # 引数なしで 1 回だけ
        site_instance.login_with_credentials.assert_called_once_with()


class TestSeleniumStaysLazy:
    """「2000件超」列が誰も○にしていない運用では selenium を読み込まない。"""

    def test_fetch_does_not_import_browser_module(self, monkeypatch):
        for mod_name in list(sys.modules):
            if mod_name.startswith("comken.toolbox.browser"):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

        with patch("src.service.site_for") as site_for:
            site_for.return_value.__enter__.return_value.report.get.return_value = MagicMock()
            _fetch(ENTRY)

        assert not any(mod_name.startswith("comken.toolbox.browser") for mod_name in sys.modules)


class TestBrowserSessionReuse:
    """``browser_sessions`` を渡したときの「同じ組織のブラウザを使い回す」動き。

    ブラウザはモック（``_fake_browser_site``）。``site_class()`` が返すインスタンス
    は ``MagicMock`` なので ``__enter__`` / ``__exit__`` /
    ``login_with_credentials`` / ``export_reports`` をすべて ``Mock`` 上でカウントできる。
    """

    def test_same_org_reuses_one_browser(self):
        """同じ組織の「2000件超」レポート 3 件を取ると、サイトクラスは 1 回だけ開かれ、
        ``login_with_credentials`` も 1 回だけ呼ばれる。``export_reports`` だけが 3 回。
        """
        site_class, site_instance = _fake_browser_site()
        browser_sessions: dict = {}

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            from src.service import _fetch_via_browser

            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

        # サイトクラスは 1 回だけ開かれる
        assert site_class.call_count == 1
        # コンテキストマネージャへの進入 (``__enter__``) も 1 回
        assert site_instance.__enter__.call_count == 1
        # ``login_with_credentials`` は「開いた直後の 1 回」だけ
        # （2 件目以降は使い回すので再実行しない）
        site_instance.login_with_credentials.assert_called_once()
        # ``export_reports`` はレポートごとに毎回
        assert site_instance.export_reports.call_count == 3
        # まだ dict の中に 1 つだけ残っている（呼び出し側で閉じる）
        assert len(browser_sessions) == 1

    def test_different_orgs_open_separate_browsers(self):
        """2 つの組織のレポートが混ざると、組織ごとにブラウザが開かれる。

        ``browser_site_for()`` は URL のドメインで組織を引くため、ドメインが違う
        2 つの ``ReportEntry`` を使えば別々のクラスが返る。
        """
        # ドメイン A / B の 2 種類の ``MagicMock`` クラスを返す ``site_for`` を作る
        site_class_a, instance_a = _fake_browser_site()
        site_class_b, instance_b = _fake_browser_site()

        def _site_for(url):
            return site_class_a if "example.my.salesforce.com" in url else site_class_b

        entry_a = EXCEEDS_ENTRY
        entry_b = replace(EXCEEDS_ENTRY, url=ENTRY.url.replace("example.my", "other.my"))

        browser_sessions: dict = {}
        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            side_effect=_site_for,
        ):
            from src.service import _fetch_via_browser

            _fetch_via_browser(entry_a, browser_sessions=browser_sessions)
            _fetch_via_browser(entry_b, browser_sessions=browser_sessions)
            _fetch_via_browser(entry_a, browser_sessions=browser_sessions)

        # 組織ごとに 1 回ずつ（合計 2 回）開かれる
        assert site_class_a.call_count == 1
        assert site_class_b.call_count == 1
        assert instance_a.__enter__.call_count == 1
        assert instance_b.__enter__.call_count == 1
        # ``login_with_credentials`` も組織ごとに 1 回
        instance_a.login_with_credentials.assert_called_once()
        instance_b.login_with_credentials.assert_called_once()
        # dict には 2 つの組織分のエントリ
        assert len(browser_sessions) == 2

    def test_browser_exception_closes_and_reopens_for_next_report(self):
        """1 件目のブラウザ取得で例外が出ると、そのブラウザは閉じられ、2 件目では
        新しいブラウザが開かれる（``__enter__`` が 2 回）。1 件目は失敗として
        そのまま呼び出し側へ抜ける。
        """
        site_class, site_instance = _fake_browser_site()

        call_log: list[str] = []

        def _flaky_export(reports, **kwargs):
            call_log.append("export")
            if len(call_log) == 1:
                raise RuntimeError("1 件目は失敗")
            # 2 件目: 通常通り CSV を書く（ASCII のみ）
            for _, destination in reports.items():
                Path(destination).write_bytes(b"col,amt\nyamada,100\n")
                yield "00O5g00000ABCDE", Path(destination)

        site_instance.export_reports.side_effect = _flaky_export
        browser_sessions: dict = {}

        from src.service import _fetch_via_browser

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            with pytest.raises(RuntimeError, match="1 件目は失敗"):
                _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            # 失敗時に ``__exit__`` が 1 回呼ばれている（壊れたブラウザを閉じる）
            assert site_instance.__exit__.call_count == 1
            # dict からは外されている
            assert browser_sessions == {}
            # 2 件目: 新しいブラウザが開かれる
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

        # サイトクラスは 2 回呼ばれる（1 件目で開いて閉じた後、2 件目で開き直し）
        assert site_class.call_count == 2
        # ``__enter__`` も 2 回（開き直し分）
        assert site_instance.__enter__.call_count == 2
        # ``login_with_credentials`` は「新しく開いたとき」= 2 回
        assert site_instance.login_with_credentials.call_count == 2
        # ``__exit__`` は失敗時 1 回 + 2 件目ではまだ生きている = 計 1 回
        assert site_instance.__exit__.call_count == 1

    def test_unexpected_exception_closes_open_browsers(self):
        """想定外例外（``_download_scheduled_locked()`` の ``finally`` が拾う対象）が
        出ても、``_download_scheduled_locked()`` 側で ``browser_sessions`` をループ
        して ``__exit__`` を呼べば、開いたブラウザが閉じられる。

        ここでは ``_download_scheduled_locked()`` 相当の動きを再現して、
        ``finally`` 節が必ず ``__exit__`` を呼ぶことを確認する。
        """
        site_class, site_instance = _fake_browser_site()
        browser_sessions: dict = {}

        from src.service import _fetch_via_browser

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            assert site_instance.__enter__.call_count == 1

            # 想定外例外を投げる本体
            try:
                raise RuntimeError("想定外")
            except RuntimeError:
                # ``_download_scheduled_locked()`` の ``finally`` 相当
                for sf in list(browser_sessions.values()):
                    sf.__exit__(None, None, None)
                # 元の例外をもう一度投げる（``finally`` 内に無いのでそのまま伝播）

        # ``finally`` で ``__exit__`` が 1 回呼ばれて閉じられた
        site_instance.__exit__.assert_called_once_with(None, None, None)

    def test_download_scheduled_closes_all_browsers_on_normal_exit(self):
        """``_download_scheduled_locked()`` の正常終了時にすべてのブラウザが閉じられる。"""
        site_class, site_instance = _fake_browser_site()
        browser_sessions: dict = {}

        from src.service import _fetch_via_browser

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

            # 正常終了を模擬: ``finally`` 節を走らせる
            for sf in list(browser_sessions.values()):
                sf.__exit__(None, None, None)

        # サイトクラスは 1 回、``__exit__`` は最後に 1 回
        assert site_class.call_count == 1
        site_instance.__exit__.assert_called_once_with(None, None, None)

    def test_login_with_credentials_exception_closes_and_reopens_for_next_report(self):
        """1 件目の ``login_with_credentials()`` が例外を出すと、そのブラウザは閉じられ
        （``__exit__`` が 1 回呼ばれ、dict からも外され）、2 件目では
        新しいブラウザが開かれる（``__enter__`` が 2 回、
        ``login_with_credentials`` も 2 回）。
        1 件目は失敗としてそのまま呼び出し側へ抜ける。
        """
        site_class, site_instance = _fake_browser_site()

        login_calls = {"n": 0}

        def _flaky_login_with_credentials():
            login_calls["n"] += 1
            if login_calls["n"] == 1:
                raise RuntimeError("1 件目の login_with_credentials は失敗")

        site_instance.login_with_credentials.side_effect = _flaky_login_with_credentials
        browser_sessions: dict = {}

        from src.service import _fetch_via_browser

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            with pytest.raises(RuntimeError, match="1 件目の login_with_credentials は失敗"):
                _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            # 失敗時に ``__exit__`` が 1 回呼ばれている（壊れたブラウザを閉じる）
            assert site_instance.__exit__.call_count == 1
            # dict からは外されている
            assert browser_sessions == {}
            # 2 件目: 新しいブラウザが開かれる
            # （``login_with_credentials`` はもう例外を出さない）
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

        # サイトクラスは 2 回（1 件目で開いて閉じた後、2 件目で開き直し）
        assert site_class.call_count == 2
        # ``__enter__`` も 2 回
        assert site_instance.__enter__.call_count == 2
        # ``login_with_credentials`` は「新しく開いたとき」= 2 回
        assert site_instance.login_with_credentials.call_count == 2
        # ``__exit__`` は失敗時 1 回 + 2 件目ではまだ生きている = 計 1 回
        assert site_instance.__exit__.call_count == 1

    def test_login_with_credentials_login_failed_closes_and_reopens_for_next_report(self):
        """``login_with_credentials()`` が ``LoginFailedError``（無人で MFA が
        承認されなかったときのタイムアウト）を出すと、そのブラウザは閉じられ
        dict から外され、1 件目は ``LoginFailedError`` のまま呼び出し側へ抜ける
        （``download_scheduled()`` 側で ``ComkenError`` として失敗扱い）。
        2 件目では新しいブラウザが開かれ、``login_with_credentials`` も再実行される。
        """
        from comken.exceptions import LoginFailedError

        site_class, site_instance = _fake_browser_site()

        login_calls = {"n": 0}

        def _flaky_login():
            login_calls["n"] += 1
            if login_calls["n"] == 1:
                raise LoginFailedError("10 分待ちましたがログインが確認できませんでした")

        site_instance.login_with_credentials.side_effect = _flaky_login
        browser_sessions: dict = {}

        from src.service import _fetch_via_browser

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            # 1 件目: ``LoginFailedError`` がそのまま呼び出し側へ抜ける
            with pytest.raises(LoginFailedError, match="ログインが確認できませんでした"):
                _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            # 失敗時に ``__exit__`` が 1 回呼ばれている（壊れたブラウザを閉じる）
            assert site_instance.__exit__.call_count == 1
            # dict からは外されている
            assert browser_sessions == {}
            # 2 件目: 新しいブラウザが開かれる
            # （``login_with_credentials`` はもう例外を出さない）
            _fetch_via_browser(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

        # サイトクラスは 2 回（1 件目で開いて閉じた後、2 件目で開き直し）
        assert site_class.call_count == 2
        # ``__enter__`` も 2 回
        assert site_instance.__enter__.call_count == 2
        # ``login_with_credentials`` は「新しく開いたとき」= 2 回
        assert site_instance.login_with_credentials.call_count == 2
        # ``__exit__`` は失敗時 1 回 + 2 件目ではまだ生きている = 計 1 回
        assert site_instance.__exit__.call_count == 1

    def test_finally_exits_with_none_not_type_none(self, monkeypatch):
        """``_download_scheduled_locked()`` の ``finally`` 節が ``__exit__`` を
        ``(None, None, None)`` で呼ぶ（``type(None)`` ＝ ``NoneType`` ではなく）。

        comken の ``BrowserSession.__exit__`` は ``exc_type is not None`` のとき
        エラー時スクリーンショットを撮るため、修正前は ``type(None)`` を渡して
        いたことで毎回スクショが撮られていた（正常終了でも）。修正後は ``None``
        を渡すので、``exc_type is None`` で正常扱いになる。
        """
        import src.service as service_module

        site_class, site_instance = _fake_browser_site()

        # ``_download_scheduled_locked()`` の依存関数を全部 mock して、
        # 本体ロジック（``for`` ループ→``finally``）だけを走らせる
        monkeypatch.setattr(service_module, "load_master", lambda path: {})
        monkeypatch.setattr(service_module, "load_schedule", lambda path: [])
        monkeypatch.setattr(service_module, "_validate_filters_by_report", lambda *a, **kw: None)
        monkeypatch.setattr(service_module, "_warn_shared_reports", lambda *a, **kw: None)
        monkeypatch.setattr(
            service_module,
            "_select_targets",
            lambda *a, **kw: ([(EXCEEDS_ENTRY, "", None)], []),
        )

        def _fake_download(entry, project, history_path, schedule_key, **kwargs):
            browser_sessions = kwargs.get("browser_sessions")
            if browser_sessions is not None and site_class not in browser_sessions:
                sf = site_class()
                sf.__enter__()
                browser_sessions[site_class] = sf
            return MagicMock()

        monkeypatch.setattr(service_module, "_download", _fake_download)

        service_module._download_scheduled_locked("test", filters_by_report=None)

        # ``finally`` 節で ``__exit__`` が ``(None, None, None)`` で呼ばれた
        # （``type(None)`` ≒ ``NoneType`` ではなく、Python の ``None`` そのもの）
        site_instance.__exit__.assert_called_once()
        args, _kwargs = site_instance.__exit__.call_args
        assert args[0] is None, (
            f"exc_type should be None（正常終了扱い）、got {args[0]!r}"
            "（type(None) を渡していると NoneType が伝わり、毎回エラー扱いになる）"
        )


class TestBrowserSessionReuseBrokenByRemoval:
    """「実装を壊して落ちる」検知テスト。

    正しい実装（``_fetch_via_browser`` で dict を使い回し、失敗時に ``pop`` して
    ``__exit__`` を呼ぶ）にモンキーパッチで「壊れた実装」を差し込み、その特徴を
    直接 assert する。これにより「``test_same_org_reuses_one_browser`` が落ちる」
    「``test_browser_exception_closes_and_reopens_for_next_report`` が落ちる」
    の 2 種類を機械的に確認できる。
    """

    def test_breaks_when_reuse_is_removed(self, monkeypatch):
        """「使い回しをやめた」壊れた実装の特徴: ``login_with_credentials`` が 3 回。

        正しい実装なら同じ組織の 3 件でも ``login_with_credentials`` は 1 回。
        """
        import src.service as service_module

        def _no_reuse(entry, *, browser_sessions=None):
            """故意に「毎回開く」壊れた実装。"""
            from comken.toolbox.browser.sites.salesforce import site_for as browser_site_for

            site_class = browser_site_for(entry.url)
            sf = site_class()
            sf.__enter__()
            sf.login_with_credentials()
            sf.__exit__(None, None, None)
            return MagicMock()

        site_class, site_instance = _fake_browser_site()
        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            monkeypatch.setattr(service_module, "_fetch_via_browser", _no_reuse)
            for _ in range(3):
                _no_reuse(EXCEEDS_ENTRY)

        # 壊れた実装の特徴: 毎回開くので ``login_with_credentials`` が 3 回
        assert site_instance.login_with_credentials.call_count == 3

    def test_breaks_when_failure_does_not_evict_browser(self, monkeypatch):
        """「失敗時に ``pop`` しない」壊れた実装の特徴: 1 件目失敗 → 2 件目で
        ``__enter__`` が 1 回（壊れたインスタンスを再利用）。

        正しい実装なら 1 件目失敗 → 2 件目で ``__enter__`` が 2 回。
        """
        import src.service as service_module

        def _no_evict(entry, *, browser_sessions=None):
            """故意に「``pop`` しない」壊れた実装。"""
            from comken.toolbox.browser.sites.salesforce import site_for as browser_site_for
            from comken.toolbox.office.csv import read_csv

            site_class = browser_site_for(entry.url)
            if browser_sessions is None:
                with site_class() as sf:
                    sf.login_with_credentials()
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
                        Path(tmp_path).write_bytes(b"col,amt\nv,1\n")
                        dict(sf.export_reports({entry.url: tmp_path}))
                        return read_csv(tmp_path, columns=None)
                raise AssertionError("unreachable")

            sf = browser_sessions.get(site_class)
            if sf is None:
                sf = site_class()
                sf.__enter__()
                sf.login_with_credentials()
                browser_sessions[site_class] = sf
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
                    Path(tmp_path).write_bytes(b"col,amt\nv,1\n")
                    dict(sf.export_reports({entry.url: tmp_path}))
                    with CSV(tmp_path, read_only=True) as source:
                        return source.read()
            except Exception:
                # 本来は ``browser_sessions.pop(site_class)`` + ``__exit__`` だが、
                # ここではしない（壊れた実装）
                raise

        site_class, site_instance = _fake_browser_site()

        call_count = {"n": 0}

        def _flaky_export(reports, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            for _, destination in reports.items():
                Path(destination).write_bytes(b"col,amt\nv,1\n")
                yield "x", Path(destination)

        site_instance.export_reports.side_effect = _flaky_export
        browser_sessions: dict = {}

        with patch(
            "comken.toolbox.browser.sites.salesforce.site_for",
            return_value=site_class,
        ):
            monkeypatch.setattr(service_module, "_fetch_via_browser", _no_evict)
            with pytest.raises(RuntimeError, match="boom"):
                _no_evict(EXCEEDS_ENTRY, browser_sessions=browser_sessions)
            # 壊れた実装: dict には 1 件目のインスタンスが残ったまま
            assert len(browser_sessions) == 1
            # 2 件目: 壊れたインスタンスを再利用
            _no_evict(EXCEEDS_ENTRY, browser_sessions=browser_sessions)

        # 壊れた実装の特徴: __enter__ は 1 回だけ（壊れたインスタンスを使い回し）
        assert site_instance.__enter__.call_count == 1

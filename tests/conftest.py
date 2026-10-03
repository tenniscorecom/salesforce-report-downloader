"""tests/conftest.py — 全テスト共通の安全装置（autouse fixture）。

## 何故 autouse fixture で本番パスを ``tmp_path`` に倒すのか

sfdl と comken の ``paths`` モジュールには、本番の置き場所を指す
モジュール変数が複数ある:

- ``src.paths.MASTER_PATH``
  → ``\\server\\share\\tools\\salesforce\\レポート管理表.xlsx``
- ``src.paths.NOTIFICATION_FOLDER``
  → ``~/Box/Salesforceレポートダウンローダー通知``
- ``comken.services.salesforce_downloader.paths.HISTORY_DB_PATH``
  → ``C:\\SalesforceDownloader\\ダウンロード履歴.sqlite3`` （=履歴の正本）
- ``comken.services.salesforce_downloader.paths.HISTORY_PATH``
  → ``C:\\SalesforceDownloader\\ダウンロード履歴.csv`` （=人が見る CSV）

過去の本タスクの前段で、これらの差し替えを **テスト本体側で 1 か所**
忘れた結果、 ``comken.services.salesforce_downloader.paths.HISTORY_DB_PATH``
の **本番の値**（当時の仮置き ``C:\\comken\\salesforce\\ダウンロード履歴.sqlite3``）
にテストが実際に書き込む事故が起きた。 差し替え忘れは ``pytest`` を
``C:\\`` から起動したときにも起きうる（``Path.home()`` が解決できない
ような壊れた環境では ``Path.home()`` 側の変数 ``NOTIFICATION_FOLDER`` が
そのまま読まれる）。

``autouse=True`` の fixture で ``tmp_path`` 配下に ``monkeypatch.setattr``
しておくと、**個別のテストで差し替える前のデフォルトが必ず ``tmp_path``
配下になる**。 個別テストがさらに ``monkeypatch.setattr`` した場合は
その値が勝つ（``monkeypatch`` は関数スコープで共有されるので、後の
``setattr`` が上の ``setattr`` を上書きする）。 したがって既存のテストは
そのまま動く。

差し替える変数は **本番の場所を指すモジュール変数** に限定している。
``MASTER_FILENAME`` / ``HISTORY_DB_FILENAME`` などのファイル名定数は
本番の置き場所に依存しないので ``tmp_path`` 配下に倒す必要はない
（個別テストが ``MASTER_PATH`` / ``HISTORY_DB_PATH`` 全体を組むときに
自然に ``tmp_path`` 配下になる）。

``tmp_path`` は pytest 組み込みの関数スコープ fixture。 autouse fixture
が引数で受けると、 ``tmp_path`` を直接 request していないテストにも
1 個ずつ ``tmp_path`` が用意される。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from comken.services.salesforce_downloader import paths as _history_paths

import src.paths as _sfdl_paths


@pytest.fixture(autouse=True)
def _isolate_production_paths(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本番パスを ``tmp_path`` 配下に差し替える（テストごとに毎回）。

    差し替える変数（``comken.services.salesforce_downloader.paths`` 側 /
    ``src.paths`` 側の、本番の固定パスを指すモジュール変数）:

    - ``src.paths.MASTER_PATH``
    - ``src.paths.NOTIFICATION_FOLDER``
    - ``comken.services.salesforce_downloader.paths.HISTORY_DB_PATH``
    - ``comken.services.salesforce_downloader.paths.HISTORY_PATH``

    派生元の ``SALESFORCE_DOWNLOADER_FOLDER`` は ``MASTER_PATH`` /
    ``HISTORY_*_PATH`` のモジュール読み込み時に **組み立て済み**
    （``SALESFORCE_DOWNLOADER_FOLDER / MASTER_FILENAME`` 等）なので、
    派生先だけ差し替えれば十分。 個別テストが後から
    ``monkeypatch.setattr`` した場合はその値が上書きする。

    ``NOTIFICATION_FOLDER`` の親（ ``~/Box`` 相当）は **ここでは作らない**。
    ``write_notification()`` を使う側のテストが ``tmp_path / Box`` を
    ``mkdir`` する前提で作られているので、ここで先に作ると ``FileExistsError``
    でテストが落ちる（``test_oserror_is_swallowed`` / ``test_non_oserror_propagates``
    / ``home_dir`` fixture のような既存パターンと競合する）。 テストが
    ``Box`` を **作らない** 前提のものは（``TestNotificationFolderConstant``
    の ``test_default_folder_layout`` のように ``write_notification()`` を
    呼ばないもの、 ``test_no_box_folder_warns_and_does_not_raise`` のように
    親不在を検証するもの）だけなので、ここで親を作る必要はない。

    **オプトアウト:** テストが ``@pytest.mark.no_isolate_production_paths``
    マーカーを持つ場合、この fixture は何もしない。 「既定値の
    ``MASTER_PATH`` が UNC であること」を検証するテスト（``test_paths.py``
    の ``TestMasterPathPatchEffect``）など、 ``monkeypatch`` されていない
    状態のモジュール変数を **直接読む** テストだけがオプトアウトする。
    """
    if request.node.get_closest_marker("no_isolate_production_paths"):
        return None

    master_path = tmp_path / "レポート管理表.xlsx"
    history_db_path = tmp_path / "ダウンロード履歴.sqlite3"
    history_path = tmp_path / "ダウンロード履歴.csv"
    box_root = tmp_path / "Box"
    notification_folder = box_root / "Salesforceレポートダウンローダー通知"

    monkeypatch.setattr(_sfdl_paths, "MASTER_PATH", master_path)
    monkeypatch.setattr(_sfdl_paths, "NOTIFICATION_FOLDER", notification_folder)
    monkeypatch.setattr(_history_paths, "HISTORY_DB_PATH", history_db_path)
    monkeypatch.setattr(_history_paths, "HISTORY_PATH", history_path)

    return {
        "master_path": master_path,
        "history_db_path": history_db_path,
        "history_path": history_path,
        "notification_folder": notification_folder,
        "box_root": box_root,
    }

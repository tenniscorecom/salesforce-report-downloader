r"""src/service/browser.py — ブラウザ経由の取得。

``download_scheduled()`` の 1 件分で、管理表の「2000件超」列が真のときに呼ばれる。
または ``_fetch_with_auto_fallback()`` が ``SalesforceReportTruncatedError`` /
``EmptyReportError`` を受けて自動でブラウザに切り替えるときに使われる。

`selenium` 依存を、実際にブラウザ経由のレポートを使うときだけ読み込むよう、
ここで初めて import する（管理表で誰も「2000件超」を「○」にしていない
運用では `src.service` を import しても `selenium` は要らない）。

    from src.service.browser import _fetch_via_browser

``_fetch_via_browser()`` の本体・使い回しブラウザ・``login_with_credentials()``
の動きは `src.service` パッケージに集約する前のものから変更していない。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from comken.core.table.model import Table
from comken.toolbox.csv import CSV

from src.sheets.master import ReportEntry

if TYPE_CHECKING:
    # ``browser_sessions`` の値の型だけ。 ``from __future__ import annotations`` で
    # 注釈を文字列扱いにしているので、 ``TYPE_CHECKING`` に閉じてよい（無いと
    # Python 3.13 以下で import 時に NameError）
    from comken.toolbox.browser.sites.salesforce.base import SalesforceReportBrowser

logger = logging.getLogger("src.service")


def _fetch_via_browser(
    entry: ReportEntry,
    *,
    browser_sessions: dict[type[SalesforceReportBrowser], SalesforceReportBrowser] | None = None,
) -> Table:
    """ブラウザ経由で entry を取得し、`_fetch()` と同じ Table を返す。

    **ログインの扱い:** ブラウザを新規に開くとき（使い回し中でないとき）は
    ``login_with_credentials()`` を呼び、ID/パスワードを DPAPI から自動入力した
    うえで MFA の承認をスマホで待つ（無人で承認され
    なければ ``LoginFailedError`` を送出し、そのレポートは失敗として記録される。
    認証情報が未登録なら ``CredentialNotFoundError``、復号できなければ
    ``CredentialError`` が出る。いずれも ``ComkenError`` 系なので
    ``download_scheduled()`` 側の既存処理で次のレポートへ続行される）。
    ``login_with_credentials()`` が例外を出した場合は ``try/except`` で拾い、
    壊れたブラウザを dict から外して閉じる（壊れた状態のブラウザを次のレポートへ
    引き継がないため）。ブラウザを使い回す 2 件目以降では ``login_with_credentials()``
    を再実行しない（既にログイン済みのブラウザをそのまま使う）。

    **ブラウザの使い回し:** ``browser_sessions`` が渡されたときは、同じ組織
    （``site_for()`` が返すクラス）のブラウザを 1 回の実行の中で使い回す。
    まだ開いていなければ ``site_class()`` の ``__enter__()`` を直接呼んで開き、
    ``login_with_credentials()`` を 1 回だけ実行して dict に登録する。次の同組織の
    レポートは同じインスタンスを再利用する（``login_with_credentials()`` は
    再実行しない）。取得中に例外が出たブラウザは dict から外して ``__exit__()``
    を直接呼び、次は新しいブラウザを開き直す（壊れた状態のブラウザを次へ引き継がない）。
    ``browser_sessions`` が ``None`` のときは単体テスト・直接呼び出し経路として
    「開いて閉じる」従来動作に戻る（``with site_class() as sf:``）。

    `selenium` 依存を、実際にブラウザ経由のレポートを使うときだけ読み込むよう、
    ここで初めて import する（管理表で誰も「2000件超」を「○」にしていない
    運用では `src/service.py` を import しても `selenium` は要らない）。
    """
    from comken.toolbox.browser.sites.salesforce import site_for as browser_site_for

    site_class = browser_site_for(entry.url)
    if browser_sessions is None:
        # 単体テスト・直接呼び出し経路。今まで通り「開いて閉じる」
        with site_class() as sf:
            # ID/パスワードは DPAPI から自動入力、MFA はスマホで承認（無人で承認されなければ
            # ``LoginFailedError``、認証情報が無ければ ``CredentialNotFoundError`` /
            # ``CredentialError``。いずれも ``with`` を抜ける際に ``__exit__()`` が呼ばれる）
            sf.login_with_credentials()
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
                dict(sf.export_reports({entry.url: tmp_path}))
                with CSV(tmp_path, read_only=True) as source:
                    return source.read()
        raise AssertionError("unreachable")  # 上の with 内で必ず return する

    # 同じ組織のブラウザが既に開いていれば再利用、無ければ開く
    sf = browser_sessions.get(site_class)
    if sf is None:
        # ``__enter__()`` を直接呼ぶのは、``sf`` を ``browser_sessions`` に乗せて
        # ``_download_scheduled_locked()`` 側の ``finally`` まで保持するため。
        # クロージャに入れると外から参照できなくなる。``__exit__()`` は例外時
        # または ``_download_scheduled_locked()`` 最後の ``finally`` で呼ぶ
        sf = site_class()
        sf.__enter__()
        # ``__enter__()`` が成功した時点で dict に登録する。``login_with_credentials()``
        # が例外を出しても「失敗したら閉じて入れ物から外す」範囲（下の ``try/except``）
        # に入るので、後続のレポートで「壊れたブラウザ」を再利用しない。
        # ``__enter__()`` 自体が失敗した場合は何も開いていないので、dict にも入れず、
        # 閉じもしない（``__exit__`` も未呼び出し）
        browser_sessions[site_class] = sf
        try:
            # ID/パスワードを DPAPI から自動入力したうえで MFA の承認をスマホで待つ。
            # 認証情報の例外（``CredentialNotFoundError`` / ``CredentialError``）も
            # 下の ``except Exception`` で拾われ、閉じて dict から外したうえで元の例外を
            # そのまま上げる（``download_scheduled()`` 側の既存処理で次のレポートへ続行）
            sf.login_with_credentials()
        except Exception:
            # ``login_with_credentials()`` 失敗時も「壊れたブラウザを dict に残したまま
            # 次へ渡さない」ため、閉じて dict から外す。閉じるときに起きた例外は元の
            # 失敗を隠すので、警告ログに留めて元例外を上げる
            browser_sessions.pop(site_class, None)
            try:
                sf.__exit__(None, None, None)
            except Exception as close_exc:
                logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)
            raise
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / f"{entry.key}.csv"
            dict(sf.export_reports({entry.url: tmp_path}))
            with CSV(tmp_path, read_only=True) as source:
                return source.read()
    except Exception:
        # 壊れた状態のブラウザを次回以降に渡さないため、ここで閉じて dict から外す。
        # 閉じるときに起きた例外は元の失敗を隠すので、警告ログに留めて元例外を上げる
        browser_sessions.pop(site_class, None)
        try:
            sf.__exit__(None, None, None)
        except Exception as close_exc:
            logger.warning("ブラウザを閉じる途中で例外が発生しました: %s", close_exc)
        raise

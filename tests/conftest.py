"""``tests/`` 共通の fixture / ヘルパー。

``src.history.record()`` は ``executed_at`` と ``folder`` を必須にしたので、
テストは毎回両方を組み立てる必要がある。組み立て方はファイルごとに違わないので、
ここで 1 か所に寄せて各テストが「何を検証したいか」だけを書く形にする。

- ``default_executed_at`` — 時刻固定がテストの本質でないときに使い回す値。
  ``record()`` 側の ``executed_at`` 引数は必須なので、時刻を特に固定しない
  テストでも何かを入れる必要がある（=None の後方互換は廃止）。
"""

from __future__ import annotations

import datetime as dt

import pytest

# 時刻を特に固定しない ``record()`` 呼び出しで使う ``executed_at`` の既定値。
# ``test_history_record.py`` の既存テストが ``2026-09-26`` の 10:00 を
# 使うので、タイムスタンプ由来の偶発的差分を疑わずに済む
DEFAULT_EXECUTED_AT = dt.datetime(2026, 9, 26, 10, 0, 0)  # noqa: DTZ001


@pytest.fixture
def default_executed_at() -> dt.datetime:
    """``executed_at`` をテスト固有の時刻で固定しない ``record()`` 呼び出し用。"""
    return DEFAULT_EXECUTED_AT

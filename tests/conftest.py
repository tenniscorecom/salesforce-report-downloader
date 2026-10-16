"""``tests/`` 共通の fixture / ヘルパー。

``src.history.record()`` は ``executed_at`` と ``folder`` を必須にしたので、
テストは毎回両方を組み立てる必要がある。組み立て方はファイルごとに違わないので、
ここで 1 か所に寄せて各テストが「何を検証したいか」だけを書く形にする。

- ``default_executed_at`` — 時刻固定がテストの本質でないときに使い回す値。
  ``record()`` 側の ``executed_at`` 引数は必須なので、時刻を特に固定しない
  テストでも何かを入れる必要がある（=None の後方互換は廃止）。

**パス設定:** comken の場所を ``PYTHON_LIBRARY`` 環境変数 → 無ければ
隣接フォルダ ``../comken`` の順で ``sys.path`` の先頭に足す。
``pyproject.toml`` に pythonpath を直書きしないのは、CI／社内 BO 環境ごとに
参照場所が違うため（環境変数での指定を優先し、最後に隣接フォルダへ
フォールバックする形にする）。

``src/`` も同じ要領で ``sys.path`` 先頭に足す（プロジェクトルート）。
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

# プロジェクトルート（ ``src/`` の親）と comken の場所を ``sys.path`` に足す。
# ``conftest.py`` は pytest が最初にロードするため、各テストが import する
# 前にこの調整が効く。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_COMKEN_FALLBACK = _PROJECT_ROOT.parent / "comken"


def _prepend_sys_path(path: Path) -> None:
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


# ``src/`` を import 可能にするため、プロジェクトルートを先頭に足す。
_prepend_sys_path(_PROJECT_ROOT)

# comken の場所を ``PYTHON_LIBRARY`` 環境変数 → 隣接 ``../comken`` の順で探す。
_python_library = os.environ.get("PYTHON_LIBRARY", "")
if _python_library:
    _prepend_sys_path(Path(_python_library))
elif _COMKEN_FALLBACK.is_dir():
    _prepend_sys_path(_COMKEN_FALLBACK)


# 時刻を特に固定しない ``record()`` 呼び出しで使う ``executed_at`` の既定値。
# ``test_history_record.py`` の既存テストが ``2026-09-26`` の 10:00 を
# 使うので、タイムスタンプ由来の偶発的差分を疑わずに済む
DEFAULT_EXECUTED_AT = dt.datetime(2026, 9, 26, 10, 0, 0)  # noqa: DTZ001


@pytest.fixture
def default_executed_at() -> dt.datetime:
    """``executed_at`` をテスト固有の時刻で固定しない ``record()`` 呼び出し用。"""
    return DEFAULT_EXECUTED_AT

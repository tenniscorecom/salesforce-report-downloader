"""src/soql_reports/_registry.py — SOQLレポート登録の置き場所。

``__init__.py`` から ``runner`` を import するので、``runner`` から
``__init__.py`` を逆に import すると循環する。``registered_reports()`` は
``runner.download_soql_reports()`` が「``reports=None`` のときの既定値」
として参照するため、**循環を切れる別のモジュール**に置く。
"""

from __future__ import annotations

from comken.core.discovery import find_subclasses

from src.exceptions import DownloaderError
from src.soql_reports import reports
from src.soql_reports.base import SoqlReport


def registered_reports() -> tuple[type[SoqlReport], ...]:
    """``reports/`` パッケージに置かれた ``SoqlReport`` サブクラスを集めて返す。

    走査は ``comken.core.discovery.find_subclasses()`` に任せる
    （``pkgutil.walk_packages`` で ``reports/`` 直下の ``.py`` を1つずつ
    ``importlib.import_module`` し、そのモジュール自身で定義された
    ``SoqlReport`` のサブクラスを拾う）。**ファイル名が ``_`` で始まる
    モジュールは走査対象外**（``_template.py`` のような雛形を登録せずに済む）。

    ``KEY`` の昇順で返す。**キャッシュはしない** — ``importlib.import_module``
    は既に import 済みなら再 load しない（``sys.modules`` 経由で軽い）ので、
    呼ぶたびに ``reports/`` を全走査し直してもコストは無視できる。
    ファイル追加のたびに再起動は不要。

    Raises:
        DownloaderError: ``KEY`` が空のレポートが含まれているか、複数の
            レポートが同じ ``KEY`` を持っている。メッセージには
            ``reports/<ファイル>.py`` のパスとクラス名を含め、
            対処（``KEY`` を埋める／重複を直す）を併記する。
    """
    classes = find_subclasses(reports, SoqlReport)
    _validate_keys(classes)
    return tuple(sorted(classes, key=lambda cls: cls.KEY))


def _validate_keys(classes: tuple[type[SoqlReport], ...]) -> None:
    """``KEY`` の空と重複を検査し、問題があれば ``DownloaderError`` を送出する。

    Args:
        classes: ``find_subclasses()`` が返したクラスのタプル（ソート前）。
    """
    by_key: dict[str, list[type[SoqlReport]]] = {}
    for cls in classes:
        by_key.setdefault(cls.KEY, []).append(cls)

    empties = [cls for cls in classes if not cls.KEY]
    if empties:
        cls = empties[0]
        path = _report_path(cls.__module__)
        raise DownloaderError(
            f"SOQLレポートの KEY が空です: {cls.__name__}（{path}）\n"
            f"対処: {path} のクラス {cls.__name__} の KEY に、社内で決める管理番号"
            f'（例: "9001"）を埋めてください。空のままでは登録できません。'
        )

    duplicates = {key: entries for key, entries in by_key.items() if len(entries) > 1}
    if duplicates:
        # メッセージに「全件のファイル名」を入れる（最初にぶつかった1件だけだと直せないため）
        lines: list[str] = []
        for key, entries in duplicates.items():
            paths = ", ".join(
                f"{cls.__name__}（{_report_path(cls.__module__)}）" for cls in entries
            )
            lines.append(f"  KEY={key!r}: {paths}")
        joined = "\n".join(lines)
        raise DownloaderError(
            f"SOQLレポートの KEY が重複しています:\n{joined}\n"
            f"対処: 同じ KEY を別の管理番号に直すか、片方のレポートを reports/ から"
            f"取り除いてください。1つの管理番号には1つの SoqlReport だけ紐付けてください。"
        )


def _report_path(module_name: str) -> str:
    """``reports.<name>`` 形式のモジュール名から ``reports/<name>.py`` 相当のパス表記を返す。"""
    short = module_name.removeprefix(f"{reports.__name__}.")
    return f"reports/{short}.py"

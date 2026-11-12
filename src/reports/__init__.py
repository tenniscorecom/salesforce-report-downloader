"""src/reports/__init__.py — 実装置き場。

**1レポート=1ファイル**で ``SoqlReport`` サブクラスを置く。
``src.soql_reports._registry.registered_reports()`` が ``pkgutil.iter_modules`` でこのパッケージの
中身を走査し、各モジュールで定義された ``SoqlReport`` サブクラスを自動で登録する
（``cls.__module__ == module.__name__`` で「このモジュール自身で定義されたもの」
だけを集めるので、他モジュールから ``from ... import`` しただけのクラスは拾わない）。

ファイル名が ``_`` で始まるモジュール（例: ``_template.py``）は走査対象外なので、
雛形をこの名前で置いておけば登録されずに済む。

基底クラス ``SoqlReport`` は同じフォルダの ``base.py`` にある
（``from src.reports.base import SoqlReport``）。 ``base.py`` 自身も走査対象だが、
``find_subclasses()`` が ``cls is base`` を除外するので、基底クラスがレポートとして
登録されることはない。
"""

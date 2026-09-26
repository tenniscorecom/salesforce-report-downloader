"""src/soql_reports/reports/__init__.py — 実装置き場。

**1レポート=1ファイル**で ``SoqlReport`` サブクラスを置く。
``_registry.registered_reports()`` が ``pkgutil.iter_modules`` でこのパッケージの
中身を走査し、各モジュールで定義された ``SoqlReport`` サブクラスを自動で登録する
（``cls.__module__ == module.__name__`` で「このモジュール自身で定義されたもの」
だけを集めるので、他モジュールから ``from ... import`` しただけのクラスは拾わない）。

ファイル名が ``_`` で始まるモジュール（例: ``_template.py``）は走査対象外なので、
雛形をこの名前で置いておけば登録されずに済む。
"""

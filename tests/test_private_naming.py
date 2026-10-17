"""別ファイルから ``_`` 付きの名前を使っていないことを確かめるテスト。

``_`` は「そのファイルの中だけで使う」印（comken の docs/CONVENTIONS_PYTHON.md の命名規則）。
パッケージ内の別ファイルから使う内部の関数・クラスには ``_`` を付けない。
"""

from __future__ import annotations

import ast
from pathlib import Path

import src

PACKAGE_ROOT = Path(src.__file__).parent
EXCLUDED_DIRS = {"__pycache__"}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _project_modules() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in PACKAGE_ROOT.rglob("*.py")
        if not EXCLUDED_DIRS & set(path.parts)
    }


def _collect_aliases(tree: ast.AST, modules: dict[str, Path]) -> dict[str, str]:
    """このファイルの中で、src のモジュールにつけた別名を集める。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for imported in node.names:
                full_name = f"{node.module}.{imported.name}"
                if full_name in modules:
                    aliases[imported.asname or imported.name] = full_name
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in modules and imported.asname:
                    aliases[imported.asname] = imported.name
    return aliases


def _private_from_imports(
    tree: ast.AST, module: str, modules: dict[str, Path]
) -> list[tuple[int, str]]:
    """``from src.x import _name`` の形（別のモジュールから）を探す。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0):
            continue
        if not node.module.startswith("src") or node.module == module:
            continue
        for imported in node.names:
            is_submodule = f"{node.module}.{imported.name}" in modules
            if _is_private(imported.name) and not is_submodule:
                found.append((node.lineno, f"from {node.module} import {imported.name}"))
    return found


def _private_attribute_uses(tree: ast.AST, aliases: dict[str, str]) -> list[tuple[int, str]]:
    """``別名._name`` の形（別のモジュールの非公開名）を探す。"""
    return [
        (node.lineno, f"{node.value.id}.{node.attr}")
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and _is_private(node.attr)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
    ]


def _private_uses_of_other_modules(module: str, path: Path, modules: dict[str, Path]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    uses = _private_from_imports(tree, module, modules)
    uses += _private_attribute_uses(tree, _collect_aliases(tree, modules))
    return [f"{path.name}:{line} {text}" for line, text in sorted(uses)]


def test_no_private_name_is_used_from_another_module() -> None:
    modules = _project_modules()
    violations: list[str] = []
    for module, path in sorted(modules.items()):
        violations.extend(_private_uses_of_other_modules(module, path, modules))
    assert not violations, (
        "別ファイルから `_` 付きの名前を使っています。使う側があるなら `_` を外してください"
        "（comken の docs/CONVENTIONS_PYTHON.md の命名規則）:\n" + "\n".join(violations)
    )

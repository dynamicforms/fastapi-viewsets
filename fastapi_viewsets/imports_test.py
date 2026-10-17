"""Celery stays inside `decorators/celery_viewset/`: no other module of the package imports it."""

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).parent
EXECUTOR_PACKAGES = {"celery", "billiard", "kombu"}
EXECUTOR_DIRECTORY = PACKAGE / "decorators" / "celery_viewset"


def _imported_roots(path: pathlib.Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_the_celery_executor_imports_celery_billiard_or_kombu():
    offenders = {
        str(path.relative_to(PACKAGE)): sorted(_imported_roots(path) & EXECUTOR_PACKAGES)
        for path in PACKAGE.rglob("*.py")
        if not path.name.endswith("_test.py") and EXECUTOR_DIRECTORY not in path.parents
    }
    assert {path: roots for path, roots in offenders.items() if roots} == {}

"""Guard: container integration tests must declare the container marker."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
CONTAINER_CALLEES = frozenset(
    {"start_systemd_container", "build_systemd_image", "require_docker"}
)


def _test_py_files() -> list[Path]:
    return sorted(p for p in TESTS_DIR.glob("test_*.py") if p.name != "test_container_markers.py")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _function_calls_container_helpers(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(func):
        name = _call_name(child)
        if name in CONTAINER_CALLEES:
            return True
    return False


def _is_container_marker_decorator(dec: ast.AST) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if not isinstance(target, ast.Attribute) or target.attr != "container":
        return False
    mark = target.value
    return (
        isinstance(mark, ast.Attribute)
        and mark.attr == "mark"
        and isinstance(mark.value, ast.Name)
        and mark.value.id == "pytest"
    )


def _has_container_marker(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_is_container_marker_decorator(dec) for dec in func.decorator_list)


def _files_with_container_marked_tests() -> set[str]:
    stems: set[str] = set()
    for path in _test_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if _has_container_marker(node):
                stems.add(path.stem)
    return stems


def _slow_matrix_stems_from_ci_yml() -> set[str]:
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    stems: list[str] = []
    for index, line in enumerate(lines):
        if not re.match(r"\s+container_test_file:\s*$", line):
            continue
        offset = index + 1
        while offset < len(lines):
            match = re.match(r"\s+-\s+(\S+)\s*$", lines[offset])
            if not match:
                break
            stems.append(match.group(1))
            offset += 1
        return set(stems)
    pytest.fail("container_test_file matrix not found in .github/workflows/ci.yml")


def test_container_marker_guard() -> None:
    missing: list[str] = []
    for path in _test_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if _function_calls_container_helpers(node) and not _has_container_marker(node):
                missing.append(f"{path.relative_to(TESTS_DIR.parent)}::{node.name}")
    if missing:
        pytest.fail(
            "tests that use docker/systemd helpers must use @pytest.mark.container:\n"
            + "\n".join(f"  - {item}" for item in sorted(missing))
        )


def test_slow_ci_matrix_lists_every_container_test_file() -> None:
    expected = _files_with_container_marked_tests()
    matrix = _slow_matrix_stems_from_ci_yml()
    missing = sorted(expected - matrix)
    extra = sorted(matrix - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(
                "add to slow.container_test_file matrix in ci.yml:\n"
                + "\n".join(f"  - {name}" for name in missing)
            )
        if extra:
            parts.append(
                "remove from slow.container_test_file matrix (no @pytest.mark.container tests):\n"
                + "\n".join(f"  - {name}" for name in extra)
            )
        pytest.fail("\n".join(parts))

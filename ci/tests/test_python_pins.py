"""python-pins CI check (R33)."""

from __future__ import annotations

from pathlib import Path

from _python_pins import scan_python_pins


def test_r33_seeded_open_python_range_fails(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8.0\n", encoding="utf-8")
    violations = scan_python_pins(tmp_path)
    assert violations == ["requirements-dev.txt:1: pytest>=8.0 is not an exact pin"]


def test_r33_exact_python_pins_pass(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text(
        "example-pkg==1.2.3\nPyExample==6.0.0\n",
        encoding="utf-8",
    )
    assert scan_python_pins(tmp_path) == []

"""plan-codes CI check: product docs contain no plan codes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from _contract_md import missing_contract_roots
from _plan_codes import scan_plan_codes
from support import REPO_ROOT as _REPO_ROOT


def test_seeded_plan_codes_fail(tmp_path: Path) -> None:
    contract = tmp_path / "platform" / "backups" / "CONTRACT.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("Enrollment is R31 and D7.\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text(
        "See p0b-10, p2-24b, OQ7, and §6.\n",
        encoding="utf-8",
    )
    readme = tmp_path / "envs" / "sample" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text("After P2 and p2-24.\n", encoding="utf-8")

    assert scan_plan_codes(tmp_path) == [
        "docs/note.md:1: p0b-10",
        "docs/note.md:1: p2-24b",
        "docs/note.md:1: OQ7",
        "docs/note.md:1: §6",
        "envs/sample/README.md:1: P2",
        "envs/sample/README.md:1: p2-24",
        "platform/backups/CONTRACT.md:1: R31",
        "platform/backups/CONTRACT.md:1: D7",
    ]


def test_plan_codes_outside_product_docs_pass(tmp_path: Path) -> None:
    (tmp_path / "rulebooks").mkdir()
    (tmp_path / "rulebooks" / "coding-agents.md").write_text(
        "R31 p0b-10 P2 §6\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text("R31 p0b-10 P2\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "restore").write_text("R31 = 'kept'\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "clean.md").write_text("No codes here.\n", encoding="utf-8")

    assert scan_plan_codes(tmp_path) == []


def test_plan_code_gaps_fail(tmp_path: Path) -> None:
    """A space in §, OQ-7, an upper-case phase suffix, R4a, and J<n> are plan codes."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "gaps.md").write_text(
        "See § 5, OQ-7, P0a, P0A, P4b, P4B, R4a, and J16.\n",
        encoding="utf-8",
    )
    (tmp_path / "RUNBOOK.md").write_text("J7\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("OQ-1\n", encoding="utf-8")
    (tmp_path / "README.txt").write_text("R4a\n", encoding="utf-8")
    assert scan_plan_codes(tmp_path) == [
        "README.txt:1: R4a",
        "RUNBOOK.md:1: J7",
        "readme.md:1: OQ-1",
        "docs/gaps.md:1: § 5",
        "docs/gaps.md:1: OQ-7",
        "docs/gaps.md:1: P0a",
        "docs/gaps.md:1: P0A",
        "docs/gaps.md:1: P4b",
        "docs/gaps.md:1: P4B",
        "docs/gaps.md:1: R4a",
        "docs/gaps.md:1: J16",
    ]


def test_plan_code_lookalikes_pass(tmp_path: Path) -> None:
    """P100, P40, and P256 are not phases. F<n> and B<n> stay unmatched."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "gpus.md").write_text(
        "GPU names P100, P40, and P256. Findings F22 and B5. Product B2.\n",
        encoding="utf-8",
    )
    assert scan_plan_codes(tmp_path) == []


def test_known_plan_code_collisions_still_fail(tmp_path: Path) -> None:
    """P4, R2, and D3 still match. They are known collisions."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "collide.md").write_text("P4 R2 D3\n", encoding="utf-8")
    assert scan_plan_codes(tmp_path) == [
        "docs/collide.md:1: P4",
        "docs/collide.md:1: R2",
        "docs/collide.md:1: D3",
    ]


def test_ordinary_words_pass(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plain.md").write_text(
        "\n".join(
            [
                "CPU request of 100m and a memory limit of 256Mi.",
                "Checked chart v40 on k3s v1.36.5+k3s1.",
                "restic/restic sha256 digest. Pull request. PVC.",
                "section 5. A bare p2, r31, or d30 is ordinary. p2-24bit is a word.",
                "OQ without digits, and P without digits.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert scan_plan_codes(tmp_path) == []


def test_contract_with_operations_and_limits_passes(tmp_path: Path) -> None:
    root = tmp_path / "platform" / "backups"
    root.mkdir(parents=True)
    (root / "CONTRACT.md").write_text(
        "# CONTRACT: backups\n\n## Operations\n\nSteps.\n\n## Limits\n\nNone extra.\n",
        encoding="utf-8",
    )
    assert missing_contract_roots(tmp_path) == []


def test_omitting_the_check_from_rule_list_fails_run_checks(tmp_path: Path) -> None:
    ci = tmp_path / "ci"
    ci.mkdir()
    shutil.copy(_REPO_ROOT / "ci" / "run-checks", ci / "run-checks")
    checks = ci / "checks"
    checks.mkdir()
    for name in ("plan-codes", "other"):
        path = checks / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    listed = ci / "rule-checks.txt"
    listed.write_text("other\n", encoding="utf-8")

    missing = subprocess.run(
        ["bash", str(ci / "run-checks")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "unexpected checks" in missing.stderr
    assert "plan-codes" in missing.stderr

    listed.write_text("other\nplan-codes\n", encoding="utf-8")
    present = subprocess.run(
        ["bash", str(ci / "run-checks")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert present.returncode == 0, present.stderr

"""product-ci: product trees do not use ci/, except the verify read."""

from __future__ import annotations

import subprocess
from pathlib import Path

from _product_ci import scan_product_ci


def test_product_file_referencing_ci_fails_and_verify_read_passes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    leak = tmp_path / "tools" / "leak.py"
    leak.parent.mkdir(parents=True)
    leak.write_text('open("ci/checks/secret-scan")\n', encoding="utf-8")
    verify = tmp_path / "tools" / "verify"
    verify.mkdir()
    (verify / "run").write_text(
        'checks = (REPO_ROOT / "ci/rule-checks.txt").read_text(encoding="utf-8").split()\n'
        "import ci.checks.lib._core_names\n",
        encoding="utf-8",
    )
    _git_add_all(tmp_path)

    violations = scan_product_ci(tmp_path)
    assert [(item.path, item.line, item.reference) for item in violations] == [
        ("tools/leak.py", 1, "ci/checks/secret-scan"),
        ("tools/verify/run", 2, "import ci"),
    ]


def _init_git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)


def _git_add_all(root: Path) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=root)

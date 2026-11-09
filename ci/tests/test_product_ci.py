"""product-ci: product trees do not use ci/."""

from __future__ import annotations

import subprocess
from pathlib import Path

from jarvis.checks.product_ci import scan_product_ci


def test_product_file_referencing_ci_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    leak = tmp_path / "infra" / "leak.py"
    leak.parent.mkdir(parents=True)
    leak.write_text('open("ci/checks/secret-scan")\n', encoding="utf-8")
    verify = tmp_path / "jarvis" / "verify"
    verify.mkdir(parents=True)
    (verify / "run.py").write_text(
        'checks = (REPO_ROOT / "ci/rule-checks.txt").read_text(encoding="utf-8").split()\n'
        "import ci.checks.lib._core_names\n",
        encoding="utf-8",
    )
    package = tmp_path / "jarvis"
    (package / "env").mkdir()
    (package / "checks").mkdir()
    (package / "env" / "leak.py").write_text('open("ci/run-checks")\n', encoding="utf-8")
    # jarvis/checks/ is CI's own code, so it may read ci/.
    (package / "checks" / "reads_ci.py").write_text('open("ci/run-checks")\n', encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_product_ci(tmp_path)
    assert [(item.path, item.line, item.reference) for item in violations] == [
        ("infra/leak.py", 1, "ci/checks/secret-scan"),
        ("jarvis/env/leak.py", 1, "ci/run-checks"),
        ("jarvis/verify/run.py", 1, "ci/rule-checks.txt"),
        ("jarvis/verify/run.py", 2, "import ci"),
    ]


def _init_git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)


def _git_add_all(root: Path) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=root)

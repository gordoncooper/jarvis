"""ci/select-tests: which tests a pull request runs, from the files it changes."""

from __future__ import annotations

import importlib.machinery
import importlib.util

from support import REPO_ROOT

_loader = importlib.machinery.SourceFileLoader("select_tests", str(REPO_ROOT / "ci" / "select-tests"))
_spec = importlib.util.spec_from_loader("select_tests", _loader)
select_tests = importlib.util.module_from_spec(_spec)
_loader.exec_module(select_tests)
select = select_tests.select


def test_docs_only_runs_no_tests() -> None:
    """Rule: a change to docs only (*.md, LICENSE) runs no tests; the rule checks still run."""
    assert select("agent/docs", ["README.md", "docs/deploy.md", "LICENSE"]) == "none"


def test_a_non_renovate_change_runs_everything() -> None:
    """Rule: only Renovate branches get a subset; any other code change runs the whole suite."""
    assert select("agent/x", ["README.md", "core/platform/kyverno/rbac.yaml"]) == "all"
    assert select("agent/x", []) == "all"


def test_a_renovate_bump_runs_the_tests_its_file_reaches(tmp_path, monkeypatch) -> None:
    """Rule: a CLI or Python pin runs the whole suite (any test may run that CLI or import that
    package); an image runs the tests that name its file or its folder, and the flux build."""
    assert select("renovate/kubeconform", ["mise.toml", "mise.lock"]) == "all"
    image = select("renovate/k8s-gateway", ["core/feature/app-dns/helmrelease.yaml"]).split()
    assert "ci/tests/test_flux_entry.py" in image and "ci/tests/test_k3s.py" not in image
    # This file names that one whole, so a suite of two shows the folder alone selecting a test.
    (tmp_path / "test_folder.py").write_text("core/feature/app-dns", encoding="utf-8")
    (tmp_path / "test_other.py").write_text("core/platform/kyverno", encoding="utf-8")
    monkeypatch.setattr(select_tests, "TESTS", tmp_path)
    assert select("renovate/k8s-gateway", ["core/feature/app-dns/helmrelease.yaml"]) == "ci/tests/test_folder.py"


def test_a_renovate_change_it_cannot_place_runs_everything() -> None:
    """Rule: a file no test names, or one every test can reach, runs the whole suite."""
    # Built here, so this file does not name it.
    assert select("renovate/x", ["/".join(("unplaced", "new.file"))]) == "all"
    assert select("renovate/python-dev", ["pyproject.toml", "uv.lock"]) == "all"

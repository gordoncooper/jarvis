"""tools/render: one command renders every generated file of an env, and --check lists stale ones."""

from __future__ import annotations

import os
import subprocess
import sys

from support import REPO_ROOT, ExampleCopy

FIXTURE_APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"


def _check(copy: ExampleCopy) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "render"), "--env", copy.name, "--check"],
        env={**os.environ, "INSTANCE": str(copy.root), "APPS_REPO": str(FIXTURE_APPS)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_passes_a_rendered_env_and_names_each_stale_file(example_copy: ExampleCopy) -> None:
    """Rule: --check writes nothing, passes a rendered env, and names an edited, a missing and an extra file."""
    flux = example_copy.env_dir / "flux"
    clean = _check(example_copy)
    assert clean.returncode == 0, clean.stderr
    sync, configmap = flux / "sync.yaml", flux / "generated" / "cluster-vars-configmap.yaml"
    extra = flux / "generated" / "stray.yaml"
    before = sync.read_text(encoding="utf-8"), configmap.read_text(encoding="utf-8")
    try:
        sync.write_text(before[0] + "# edited\n", encoding="utf-8")
        configmap.unlink()
        extra.write_text("kind: ConfigMap\n", encoding="utf-8")
        stale = _check(example_copy)
        assert stale.returncode == 1
        assert f"envs/{example_copy.name}/flux/sync.yaml: differs from the render" in stale.stderr
        assert "flux/generated/cluster-vars-configmap.yaml: missing" in stale.stderr
        assert "flux/generated/stray.yaml: not rendered from the values" in stale.stderr
        assert sync.read_text(encoding="utf-8").endswith("# edited\n")
    finally:
        sync.write_text(before[0], encoding="utf-8")
        configmap.write_text(before[1], encoding="utf-8")
        extra.unlink(missing_ok=True)

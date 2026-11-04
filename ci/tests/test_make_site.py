"""make site: schema first, then ansible-playbook with the documented inventories."""

from __future__ import annotations

import os
import stat
import subprocess

from ansible_role_proof import env_name
from support import REPO_ROOT


def _run_site(tmp_path, *extra: str) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "ansible-playbook.log"
    stub = tmp_path / "ansible-playbook"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$0\" \"$@\" > \"${CALL_LOG}\"\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env["CALL_LOG"] = str(log)
    env.pop("ENV", None)
    env.pop("TAGS", None)
    proc = subprocess.run(
        ["make", "site", f"ANSIBLE_PLAYBOOK={stub}", *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    recorded = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, recorded


def test_make_site_schema_error_skips_playbook(tmp_path) -> None:
    """A schema error means ansible-playbook is not called."""
    proc, recorded = _run_site(tmp_path, "ENV=missing-env")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "unknown env" in proc.stdout + proc.stderr
    assert recorded == ""


def test_make_site_passes_inventories_tags(tmp_path) -> None:
    """A valid env passes -i, -e, and TAGS through to ansible-playbook."""
    name = env_name()
    proc, recorded = _run_site(tmp_path, f"ENV={name}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = recorded.splitlines()
    assert args[1] == "site.yml"
    assert f"../../envs/{name}/inventory/hosts.yml" in args
    assert "../inventory/roles" in args
    assert f"jarvis_cluster_vars=../../envs/{name}/values/cluster-vars.yaml" in args
    assert "--tags" not in args

    tagged, recorded = _run_site(tmp_path, f"ENV={name}", "TAGS=base-os,k3s")
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    args = recorded.splitlines()
    assert args[args.index("--tags") + 1] == "base-os,k3s"

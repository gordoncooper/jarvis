"""Apply an env's hosts with core's Ansible playbook (infra/ansible/site.yml).

  python -m jarvis.node.site --env <env> [--tags <tags>] [--limit <hosts>] [--check]

The env schema runs first and refuses before Ansible on any error. The playbook reads the
env's inventory and cluster-vars from the instance repo (INSTANCE, default this checkout).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from jarvis.env.runtime import instance_root
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema.env import ValidationError, validate_one_env


def playbook_command(instance: Path, env: str, *, tags: str | None, limit: str | None, check: bool) -> list[str]:
    """The ansible-playbook command line, run from infra/ansible."""
    env_dir = instance / "envs" / env
    command = [
        "ansible-playbook",
        "site.yml",
        "-i",
        str(env_dir / "inventory" / "hosts.yml"),
        "-i",
        "../inventory/roles",
        "-e",
        f"jarvis_instance_root={instance}",
        "-e",
        f"jarvis_cluster_vars={env_dir / 'values' / 'cluster-vars.yaml'}",
    ]
    if tags:
        command += ["--tags", tags]
    if limit:
        command += ["--limit", limit]
    if check:
        command += ["--check", "--diff"]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help="Env under envs/")
    parser.add_argument("--tags", help="Run only these play tags (comma-separated)")
    parser.add_argument("--limit", help="Run only on these hosts")
    parser.add_argument("--check", action="store_true", help="Dry run: show what would change")
    args = parser.parse_args(argv)
    instance = instance_root(PRODUCT_ROOT)
    try:
        validate_one_env(instance, args.env)
    except ValidationError as exc:
        print(f"REFUSE site: {args.env} fails validation", flush=True)
        print(exc, flush=True)
        return 3
    if shutil.which("ansible-playbook") is None:
        print("FAIL site: ansible-playbook is not on PATH (run jarvis doctor)", flush=True)
        return 4
    command = playbook_command(instance, args.env, tags=args.tags, limit=args.limit, check=args.check)
    return subprocess.run(command, cwd=PRODUCT_ROOT / "infra" / "ansible", check=False).returncode


if __name__ == "__main__":
    sys.exit(main())

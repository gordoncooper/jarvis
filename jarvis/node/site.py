"""Apply an env's hosts with core's Ansible playbook (infra/ansible/site.yml).

  jarvis site [--tags <tags>] [--limit <hosts>] [--check]

The env schema runs first and refuses before Ansible on any error. The playbook reads the
env's inventory from the instance repo (INSTANCE, default this checkout), and what validation
resolved as one extra-vars file (site_vars).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jarvis.env.runtime import instance_root
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema.env import ValidationError, validate_one_env
from jarvis.secrets import age


def site_vars(instance: Path, env: str, cluster_vars: dict[str, Any]) -> dict[str, Any]:
    """What every play reads of the env, resolved once: its cluster-vars with the schema's
    defaults (validate_one_env's result), the instance, the working age key's path, and the age
    recipients of the env's .sops.yaml rule."""
    return {
        **cluster_vars,
        "jarvis_instance_root": str(instance),
        "sops_age_key_file": str(age.key_path(cluster_vars["age_key_file"])),
        "sops_age_recipients": age.recipients(instance / ".sops.yaml", env),
    }


def write_vars(instance: Path, env: str, path: Path) -> None:
    """Validate the env, then write its site_vars to path as JSON. ValidationError on any error."""
    path.write_text(json.dumps(site_vars(instance, env, validate_one_env(instance, env))), encoding="utf-8")


def playbook_command(instance: Path, env: str, vars_file: Path, *, tags: str | None, limit: str | None, check: bool) -> list[str]:
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
        f"@{vars_file}",
    ]
    if tags:
        command += ["--tags", tags]
    if limit:
        command += ["--limit", limit]
    if check:
        command += ["--check", "--diff"]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis site", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--tags", help="Run only these play tags (comma-separated)")
    parser.add_argument("--limit", help="Run only on these hosts")
    parser.add_argument("--check", action="store_true", help="Dry run: show what would change")
    args = parser.parse_args(argv)
    instance = instance_root(PRODUCT_ROOT)
    with tempfile.TemporaryDirectory() as work:
        vars_file = Path(work) / "site-vars.json"
        try:
            write_vars(instance, args.env, vars_file)
        except ValidationError as exc:
            print(f"REFUSE site: {args.env} fails validation", flush=True)
            print(exc, flush=True)
            return 3
        if shutil.which("ansible-playbook") is None:
            print("FAIL site: ansible-playbook is not on PATH (run jarvis doctor)", flush=True)
            return 4
        command = playbook_command(instance, args.env, vars_file, tags=args.tags, limit=args.limit, check=args.check)
        return subprocess.run(command, cwd=PRODUCT_ROOT / "infra" / "ansible", check=False).returncode


if __name__ == "__main__":
    sys.exit(main())

"""Every env-level check on an instance repo: its schema, wiring and committed files.

jarvis validate runs these for the instance it is run from; core's own CI runs each one by name
on core's own envs. Each check reads every env under envs/.

  python -m jarvis.env.validate [--only <name>]...

Exit 0 when every check passes, 1 when one fails.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from jarvis import paths as _paths
from jarvis.checks import (
    addons,
    app_hosting,
    backup_isolation,
    flux_identity,
    flux_prune,
    manifest_lint,
)
from jarvis.env.runtime import instance_root
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema.env import ValidationError, validate_env_tree


def _env_schema(instance: Path) -> list[str]:
    try:
        validate_env_tree(instance)
    except ValidationError as exc:
        return list(exc.messages)
    return []


def _manifest_lint(instance: Path) -> list[str]:
    # An instance repo holds only envs/, so the core roots are required only in core itself.
    found = manifest_lint.scan_manifest_lint(instance, require_roots=instance == PRODUCT_ROOT)
    return manifest_lint.format_violations(found)


def _secret_scan(instance: Path) -> list[str]:
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        return ["gitleaks is not on PATH (run jarvis doctor)"]
    proc = subprocess.run(
        [gitleaks, "detect", "--source", str(instance), "--redact", "--no-banner"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return []
    return [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()] or ["gitleaks found a leak"]


def _scan(module) -> Callable[[Path], list[str]]:
    """A check whose module has scan_<name>(instance) and format_violations."""
    name = module.__name__.rsplit(".", 1)[1]
    scan = getattr(module, f"scan_{name}")
    return lambda instance: module.format_violations(scan(instance))


# Name, then the check. Core's CI runs each under the same name, so a failure reads the same everywhere.
CHECKS: dict[str, Callable[[Path], list[str]]] = {
    "env-schema": _env_schema,
    "addons": lambda instance: addons.format_violations(addons.scan_addons(_paths.core_root(), instance)),
    "app-hosting": _scan(app_hosting),
    "backup-isolation": _scan(backup_isolation),
    "flux-identity": _scan(flux_identity),
    "flux-prune": _scan(flux_prune),
    "manifest-lint": _manifest_lint,
    "secret-scan": _secret_scan,
}


def run(instance: Path, only: list[str] | None = None) -> int:
    """Run the checks (all, or the ones named), printing PASS or FAIL for each."""
    failed = []
    for name in only or list(CHECKS):
        # The checks read core's trees relative to the core checkout.
        with contextlib.chdir(PRODUCT_ROOT):
            problems = CHECKS[name](instance)
        if problems:
            failed.append(name)
            print(f"FAIL {name}", flush=True)
            for line in problems:
                print(f"  {line}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", choices=list(CHECKS), help="Run only this check (repeatable)")
    args = parser.parse_args(argv)
    return run(instance_root(PRODUCT_ROOT), args.only)


if __name__ == "__main__":
    sys.exit(main())

"""Fail-closed verification of one environment: read-only standing checks, and with --smoke the
short-lived probes (jarvis/verify/smoke.py), which make their objects in namespace platform-smoke
and delete them again."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

from jarvis.apps import values as _apps
from jarvis.env import switches as _switches
from jarvis.env.runtime import (
    EnvError,
    instance_root,
    kubeconfig_for,
    repository_values,
    validate_cluster,
)
from jarvis.env.runtime import (
    kubectl as run_kubectl,
)
from jarvis.paths import PRODUCT_ROOT
from jarvis.schema.env import core_dir, enabled_addons
from jarvis.verify import apps as _apps_checks  # noqa: F401  (importing it registers its checks)
from jarvis.verify import backups as _backups  # noqa: F401  (importing it registers its checks)
from jarvis.verify import platform as _platform
from jarvis.verify import smoke as _smoke
from jarvis.verify.common import CheckError, CheckWarning, Context
from jarvis.verify.platform import (
    _fetch,
    _inventory,
    _issuer,
    check_https,
    traefik_address,
)
from jarvis.verify.registry import collect, enabled_components, load_addons

REPO_ROOT = PRODUCT_ROOT

REQUEST_TIMEOUT = "30s"
KUBECTL_TIMEOUT = 60.0
Kubectl = _platform.Kubectl
Fetch = _platform.Fetch
Issuer = _platform.Issuer


def _line(text: str) -> None:
    print(text, flush=True)


def bound_kubectl(
    args: Sequence[str],
    input: str | None = None,
    *,
    repo_root: Path,
    kubeconfig: Path,
) -> CompletedProcess[str]:
    return run_kubectl(
        args,
        input=input,
        repo_root=repo_root,
        kubeconfig=kubeconfig,
        request_timeout=REQUEST_TIMEOUT,
        timeout=KUBECTL_TIMEOUT,
    )


def run(
    repo_root: Path,
    env: str,
    kubectl: Kubectl,
    fetch: Fetch,
    issuer: Issuer,
    *,
    now: datetime | None = None,
    enabled: Sequence[str] | None = None,
    smoke: bool = False,
) -> int:
    try:
        values = repository_values(repo_root, env)
        validate_cluster(kubectl, env)
    except EnvError as exc:
        _line(f"FAIL environment: {exc}")
        return 1

    try:
        roles = _inventory(repo_root, env)
    except CheckError as exc:
        _line(f"FAIL inventory: {exc}")
        return 1

    ctx = Context(
        repo_root=repo_root,
        env=env,
        kubectl=kubectl,
        values=values,
        now=now or datetime.now(UTC),
        roles=roles,
    )
    try:
        if enabled is None:
            # The env's add-on list; checks for an add-on it does not enable never load.
            # The core the env reads: the checkout running this, or a tree laid out like the artifact.
            enabled = load_addons(core_dir(repo_root), enabled_addons(repo_root / "envs" / env))
            # Hosted-app checks only for an env that lists an app.
            apps_data, apps_errors = _apps.load_values(repo_root / "envs" / env)
            if apps_errors:
                raise CheckError(f"values: {'; '.join(apps_errors)}")
            if _apps.app_names(apps_data):
                enabled = (*enabled, "apps")
        names = enabled_components(enabled)
        checks = collect(names, ctx)
    except CheckError as exc:
        _line(f"FAIL verify components: {exc}")
        return 1

    failed = _report(checks)
    switches, _ = _switches.load(repo_root / "envs" / env)
    app_dns = _switches.APP_DNS in _switches.names(switches, _switches.FEATURES)
    try:
        # With App DNS off, the LAN's own DNS names apps; verify reaches Traefik's address directly.
        address = None if app_dns else traefik_address(kubectl)
        cert_issuer = check_https(
            repo_root, env, str(values["dns_suffix"]), fetch, issuer, address=address
        )
    except (CheckError, KeyError, TypeError) as exc:
        _line(f"FAIL LAN HTTPS and certificate: {exc}")
        failed += 1
    else:
        _line(f"PASS LAN HTTPS and certificate issuer={cert_issuer}")
    if smoke:
        failed += _run_smoke(ctx, (*names, _switches.APP_DNS) if app_dns else names)
    return int(bool(failed))


def _report(checks: Sequence[tuple[str, Callable[[], object]]]) -> int:
    """Run each check and print its line; returns how many failed."""
    failed = 0
    for label, check in checks:
        try:
            check()
        except CheckWarning as exc:
            _line(f"WARN {label}: {exc}")
        except (CheckError, EnvError, KeyError, TypeError) as exc:
            _line(f"FAIL {label}: {exc}")
            failed += 1
        else:
            _line(f"PASS {label}")
    return failed


def _run_smoke(ctx: Context, names: Sequence[str]) -> int:
    """The enabled components' probes, between creating platform-smoke and deleting it."""
    try:
        probes = collect(names, ctx, smoke=True)
        _smoke.open_scratch(ctx)
    except (CheckError, EnvError) as exc:
        _line(f"FAIL smoke: {exc}")
        return 1
    try:
        failed = _report(probes)
    finally:
        try:
            _smoke.close_scratch(ctx)
        except (CheckError, EnvError) as exc:
            _line(f"FAIL smoke: {exc}; delete namespace {_smoke.NAMESPACE} by hand")
            failed = 1
        else:
            _line(f"PASS smoke: namespace {_smoke.NAMESPACE} removed")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Environment under envs/")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Also run the short-lived probes: each makes its objects in namespace platform-smoke, checks them and deletes them",
    )
    args = parser.parse_args(argv)
    kubeconfig = kubeconfig_for(args.env)
    if not kubeconfig.is_file():
        _line(f"FAIL kubeconfig does not exist: {kubeconfig}")
        return 1

    def bound(args_: Sequence[str], input: str | None = None) -> CompletedProcess[str]:
        return bound_kubectl(
            args_,
            input=input,
            repo_root=REPO_ROOT,
            kubeconfig=kubeconfig,
        )

    return run(instance_root(REPO_ROOT), args.env, bound, _fetch, _issuer, smoke=args.smoke)


if __name__ == "__main__":
    sys.exit(main())

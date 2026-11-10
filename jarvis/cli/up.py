"""jarvis up: bring an env from its committed files to verify PASS.

doctor, validate, render --check, secrets (mints only what is missing), a refusal while the
env's files differ from what Flux reads, site, a wait for every Flux Kustomization to be
Ready, then verify. Each step must pass before the next starts; the first failure stops it with
that step's exit code.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from jarvis.cli import doctor
from jarvis.env import validate
from jarvis.env.runtime import kubeconfig_for, kubectl
from jarvis.node import site
from jarvis.paths import PRODUCT_ROOT
from jarvis.render import env as render
from jarvis.secrets import mint
from jarvis.verify import run as verify

FLUX_READY_TIMEOUT_SECONDS = 20 * 60


def _git(instance: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(instance), *args], capture_output=True, text=True, check=False)


def unpushed(instance: Path, env: str) -> str | None:
    """Why Flux would not read what this checkout holds for the env, or None when it would."""
    env_rel = f"envs/{env}"
    if _git(instance, "status", "--porcelain", "--", env_rel, ".sops.yaml").stdout.strip():
        return f"{env_rel} or .sops.yaml has uncommitted changes; commit and push them first"
    data = yaml.safe_load((instance / env_rel / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    ref = str((data or {}).get("flux_git_ref") or "")
    if not ref:
        return f"{env_rel}/values/cluster-vars.yaml sets no flux_git_ref"
    if _git(instance, "fetch", "--quiet", "origin", ref).returncode:
        return f"cannot fetch {ref} from origin"
    if _git(instance, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD").returncode:
        return f"this checkout has commits that {ref} on origin lacks; push them first"
    return None


_FLUX_KUSTOMIZATION = "kustomize.toolkit.fluxcd.io/"
POLL_SECONDS = 10
# Kept as module names so the tests can replace them.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def expected_kustomizations(env_dir: Path) -> set[str]:
    """The Flux Kustomizations the env's wiring declares: core's layers and every one in its
    rendered flux/ (the root, services, the app wiring). Add-ons' appear once services applies."""
    names: set[str] = set()
    for path in [*sorted((PRODUCT_ROOT / "core" / "flux" / "layers").glob("*.yaml")), *sorted((env_dir / "flux").rglob("*.yaml"))]:
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "Kustomization" and str(doc.get("apiVersion", "")).startswith(_FLUX_KUSTOMIZATION):
                names.add(str((doc.get("metadata") or {}).get("name")))
    return names


def _not_ready(items: list[dict]) -> dict[str, str]:
    """Each listed Kustomization that is not Ready, with its Ready condition's message."""
    waiting = {}
    for item in items:
        ready = next((c for c in (item.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"), {})
        if ready.get("status") != "True":
            waiting[str(item["metadata"]["name"])] = str(ready.get("message") or "no Ready condition yet")
    return waiting


def wait_for_flux(env: str, env_dir: Path) -> int:
    """Until every expected Kustomization exists and every one in flux-system has been Ready on
    two polls in a row: a layer applies its children after it is Ready, so one pass is not enough.
    `kubectl wait --all` would see only the root, the one object that exists when it starts."""
    expected = expected_kustomizations(env_dir)
    deadline = _clock() + FLUX_READY_TIMEOUT_SECONDS
    settled, waiting = 0, {name: "not created yet" for name in expected}
    while _clock() < deadline:
        proc = kubectl(
            ["get", "kustomizations.kustomize.toolkit.fluxcd.io", "-n", "flux-system", "-o", "json"],
            repo_root=PRODUCT_ROOT,
            kubeconfig=kubeconfig_for(env),
        )
        if proc.returncode == 0:
            items = (json.loads(proc.stdout or "{}") or {}).get("items") or []
            listed = {str(item["metadata"]["name"]) for item in items}
            waiting = _not_ready(items) | {name: "not created yet" for name in expected - listed}
            settled = settled + 1 if not waiting else 0
            if settled >= 2:
                print(f"PASS up: every Flux Kustomization is Ready ({len(listed)})", flush=True)
                return 0
        _sleep(POLL_SECONDS)
    detail = "; ".join(f"{name}: {why}" for name, why in sorted(waiting.items()))
    print(f"FAIL up: Flux is not Ready after {FLUX_READY_TIMEOUT_SECONDS // 60} minutes: {detail}", flush=True)
    return 1


def run(instance: Path, env: str) -> int:
    env_dir = instance / "envs" / env
    steps: list[tuple[str, Callable[[], int]]] = [
        ("doctor", lambda: doctor.run(instance, env_dir)),
        ("validate", lambda: validate.run(instance)),
        ("render --check", lambda: render.main(["--env", env, "--check"])),
        ("secrets", lambda: mint.main(["--env", env])),
        ("pushed", lambda: _refuse_unpushed(instance, env)),
        ("site", lambda: site.main(["--env", env])),
        ("flux", lambda: wait_for_flux(env, env_dir)),
        ("verify", lambda: verify.main(["--env", env])),
    ]
    for name, step in steps:
        print(f"== up: {name}", flush=True)
        code = step()
        if code:
            print(f"FAIL up: stopped at {name} (exit {code})", flush=True)
            return code
    print(f"PASS up: {env} is up", flush=True)
    return 0


def _refuse_unpushed(instance: Path, env: str) -> int:
    problem = unpushed(instance, env)
    if problem:
        print(f"REFUSE up: {problem}", flush=True)
        return 3
    print("PASS up: Flux reads what this checkout holds", flush=True)
    return 0

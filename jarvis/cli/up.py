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

from jarvis.checks.flux_build import env_tree
from jarvis.cli import doctor
from jarvis.env import validate
from jarvis.env.runtime import kubeconfig_for, kubectl
from jarvis.node import site
from jarvis.paths import PRODUCT_ROOT
from jarvis.render import env as render
from jarvis.schema.env import ROOT_SOURCE
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


POLL_SECONDS = 10
# Kept as module names so the tests can replace them.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def expected_kustomizations(instance: Path, env: str) -> tuple[dict[str, bool], str | None]:
    """Every Flux Kustomization the env applies, from Flux's build of its tree (the root, core's
    layers, add-ons, each app's wiring and its own Kustomizations), each with whether it reads the
    root source; or why the tree does not build."""
    tree = env_tree(instance, f"envs/{env}/flux")
    if tree.error:
        return {}, tree.error
    expected = {ROOT_SOURCE: True}
    for path in tree.paths:
        source = (path.kustomization.get("spec") or {}).get("sourceRef") or {}
        expected[str((path.kustomization.get("metadata") or {}).get("name"))] = source.get("name") == ROOT_SOURCE
    return expected, None


def pushed_revision(instance: Path, env: str) -> str | None:
    """The commit Flux reads for the env: the tip of its flux_git_ref on origin."""
    data = yaml.safe_load((instance / "envs" / env / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    ref = str((data or {}).get("flux_git_ref") or "")
    proc = _git(instance, "ls-remote", "origin", ref)
    line = proc.stdout.split("\n", 1)[0].split() if proc.returncode == 0 else []
    return line[0] if line else None


def _waiting(items: list[dict], expected: dict[str, bool], revision: str) -> dict[str, str]:
    """Each expected Kustomization that is not yet done, with why: not created, not Ready, Ready on
    an older spec, or (reading the root source) not yet at the pushed commit."""
    listed = {str(item["metadata"]["name"]): item for item in items}
    waiting = {}
    for name, root_source in sorted(expected.items()):
        item = listed.get(name)
        if item is None:
            waiting[name] = "not created yet"
            continue
        status = item.get("status") or {}
        ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
        if ready.get("status") != "True":
            waiting[name] = str(ready.get("message") or "no Ready condition yet")
        elif status.get("observedGeneration") != (item.get("metadata") or {}).get("generation"):
            waiting[name] = "Ready for an older spec; reconciling"
        elif root_source and not str(status.get("lastAppliedRevision") or "").endswith(revision):
            waiting[name] = f"not yet at {revision[:12]}"
    return waiting


def wait_for_flux(instance: Path, env: str) -> int:
    """Until every Kustomization the env's tree applies exists, is Ready for its current spec, and,
    if it reads the root source, has applied the pushed commit. The tree says what to wait for, so
    a layer that applies its children only once it is Ready is waited for like any other."""
    expected, error = expected_kustomizations(instance, env)
    revision = pushed_revision(instance, env)
    if error or revision is None:
        print(f"FAIL up: {error or 'cannot read the pushed commit of flux_git_ref from origin'}", flush=True)
        return 1
    deadline = _clock() + FLUX_READY_TIMEOUT_SECONDS
    waiting = {name: "not created yet" for name in expected}
    while _clock() < deadline:
        proc = kubectl(
            ["get", "kustomizations.kustomize.toolkit.fluxcd.io", "-n", "flux-system", "-o", "json"],
            repo_root=PRODUCT_ROOT,
            kubeconfig=kubeconfig_for(env),
        )
        if proc.returncode == 0:
            waiting = _waiting((json.loads(proc.stdout or "{}") or {}).get("items") or [], expected, revision)
            if not waiting:
                print(f"PASS up: every Flux Kustomization is Ready at {revision[:12]} ({len(expected)})", flush=True)
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
        ("flux", lambda: wait_for_flux(instance, env)),
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

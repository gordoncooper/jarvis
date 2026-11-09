"""jarvis up: bring an env from its committed files to verify PASS.

doctor, validate, render --check, secrets (mints only what is missing), a refusal while the
env's files differ from what Flux reads, site, a wait for every Flux Kustomization to be
Ready, then verify. Each step must pass before the next starts; the first failure stops it with
that step's exit code.
"""

from __future__ import annotations

import subprocess
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

FLUX_READY_TIMEOUT = "20m"


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


def wait_for_flux(env: str) -> int:
    proc = kubectl(
        ["wait", "kustomizations.kustomize.toolkit.fluxcd.io", "--all", "-n", "flux-system",
         "--for=condition=Ready", f"--timeout={FLUX_READY_TIMEOUT}"],
        repo_root=PRODUCT_ROOT,
        kubeconfig=kubeconfig_for(env),
    )  # fmt: skip
    if proc.returncode:
        print(f"FAIL up: Flux is not Ready after {FLUX_READY_TIMEOUT}: {(proc.stderr or proc.stdout).strip()}", flush=True)
        return 1
    print("PASS up: every Flux Kustomization is Ready", flush=True)
    return 0


def run(instance: Path, env: str) -> int:
    env_dir = instance / "envs" / env
    steps: list[tuple[str, Callable[[], int]]] = [
        ("doctor", lambda: doctor.run(instance, env_dir)),
        ("validate", lambda: validate.run(instance)),
        ("render --check", lambda: render.main(["--env", env, "--check"])),
        ("secrets", lambda: mint.main(["--env", env])),
        ("pushed", lambda: _refuse_unpushed(instance, env)),
        ("site", lambda: site.main(["--env", env])),
        ("flux", lambda: wait_for_flux(env)),
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

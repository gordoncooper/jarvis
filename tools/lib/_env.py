"""Shared environment binding for operator tools."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess

import yaml

Kubectl = Callable[..., CompletedProcess[str]]
ENV_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class EnvError(Exception):
    pass


def tool_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    prefix = os.pathsep.join(
        (str(repo_root / ".venv" / "bin"), str(Path.home() / ".local" / "bin"))
    )
    env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


def kubeconfig_for(env: str) -> Path:
    return Path.home() / ".kube" / f"jarvis-{env}.yaml"


def kubectl(
    args: Sequence[str],
    input: str | None = None,
    *,
    repo_root: Path,
    kubeconfig: Path,
) -> CompletedProcess[str]:
    try:
        return subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), *args],
            input=input,
            env=tool_env(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("run make deps", flush=True)
        raise SystemExit(1) from None


def reason(proc: CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "kubectl failed").strip()
    return text.splitlines()[0] if text else "kubectl failed"


def not_found(proc: CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    text = f"{proc.stderr}\n{proc.stdout}".lower()
    return "notfound" in text or "not found" in text


def json_object(proc: CompletedProcess[str], what: str) -> dict:
    if proc.returncode != 0:
        raise EnvError(f"{what}: {reason(proc)}")
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EnvError(f"{what}: invalid kubectl JSON") from exc
    if not isinstance(value, dict):
        raise EnvError(f"{what}: invalid kubectl JSON")
    return value


def repository_values(repo_root: Path, env: str) -> dict:
    if not ENV_NAME.fullmatch(env):
        raise EnvError(f"invalid ENV={env}")
    path = repo_root / "envs" / env / "values" / "cluster-vars.yaml"
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EnvError(f"ENV={env} is not a valid repository environment") from exc
    if not isinstance(values, dict) or values.get("jarvis_env") != env:
        raise EnvError(f"ENV={env} does not match its repository marker")
    return values


def validate_cluster(kubectl_: Kubectl, env: str) -> None:
    body = json_object(
        kubectl_(
            ["get", "configmap", "cluster-vars", "-n", "flux-system", "-o", "json"]
        ),
        "read cluster environment marker",
    )
    if (body.get("data") or {}).get("jarvis_env") != env:
        raise EnvError(f"refusing cluster whose environment marker is not ENV={env}")

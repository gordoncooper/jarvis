"""jarvis doctor: is this controller ready to run the env? Changes nothing but a missing hook.

Checks the pinned CLIs, the age key's mode and owner, the instance repo's remote, and that
every inventory host resolves, and installs the pre-commit hook in a clone that lacks it. Exit 0
when nothing fails, 4 when something does.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import yaml

# The commands jarvis runs on the controller: mise.toml's CLIs, Ansible from the venv, and the
# system's git and ssh.
TOOLS = (
    "sops", "age", "age-keygen", "kubectl", "flux", "kustomize", "kubeconform", "kyverno",
    "gitleaks", "restic", "ansible-playbook", "git", "ssh", "ssh-keygen",
)  # fmt: skip


def _values(env_dir: Path) -> dict:
    try:
        data = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _hosts(env_dir: Path) -> list[str]:
    try:
        data = yaml.safe_load((env_dir / "inventory" / "hosts.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    hosts = ((data or {}).get("all") or {}).get("hosts") or {}
    return sorted(hosts) if isinstance(hosts, dict) else []


def check_tools() -> list[tuple[str, str]]:
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        return [("FAIL", f"not on PATH: {', '.join(missing)} (git and ssh come from the system; the rest from mise)")]
    return [("PASS", "every CLI is on PATH")]


def check_age_key(env_dir: Path) -> list[tuple[str, str]]:
    raw = _values(env_dir).get("age_key_file") or "~/.config/sops/age/keys.txt"
    path = Path(str(raw)).expanduser()
    try:
        info = path.stat()
    except OSError:
        return [("FAIL", f"age key {path} is missing; copy it from where you keep it (mode 0600)")]
    problems = []
    if info.st_uid != os.getuid():
        problems.append("is not yours")
    if stat.S_IMODE(info.st_mode) & 0o077:
        problems.append(f"is mode {stat.S_IMODE(info.st_mode):04o}, not 0600 (chmod 600 {path})")
    if problems:
        return [("FAIL", f"age key {path} {' and '.join(problems)}")]
    return [("PASS", f"age key {path} is yours and mode 0600")]


def check_remote(instance: Path) -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["git", "-C", str(instance), "ls-remote", "--exit-code", "origin", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode:
        return [("FAIL", "cannot read the instance repo's origin remote (git ls-remote origin)")]
    return [("PASS", "the instance repo's origin remote is readable")]


def check_hosts(env_dir: Path) -> list[tuple[str, str]]:
    results = []
    for host in _hosts(env_dir):
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            results.append(("FAIL", f"{host} does not resolve; add it on the router or in your DNS"))
        else:
            results.append(("PASS", f"{host} resolves"))
    return results or [("WARN", "the inventory lists no host")]


def check_recovery(instance: Path, env_dir: Path) -> list[tuple[str, str]]:
    """An env's secrets open for the working key and an offline recovery key."""
    from jarvis.secrets.age import recipients

    keys = recipients(instance / ".sops.yaml", env_dir.name)
    if len(keys) >= 2:
        return [("PASS", f"envs/{env_dir.name}/secrets/ open for {len(keys)} age keys (working and recovery)")]
    message = f"envs/{env_dir.name}/secrets/ open for one age key only; add the recovery key: jarvis secrets --add-recovery-key"
    return [("WARN", message)]


def check_hook(instance: Path) -> list[tuple[str, str]]:
    """The pre-commit hook jarvis init installs: a fresh clone does not have it."""
    hook = instance / ".git" / "hooks" / "pre-commit"
    if hook.is_file() and "jarvis" in hook.read_text(encoding="utf-8", errors="ignore"):
        return [("PASS", "the pre-commit hook runs jarvis render --check and validate")]
    from jarvis.env.scaffold import install_hook

    if (instance / ".git").is_dir() and not hook.exists():
        install_hook(instance)
        return [("PASS", "installed the pre-commit hook (a fresh clone has none)")]
    return [("WARN", f"{hook} is not jarvis's; commits are not checked before they are made")]


def run(instance: Path, env_dir: Path) -> int:
    results = (
        check_tools()
        + check_age_key(env_dir)
        + check_remote(instance)
        + check_hosts(env_dir)
        + check_recovery(instance, env_dir)
        + check_hook(instance)
    )
    for status, message in results:
        print(f"{status} doctor: {message}", flush=True)
    return 4 if any(status == "FAIL" for status, _ in results) else 0

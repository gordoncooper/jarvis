"""The env's core pin: a command acts on an env only with the core release the env deploys.

An env pins core with core_git_ref in values/cluster-vars.yaml, a tag (refs/tags/<tag>). A
checkout at another commit, with uncommitted changes to tracked files, or for an env that follows
a branch would act with code the cluster does not run, so the jarvis CLI runs the pinned tag
instead (jarvis/cli/core.py) and says why when --core runs another checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

TAG_PREFIX = "refs/tags/"


class PinError(Exception):
    pass


def _git(core_root: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(core_root), *args], capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def core_ref(env_dir: Path) -> str:
    path = env_dir / "values" / "cluster-vars.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PinError(f"{path}: unreadable ({exc})") from exc
    ref = data.get("core_git_ref") if isinstance(data, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        raise PinError(f"{path}: core_git_ref is not set")
    return ref.strip()


def pin_problem(core_root: Path, env_dir: Path) -> str | None:
    """Why the core checkout is not the env's pin, or None when it is."""
    ref = core_ref(env_dir)
    env = env_dir.name
    if not ref.startswith(TAG_PREFIX):
        return f"{env} follows {ref}, not a tag; pin core_git_ref to refs/tags/<tag>"
    tag = ref.removeprefix(TAG_PREFIX)
    tagged = _git(core_root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if tagged is None:
        return f"{env} pins core {tag}, which this checkout does not have"
    head = _git(core_root, "rev-parse", "HEAD")
    if head != tagged:
        return f"{env} pins core {tag} ({tagged[:12]}), but this checkout is at {(head or 'no commit')[:12]}"
    if _git(core_root, "status", "--porcelain", "--untracked-files=no"):
        return f"this checkout has uncommitted changes to tracked files, so it is not {tag}"
    return None

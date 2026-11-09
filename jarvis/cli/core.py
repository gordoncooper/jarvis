"""Which core checkout runs a command: the release the env pins.

An env pins core with core_git_ref (refs/tags/<tag>) and core_git_repo_url in its
values/cluster-vars.yaml. A command runs here when this checkout is exactly that tag. Otherwise
it runs from a checkout of the tag in the user's cache, set up from that release's own mise.lock
and uv.lock. So the CLI, the checks and the cluster always run the same core, and two envs in
one repo can pin different releases.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from jarvis.env.pin import TAG_PREFIX, PinError, core_ref, pin_problem
from jarvis.paths import PRODUCT_ROOT, cache_dir

# Set in a command that runs from a cached checkout, so that checkout never hands off again.
DELEGATED_VAR = "JARVIS_DELEGATED"
# Set by --core: run that checkout as it is, whatever the env pins.
UNPINNED_VAR = "JARVIS_UNPINNED"
_READY = ".jarvis-ready"


class CoreError(Exception):
    """The pinned core cannot be checked out or set up (exit 4)."""


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise CoreError(f"{' '.join(command[:3])} failed: {detail[-1] if detail else 'no output'}")


def core_url(env_dir: Path) -> str:
    path = env_dir / "values" / "cluster-vars.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PinError(f"{path}: unreadable ({exc})") from exc
    url = data.get("core_git_repo_url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise PinError(f"{path}: core_git_repo_url is not set")
    return url.strip()


def runs_here(instance: Path, env_dir: Path) -> bool:
    """True when this checkout should run the command itself."""
    if os.environ.get(UNPINNED_VAR) or instance == PRODUCT_ROOT:
        # --core, or core's own envs: the checkout is the code under test.
        return True
    return pin_problem(PRODUCT_ROOT, env_dir) is None


def pinned_tag(env_dir: Path) -> str:
    ref = core_ref(env_dir)
    if not ref.startswith(TAG_PREFIX):
        raise PinError(f"{env_dir.name} follows {ref}, not a tag; pin core_git_ref to refs/tags/<tag>")
    return ref.removeprefix(TAG_PREFIX)


def checkout(env_dir: Path) -> Path:
    """The cached checkout of the env's pinned core, cloned and set up on first use."""
    tag = pinned_tag(env_dir)
    dest = cache_dir() / "core" / tag
    if not (dest / _READY).is_file():
        if not dest.is_dir():
            partial = dest.with_name(f".{tag}.partial")
            shutil.rmtree(partial, ignore_errors=True)
            partial.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, core_url(env_dir), str(partial)])
            partial.rename(dest)
        setup(dest)
    return dest


def setup(core: Path) -> None:
    """Install a core checkout's pinned CLIs and Python packages: that release's own deps task."""
    if shutil.which("mise") is None:
        raise CoreError("mise is not installed: https://mise.jdx.dev/getting-started.html")
    if not (core / "jarvis" / "cli").is_dir():
        raise CoreError(f"{core} predates the jarvis CLI (v0.5.0); pin core_git_ref to v0.5.0 or later")
    _run(["mise", "trust", "--quiet", str(core / "mise.toml")])
    # The venv belongs in the checkout whatever the caller's environment says: hand_off runs it.
    _run(["mise", "-C", str(core), "run", "deps"], env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(core / ".venv")})
    (core / _READY).write_text("ok\n", encoding="utf-8")


def hand_off(core: Path, argv: list[str]) -> None:
    """Run the command with that checkout's own CLI, replacing this process."""
    program = core / ".venv" / "bin" / "jarvis"
    if not program.is_file():
        raise CoreError(f"{program} is missing; delete {core} and run again")
    os.execve(program, [str(program), *argv], {**os.environ, DELEGATED_VAR: "1"})

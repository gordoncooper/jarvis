"""Shared SOPS encryption helpers (bootstrap-secrets, seal)."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

# One path segment. Rejects "..", slashes, and uppercase.
_SEGMENT = re.compile(r"^[a-z0-9-]+$")


def secret_path(repo_root: Path, env: str, name: str) -> Path:
    """Return envs/<env>/secrets/<name>.sops.yaml, or refuse before sops runs.

    The env directory must already exist. The resolved file's parent must be
    that env's secrets directory, so a symlinked secrets/ cannot leave the repo.
    """
    if not _SEGMENT.fullmatch(env) or not _SEGMENT.fullmatch(name):
        raise SystemExit("refusing env or name: only [a-z0-9-] segments are allowed")
    root = repo_root.resolve()
    env_dir = root / "envs" / env
    if not env_dir.is_dir():
        raise SystemExit("env folder does not exist")
    expected_parent = env_dir / "secrets"
    destination = expected_parent / f"{name}.sops.yaml"
    if destination.resolve().parent != expected_parent:
        raise SystemExit("refusing a secret path outside envs/<env>/secrets")
    return repo_root / "envs" / env / "secrets" / f"{name}.sops.yaml"


def require_sops() -> str:
    sops = shutil.which("sops")
    if not sops:
        raise SystemExit(
            "sops not found on PATH; install sops (https://github.com/getsops/sops) "
            "and age (https://github.com/FiloSottile/age) for the age backend"
        )
    return sops


def encrypt_secret_to_file(
    *,
    repo_root: Path,
    env: str,
    name: str,
    document: dict,
    sops_bin: str,
) -> None:
    """Write an encrypted Secret manifest under envs/<env>/secrets/.

    Plaintext is piped to sops on stdin and is not written to disk. The
    ciphertext is renamed into place so a failed encrypt leaves no target file.
    """
    destination = secret_path(repo_root, env, name).resolve()
    repo_root = repo_root.resolve()
    override = destination.relative_to(repo_root).as_posix()
    payload = yaml.safe_dump(document, sort_keys=False)

    result = subprocess.run(
        [sops_bin, "encrypt", f"--filename-override={override}", "/dev/stdin"],
        cwd=repo_root,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            result.stdout,
            result.stderr,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    enc_tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=destination.parent,
            prefix=".sops-",
            suffix=".yaml",
        ) as enc_tmp:
            enc_tmp.write(result.stdout)
            enc_tmp_path = Path(enc_tmp.name)
        enc_tmp_path.replace(destination)
    finally:
        if enc_tmp_path is not None and enc_tmp_path.exists():
            enc_tmp_path.unlink(missing_ok=True)

"""Shared SOPS encryption helpers (bootstrap-secrets, seal)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml


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
    destination: Path,
    document: dict,
    sops_bin: str,
) -> None:
    """Write an encrypted Secret manifest to destination; plaintext never touches that path."""
    destination = destination.resolve()
    repo_root = repo_root.resolve()
    override = destination.relative_to(repo_root).as_posix()

    fd, plaintext_path = tempfile.mkstemp(prefix="jarvis-secret-", suffix=".yaml")
    os.close(fd)
    plaintext = Path(plaintext_path)
    enc_tmp_path: Path | None = None
    try:
        os.chmod(plaintext, 0o600)
        plaintext.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        result = subprocess.run(
            [sops_bin, "encrypt", f"--filename-override={override}", str(plaintext)],
            cwd=repo_root,
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
        plaintext.unlink(missing_ok=True)
        if enc_tmp_path is not None and enc_tmp_path.exists():
            enc_tmp_path.unlink(missing_ok=True)

# seed (do not merge): subprocess.run(["bash", "ci/run-checks"])

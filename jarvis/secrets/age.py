"""The env's age keys: the working key on the controller, and the recipients in .sops.yaml.

Every env's secrets are encrypted to two recipients: the working key, kept on the controller at
age_key_file, and an offline recovery key, whose private half is shown once by jarvis init and
then lives only where the owner keeps it. Losing the working key is then not fatal.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

DEFAULT_KEY_FILE = "~/.config/sops/age/keys.txt"
PUBLIC_KEY = re.compile(r"age1[0-9a-z]{58}")


class AgeError(Exception):
    pass


def key_path(raw: str | None) -> Path:
    """The working key's path: SOPS_AGE_KEY_FILE when set, else age_key_file, else the default."""
    return Path(os.environ.get("SOPS_AGE_KEY_FILE") or raw or DEFAULT_KEY_FILE).expanduser()


def public_keys(path: Path) -> list[str]:
    """The public key of each identity in a key file, newest first (jarvis secrets --rotate age
    puts the new identity first and leaves the old one until the owner retires it)."""
    proc = subprocess.run(["age-keygen", "-y", str(path)], capture_output=True, text=True, check=False)
    found = proc.stdout.split()
    if proc.returncode or not found or not all(PUBLIC_KEY.fullmatch(key) for key in found):
        raise AgeError(f"could not read the age public key from {path}")
    return found


def public_key(path: Path) -> str:
    """The key file's newest identity's public key."""
    return public_keys(path)[0]


def create_key(path: Path) -> str:
    """Write a new key at path (mode 0600, its folder 0700). Never overwrites. Returns its public key."""
    if path.exists():
        raise AgeError(f"{path} already exists; not overwriting")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    proc = subprocess.run(["age-keygen", "-o", str(path)], capture_output=True, text=True, check=False)
    if proc.returncode or not path.is_file():
        path.unlink(missing_ok=True)
        raise AgeError("age-keygen failed")
    path.chmod(0o600)
    return public_key(path)


def new_key_in_memory() -> tuple[str, str]:
    """A new key that never touches disk: (its private key line, its public key)."""
    proc = subprocess.run(["age-keygen"], capture_output=True, text=True, check=False)
    secret = next((line for line in proc.stdout.splitlines() if line.startswith("AGE-SECRET-KEY-")), "")
    found = PUBLIC_KEY.search(proc.stderr + proc.stdout)
    if proc.returncode or not secret or not found:
        raise AgeError("age-keygen failed")
    return secret, found.group(0)


def recipients(sops_config: Path, env: str) -> list[str]:
    """The age recipients of the env's creation rule in .sops.yaml, in order; empty when none."""
    try:
        data = yaml.safe_load(sops_config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    needle = f"envs/{env}/secrets/"
    for rule in data.get("creation_rules") or []:
        if isinstance(rule, dict) and needle in str(rule.get("path_regex", "")):
            return [part.strip() for part in str(rule.get("age", "")).split(",") if part.strip()]
    return []


def creation_rule(env: str, public_keys: list[str]) -> str:
    """The .sops.yaml creation rule block for one env, encrypting to every key given."""
    if not re.fullmatch(r"[a-z0-9-]+", env) or not public_keys or not all(PUBLIC_KEY.fullmatch(k) for k in public_keys):
        raise AgeError("could not write an age creation rule")
    return (
        f"  - path_regex: envs/{env}/secrets/.*\\.sops\\.yaml$\n"
        f"    encrypted_regex: ^(data|stringData)$\n"
        f"    age: {','.join(public_keys)}\n"
    )

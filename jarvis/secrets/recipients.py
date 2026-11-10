"""The env's age recipients over its life: who every sealed file opens for, and changing that.

Each env's creation rule in .sops.yaml names its recipients: the working key and the offline
recovery key. Every sealed file under envs/<env>/secrets/ is encrypted to exactly those
(jarvis validate checks it). The operations here change the rule and re-encrypt the files to
match, with sops updatekeys; each needs the working key to open the files, and none prints a
secret value.

  update     re-encrypt every file whose recipients differ from the rule's
  recovery   give an env with one recipient its offline recovery key (shown once)
  rotate     a new working key in place of the old (or lost) one: added to the key file, put in
             the rule, every file re-encrypted
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from jarvis.secrets import age as _age


class RecipientsError(Exception):
    pass


def sealed_files(env_dir: Path) -> list[Path]:
    secrets = env_dir / "secrets"
    return sorted(secrets.rglob("*.sops.yaml")) if secrets.is_dir() else []


def file_recipients(path: Path) -> list[str] | None:
    """The age recipients a sealed file is encrypted to, or None when it is not SOPS-encrypted."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    meta = doc.get("sops") if isinstance(doc, dict) else None
    entries = meta.get("age") if isinstance(meta, dict) else None
    if not isinstance(entries, list) or not entries:
        return None
    found = [entry.get("recipient") for entry in entries if isinstance(entry, dict)]
    return [str(item) for item in found if item] or None


def problems(instance: Path, env_dir: Path) -> list[str]:
    """Why a sealed file does not open for exactly the env's recipients; empty when every one does."""
    wanted = _age.recipients(instance / ".sops.yaml", env_dir.name)
    if not wanted:
        return []
    errors = []
    for path in sealed_files(env_dir):
        rel = path.relative_to(instance).as_posix()
        found = file_recipients(path)
        if found is None:
            errors.append(f"{rel}: not SOPS-encrypted with age; seal it with jarvis encrypt")
        elif set(found) != set(wanted):
            errors.append(
                f"{rel}: encrypted to {len(found)} age recipient(s) that are not the {len(wanted)} in "
                f".sops.yaml's rule for envs/{env_dir.name}/secrets/; run jarvis secrets --update-recipients"
            )
    return errors


def set_rule(sops_config: Path, env: str, keys: list[str]) -> None:
    """Replace the recipients of the env's creation rule, keeping the rest of the file as it is."""
    lines = sops_config.read_text(encoding="utf-8").splitlines(keepends=True)
    needle, in_rule = f"envs/{env}/secrets/", False
    for i, line in enumerate(lines):
        if "path_regex:" in line:
            in_rule = needle in line
        elif in_rule and re.match(r"\s*age:\s*\S", line):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}age: {','.join(keys)}\n"
            sops_config.write_text("".join(lines), encoding="utf-8")
            return
    raise RecipientsError(f".sops.yaml has no age line in a creation rule for envs/{env}/secrets/")


def update(instance: Path, env: str) -> list[str]:
    """Re-encrypt each sealed file whose recipients are not the rule's. Returns the files changed."""
    env_dir = instance / "envs" / env
    wanted = set(_age.recipients(instance / ".sops.yaml", env))
    if not wanted:
        raise RecipientsError(f".sops.yaml has no age creation rule for envs/{env}/secrets/")
    changed = []
    for path in sealed_files(env_dir):
        found = file_recipients(path)
        if found is None:
            raise RecipientsError(
                f"{path.relative_to(instance)}: not SOPS-encrypted with age; nothing was changed after it"
            )
        if set(found) == wanted:
            continue
        proc = subprocess.run(
            ["sops", "updatekeys", "--yes", str(path.relative_to(instance))],
            cwd=instance,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode:
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            raise RecipientsError(
                f"sops updatekeys {path.relative_to(instance)} failed ({reason[-1] if reason else 'no output'}); "
                "the working key must open it (SOPS_AGE_KEY_FILE or age_key_file)"
            )
        changed.append(path.relative_to(instance).as_posix())
    return changed


def add_recovery(instance: Path, env: str) -> str | None:
    """Give an env whose rule has one recipient the offline recovery key: print its private half
    once, add its public key to the rule, re-encrypt every file. Returns the public key, or None
    when the rule already has a second recipient."""
    sops_config = instance / ".sops.yaml"
    keys = _age.recipients(sops_config, env)
    if not keys:
        raise RecipientsError(f".sops.yaml has no age creation rule for envs/{env}/secrets/")
    if len(keys) > 1:
        return None
    secret, public = _age.new_key_in_memory()
    print_recovery_key(secret, public)
    set_rule(sops_config, env, [*keys, public])
    return public


def rotate(instance: Path, env: str, key_file: Path) -> tuple[str, str]:
    """A new working key. The rule's first recipient is the working key (init and add_recovery
    write it first); the new key replaces it, and the recovery key stays. key_file must hold one
    of the recipients, so the files still open while they are re-encrypted: the working key, or
    the recovery key when the working key is lost. The new identity is put first in key_file,
    beside what it held. Returns (old working public key, new public key)."""
    sops_config = instance / ".sops.yaml"
    keys = _age.recipients(sops_config, env)
    if not any(key in keys for key in _age.public_keys(key_file)):
        raise RecipientsError(f"{key_file} is not one of envs/{env}/secrets/'s recipients; it cannot rotate them")
    secret, new = _age.new_key_in_memory()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    text = key_file.read_text(encoding="utf-8")
    key_file.write_text(
        f"# created: {stamp} by jarvis secrets --rotate age\n# public key: {new}\n{secret}\n" + text, encoding="utf-8"
    )
    os.chmod(key_file, 0o600)
    set_rule(sops_config, env, [new, *keys[1:]])
    return keys[0], new


def print_recovery_key(secret: str, public: str) -> None:
    print()
    print("=" * 78)
    print("The RECOVERY KEY. It is shown this once and kept nowhere else. Store it offline now")
    print("(paper, or a password manager entry you do not use day to day). With it, the secrets")
    print("open even if the working key is lost.")
    print()
    print(f"  {secret}")
    print()
    print(f"  public key: {public}")
    print("=" * 78)
    print()

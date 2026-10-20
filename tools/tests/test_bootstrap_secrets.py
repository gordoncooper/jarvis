"""bootstrap-secrets idempotency and no-overwrite behaviour (D33)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "tools" / "bootstrap-secrets"


def _load_module():
    import importlib.machinery

    loader = importlib.machinery.SourceFileLoader("bootstrap_secrets", str(BOOTSTRAP))
    spec = importlib.util.spec_from_loader("bootstrap_secrets", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


def _write_test_sops_yaml(root: Path, public_key: str) -> None:
    (root / ".sops.yaml").write_text(
        textwrap.dedent(
            f"""
            creation_rules:
              - path_regex: envs/.*/secrets/.*\\.sops\\.yaml$
                encrypted_regex: ^(data|stringData)$
                age: {public_key}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _generate_throwaway_age_keypair() -> tuple[str, Path]:
    with tempfile.TemporaryDirectory() as tmp:
        keyfile = Path(tmp) / "key.txt"
        subprocess.check_call(["age-keygen", "-o", str(keyfile)])
        private = keyfile.read_text(encoding="utf-8")
        pub_line = next(line for line in private.splitlines() if line.startswith("# public key:"))
        public = pub_line.split(":", 1)[1].strip()
        persist = Path(tempfile.mkstemp(suffix="-age-key.txt")[1])
        persist.write_text(private, encoding="utf-8")
        return public, persist


def test_second_run_is_byte_identical_and_skips_existing(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair()
    _write_test_sops_yaml(tmp_path, public)

    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)

    sops_bin = subprocess.check_output(["which", "sops"], text=True).strip()

    mod.bootstrap("env-01", sops_bin=sops_bin)
    secrets_dir = tmp_path / "envs" / "env-01" / "secrets"
    files = sorted(secrets_dir.glob("*.sops.yaml"))
    assert len(files) == 7
    first_bytes = {path: path.read_bytes() for path in files}

    mod.bootstrap("env-01", sops_bin=sops_bin)
    for path in files:
        assert path.read_bytes() == first_bytes[path], path.name

    victim = files[0]
    victim.write_bytes(b"stale-content\n")
    mod.bootstrap("env-01", sops_bin=sops_bin)
    assert victim.read_bytes() == b"stale-content\n"


def test_secret_manifest_shape(tmp_path, monkeypatch):
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair()
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = subprocess.check_output(["which", "sops"], text=True).strip()
    mod.bootstrap("env-01", sops_bin=sops_bin)

    restic_path = tmp_path / "envs/env-01/secrets/backups-restic.sops.yaml"
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    decrypted = subprocess.check_output(["sops", "-d", str(restic_path)], env=env, text=True)
    restic = yaml.safe_load(decrypted)
    assert restic["metadata"]["namespace"] == "platform-backups"
    assert "restic_password" in restic["stringData"]

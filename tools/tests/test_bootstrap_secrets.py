"""bootstrap-secrets idempotency and no-overwrite behaviour (D33)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "tools" / "bootstrap-secrets"
TOOLS = REPO_ROOT / "tools"


def _load_module():
    import importlib.machinery

    loader = importlib.machinery.SourceFileLoader("bootstrap_secrets", str(BOOTSTRAP))
    spec = importlib.util.spec_from_loader("bootstrap_secrets", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    loader.exec_module(mod)
    return mod


def _load_sops():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    import _sops

    return _sops


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (required for this test)")
    return path


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


def _generate_throwaway_age_keypair(tmp_path: Path) -> tuple[str, Path]:
    age_keygen = _require_tool("age-keygen")
    keyfile = tmp_path / "age-key.txt"
    subprocess.check_call([age_keygen, "-o", str(keyfile)])
    private = keyfile.read_text(encoding="utf-8")
    pub_line = next(line for line in private.splitlines() if line.startswith("# public key:"))
    public = pub_line.split(":", 1)[1].strip()
    return public, keyfile


def test_second_run_is_byte_identical_and_skips_existing(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)

    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)

    sops_bin = _require_tool("sops")

    env_name = "env-test"
    (tmp_path / "envs" / env_name / "secrets").mkdir(parents=True)

    mod.bootstrap(env_name, sops_bin=sops_bin)
    secrets_dir = tmp_path / "envs" / env_name / "secrets"
    files = sorted(secrets_dir.glob("*.sops.yaml"))
    assert len(files) == 7
    first_bytes = {path: path.read_bytes() for path in files}

    mod.bootstrap(env_name, sops_bin=sops_bin)
    for path in files:
        assert path.read_bytes() == first_bytes[path], path.name

    victim = files[0]
    victim.write_bytes(b"stale-content\n")
    mod.bootstrap(env_name, sops_bin=sops_bin)
    assert victim.read_bytes() == b"stale-content\n"


def test_openssl_stderr_is_shown_only_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_module()
    key = tmp_path / "ca.key"
    out = mod._run_openssl(
        [
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ]
    )
    quiet = capsys.readouterr()
    assert quiet.out == ""
    assert quiet.err == ""
    assert out == b""
    assert key.is_file()

    with pytest.raises(subprocess.CalledProcessError):
        mod._run_openssl(["genpkey", "-algorithm", "not-a-real-algorithm"])
    failed = capsys.readouterr()
    assert failed.out == ""
    assert failed.err.strip()


def test_secret_manifest_shape(tmp_path, monkeypatch):
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    env_name = "env-test"
    (tmp_path / "envs" / env_name / "secrets").mkdir(parents=True)
    mod.bootstrap(env_name, sops_bin=sops_bin)

    restic_path = tmp_path / "envs" / env_name / "secrets" / "backups-restic.sops.yaml"
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    decrypted = subprocess.check_output([sops_bin, "-d", str(restic_path)], env=env, text=True)
    restic = yaml.safe_load(decrypted)
    assert restic["metadata"]["namespace"] == "platform-backups"
    assert "RESTIC_PASSWORD" in restic["stringData"]


def test_sops_failure_leaves_no_target_file(tmp_path):
    sops_mod = _load_sops()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)

    fail_sops = tmp_path / "fail-sops"
    fail_sops.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fail_sops.chmod(0o755)

    target = tmp_path / "envs/env-test/secrets/backups-restic.sops.yaml"
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "backups-restic", "namespace": "platform-backups"},
        "type": "Opaque",
        "stringData": {"RESTIC_PASSWORD": "test"},
    }

    with pytest.raises(subprocess.CalledProcessError):
        sops_mod.encrypt_secret_to_file(
            repo_root=tmp_path,
            destination=target,
            document=doc,
            sops_bin=str(fail_sops),
        )
    assert not target.exists()

"""bootstrap-secrets idempotency and no-overwrite behaviour (D33)."""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "tools" / "bootstrap-secrets"
TOOLS_LIB = REPO_ROOT / "tools" / "lib"


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
    if str(TOOLS_LIB) not in sys.path:
        sys.path.insert(0, str(TOOLS_LIB))
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
    root = tmp_path / "envs" / env_name / "ca.crt"
    assert len(files) == 7
    cert = root.read_text(encoding="utf-8")
    assert "BEGIN CERTIFICATE" in cert
    assert "PRIVATE KEY" not in cert
    first_bytes = {path: path.read_bytes() for path in [*files, root]}

    mod.bootstrap(env_name, sops_bin=sops_bin)
    for path, blob in first_bytes.items():
        assert path.read_bytes() == blob, path.name

    victim = files[0]
    victim.write_bytes(b"stale-content\n")
    root.write_bytes(b"stale-root\n")
    mod.bootstrap(env_name, sops_bin=sops_bin)
    assert victim.read_bytes() == b"stale-content\n"
    assert root.read_bytes() == b"stale-root\n"


def _wrap_sops(tmp_path: Path, sops_bin: str, *, reject_decrypt: bool) -> tuple[Path, Path]:
    log = tmp_path / "sops-args"
    wrapper = tmp_path / "sops-wrap"
    reject = ""
    if reject_decrypt:
        reject = 'if [ "$1" = decrypt ]; then echo "decrypt not allowed" >&2; exit 1; fi\n'
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f"{reject}"
        f"exec {shlex.quote(sops_bin)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, log


def test_new_ca_writes_the_root_without_decrypting(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    sops_bin = _require_tool("sops")
    wrapper, log = _wrap_sops(tmp_path, sops_bin, reject_decrypt=True)
    env_name = "env-test"
    (tmp_path / "envs" / env_name / "secrets").mkdir(parents=True)

    mod.bootstrap(env_name, sops_bin=str(wrapper))
    root = tmp_path / "envs" / env_name / "ca.crt"
    cert = root.read_text(encoding="utf-8")
    assert "BEGIN CERTIFICATE" in cert
    assert "PRIVATE KEY" not in cert
    recorded = log.read_text(encoding="utf-8")
    assert "encrypt" in recorded
    assert "decrypt" not in recorded


def test_missing_root_is_recovered_from_tls_crt_only(tmp_path, monkeypatch):
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    sops_bin = _require_tool("sops")
    env_name = "env-test"
    (tmp_path / "envs" / env_name / "secrets").mkdir(parents=True)
    mod.bootstrap(env_name, sops_bin=sops_bin)

    secret = tmp_path / "envs" / env_name / "secrets" / "cert-manager-ca.sops.yaml"
    root = tmp_path / "envs" / env_name / "ca.crt"
    secret_bytes = secret.read_bytes()
    root.unlink()
    wrapper, log = _wrap_sops(tmp_path, sops_bin, reject_decrypt=False)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(keyfile))
    mod.bootstrap(env_name, sops_bin=str(wrapper))

    assert secret.read_bytes() == secret_bytes
    recorded = log.read_text(encoding="utf-8")
    assert "decrypt" in recorded
    assert "--extract" in recorded
    assert "tls.crt" in recorded
    assert "tls.key" not in recorded
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    extracted = subprocess.check_output(
        [sops_bin, "decrypt", "--extract", '["stringData"]["tls.crt"]', str(secret)],
        env=env,
        text=True,
    )
    assert root.read_text(encoding="utf-8") == extracted
    assert "BEGIN CERTIFICATE" in extracted
    assert "PRIVATE KEY" not in extracted


def test_public_root_refuses_a_private_key(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    dest = tmp_path / "envs" / "env-test" / "ca.crt"
    with pytest.raises(SystemExit, match="not a certificate"):
        mod._write_public_root(
            dest,
            "-----BEGIN PRIVATE KEY-----\nYQ==\n-----END PRIVATE KEY-----\n",
        )
    assert not dest.exists()


def test_ca_leaf_passes_x509_strict_verify(tmp_path: Path) -> None:
    mod = _load_module()
    cert_pem, key_pem = mod._generate_ca_material()
    assert "PRIVATE KEY" not in cert_pem
    ca_cert = tmp_path / "ca.crt"
    ca_key = tmp_path / "ca.key"
    ca_cert.write_text(cert_pem, encoding="utf-8")
    ca_key.write_text(key_pem, encoding="utf-8")
    ca_key.chmod(0o600)

    leaf_csr = tmp_path / "leaf.csr"
    leaf_crt = tmp_path / "leaf.crt"
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(tmp_path / "leaf.key"),
            "-out",
            str(leaf_csr),
            "-subj",
            "/CN=leaf.example",
            "-batch",
        ],
        stdout=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(leaf_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(leaf_crt),
            "-days",
            "1",
            "-sha256",
        ],
        stdout=subprocess.DEVNULL,
    )
    verified = subprocess.run(
        ["openssl", "verify", "-x509_strict", "-CAfile", str(ca_cert), str(leaf_crt)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip().endswith(": OK")


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
    data = restic["stringData"]
    assert set(data) == {
        "RESTIC_PASSWORD",
        "RESTIC_REPOSITORY",
        "RESTIC_REST_USERNAME",
        "RESTIC_REST_PASSWORD",
        "htpasswd",
    }
    assert data["RESTIC_REPOSITORY"] == mod.RESTIC_REPOSITORY
    assert data["RESTIC_REST_USERNAME"] == mod.RESTIC_REST_USERNAME
    passwords_differ = data["RESTIC_PASSWORD"] != data["RESTIC_REST_PASSWORD"]
    assert passwords_differ
    line = data["htpasswd"]
    one_line = line.endswith("\n") and line.count("\n") == 1
    assert one_line
    accepted = mod._htpasswd_accepts(line, data["RESTIC_REST_USERNAME"], data["RESTIC_REST_PASSWORD"])
    assert accepted


def test_sops_failure_leaves_no_target_file(tmp_path):
    sops_mod = _load_sops()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)

    fail_sops = tmp_path / "fail-sops"
    fail_sops.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fail_sops.chmod(0o755)

    (tmp_path / "envs" / "env-test").mkdir(parents=True)
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
            env="env-test",
            name="backups-restic",
            document=doc,
            sops_bin=str(fail_sops),
        )
    assert not target.exists()


def test_new_ca_and_restic_secrets_are_prune_guarded(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test" / "secrets").mkdir(parents=True)
    mod.bootstrap("env-test", sops_bin=_require_tool("sops"))
    secrets = tmp_path / "envs" / "env-test" / "secrets"
    for name in ("cert-manager-ca", "backups-restic"):
        doc = yaml.safe_load((secrets / f"{name}.sops.yaml").read_text(encoding="utf-8"))
        annotations = doc["metadata"]["annotations"]
        assert annotations["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        for value in doc["stringData"].values():
            assert isinstance(value, str) and value.startswith("ENC[")

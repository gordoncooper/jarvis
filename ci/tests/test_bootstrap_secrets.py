"""bootstrap-secrets idempotency and no-overwrite behaviour."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT, load_tool

BOOTSTRAP = REPO_ROOT / "tools" / "bootstrap-secrets"
TOOLS_LIB = REPO_ROOT / "tools" / "lib"


def _load_module():
    return load_tool("bootstrap_secrets", BOOTSTRAP)


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


def _prepare_env(
    root: Path,
    env_name: str = "env-test",
    *,
    dns_suffix: str = "app.example",
    age_key_file: str | None = None,
) -> None:
    (root / "envs" / env_name / "secrets").mkdir(parents=True, exist_ok=True)
    values = root / "envs" / env_name / "values"
    values.mkdir(parents=True, exist_ok=True)
    lines = [f"dns_suffix: {dns_suffix}\n"]
    if age_key_file is not None:
        lines.append(f"age_key_file: {age_key_file}\n")
    (values / "cluster-vars.yaml").write_text("".join(lines), encoding="utf-8")


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
    _prepare_env(tmp_path, env_name)

    mod.bootstrap(env_name, sops_bin=sops_bin)
    secrets_dir = tmp_path / "envs" / env_name / "secrets"
    files = sorted(secrets_dir.glob("*.sops.yaml"))
    root = tmp_path / "envs" / env_name / "ca.crt"
    assert [f.name for f in files] == [
        "backups-restic.sops.yaml",
        "cert-manager-ca.sops.yaml",
        "k3s-token.sops.yaml",
    ]
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
    _prepare_env(tmp_path, env_name)

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
    _prepare_env(tmp_path, env_name)
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
    cert_pem, key_pem = mod._generate_ca_material("leaf.example")
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
            "-addext",
            "subjectAltName=DNS:leaf.example",
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
            "-copy_extensions",
            "copy",
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
    _prepare_env(tmp_path, env_name)
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


def test_bootstrap_refuses_a_bad_env_before_sops(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    marker = tmp_path / "sops-ran"
    wrapper = tmp_path / "sops-wrap"
    wrapper.write_text(
        "#!/bin/sh\n" + f"touch {shlex.quote(str(marker))}\n" + "exit 99\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    for env in ("../x", "env-missing"):
        marker.unlink(missing_ok=True)
        with pytest.raises(SystemExit):
            mod.bootstrap(env, sops_bin=str(wrapper))
        assert not marker.exists()


def test_new_ca_and_restic_secrets_are_prune_guarded(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=_require_tool("sops"))
    secrets = tmp_path / "envs" / "env-test" / "secrets"
    for name in ("cert-manager-ca", "backups-restic"):
        doc = yaml.safe_load((secrets / f"{name}.sops.yaml").read_text(encoding="utf-8"))
        annotations = doc["metadata"]["annotations"]
        assert annotations["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
        for value in doc["stringData"].values():
            assert isinstance(value, str) and value.startswith("ENC[")


def _git(root: Path, *args: str) -> None:
    subprocess.check_call(["git", "-C", str(root), *args], stdout=subprocess.DEVNULL)


def _scalars(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _scalars(child)]
    if isinstance(value, list):
        return [item for child in value for item in _scalars(child)]
    return []


def _sign_leaf(
    tmp_path: Path, ca_cert: Path, ca_key: Path, dns_name: str, stem: str, *, san: str | None = None
) -> Path:
    csr = tmp_path / f"{stem}.csr"
    crt = tmp_path / f"{stem}.crt"
    subprocess.check_call(
        [
            "openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(tmp_path / f"{stem}.key"),
            "-out", str(csr),
            "-subj", f"/CN={dns_name}",
            "-addext", f"subjectAltName={san or f'DNS:{dns_name}'}",
            "-batch",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [
            "openssl", "x509", "-req", "-in", str(csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(crt), "-days", "1", "-sha256", "-copy_extensions", "copy",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return crt


def test_tracked_restic_is_not_recreated_without_rotate(tmp_path, monkeypatch):
    """Deleting a git-recorded backups-restic file refuses unless --rotate-restic is set."""
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=sops_bin)
    rel = "envs/env-test/secrets/backups-restic.sops.yaml"
    recorded = (tmp_path / rel).read_bytes()
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "--", rel)
    _git(
        tmp_path,
        "-c", "user.name=test",
        "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", "record restic",
    )
    (tmp_path / rel).unlink()
    with pytest.raises(SystemExit, match="refusing to recreate backups-restic"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not (tmp_path / rel).exists()
    mod.bootstrap("env-test", sops_bin=sops_bin, rotate_restic=True)
    assert (tmp_path / rel).read_bytes() != recorded


def test_app_credential_is_its_own_and_never_minted_twice(tmp_path, monkeypatch):
    """--app seals the app's own password and an apps-server login named for its namespace in
    its own folder; a recorded credential, or an edited registration, refuses before any write."""
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    rel = "envs/env-test/secrets/app-shop/backups-restic-app-shop.sops.yaml"
    component = tmp_path / "envs/env-test/flux/platform-configs/backups/app-shop/kustomization.yaml"
    assert mod.bootstrap_app("env-test", "app-shop", sops_bin=sops_bin) == [rel, str(component.relative_to(tmp_path))]
    child = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    doc = yaml.safe_load(subprocess.check_output([sops_bin, "-d", str(tmp_path / rel)], env=child, text=True))
    assert doc["metadata"]["name"] == "backups-restic-app-shop"
    assert doc["metadata"]["namespace"] == "platform-backups"
    data = doc["stringData"]
    assert data["RESTIC_REST_USERNAME"] == "ns-app-shop"
    assert data["RESTIC_REPOSITORY"] == "rest:http://restic-apps.platform-backups.svc.cluster.local:8000/"
    assert mod._htpasswd_accepts(data["htpasswd"], "ns-app-shop", data["RESTIC_REST_PASSWORD"])
    assert len(data["RESTIC_PASSWORD"]) >= 32
    wanted = component.read_text(encoding="utf-8")
    component.write_text(wanted + "# edited\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        mod.bootstrap_app("env-test", "app-shop", sops_bin=sops_bin)
    component.write_text(wanted, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "--", rel)
    _git(tmp_path, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "app")
    (tmp_path / rel).unlink()
    with pytest.raises(SystemExit, match="refusing to recreate .*app-shop.* because git already records it"):
        mod.bootstrap_app("env-test", "app-shop", sops_bin=sops_bin)
    assert not (tmp_path / rel).exists()
    # An app run never also mints core's restic password.
    with pytest.raises(SystemExit) as caught:
        mod.main(["--env", "env-test", "--app", "app-blog", "--rotate-restic"])
    assert caught.value.code == 2
    assert not (tmp_path / "envs/env-test/secrets/app-blog").exists()


def _record_restic(tmp_path, monkeypatch):
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=sops_bin)
    rel = "envs/env-test/secrets/backups-restic.sops.yaml"
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "--", rel)
    _git(
        tmp_path,
        "-c", "user.name=test",
        "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", "record restic",
    )
    return mod, sops_bin, rel


def test_staged_restic_deletion_is_still_recorded(tmp_path, monkeypatch):
    """A staged but uncommitted deletion is still recorded, so bootstrap refuses."""
    mod, sops_bin, rel = _record_restic(tmp_path, monkeypatch)
    _git(tmp_path, "rm", "-q", "--", rel)
    with pytest.raises(SystemExit, match="refusing to recreate backups-restic"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not (tmp_path / rel).exists()


def test_git_error_does_not_mint_a_new_restic_password(tmp_path, monkeypatch):
    """A repository git cannot read refuses, and does not write a new restic file."""
    mod, sops_bin, rel = _record_restic(tmp_path, monkeypatch)
    (tmp_path / rel).unlink()
    git_dir = tmp_path / ".git"
    shutil.rmtree(git_dir)
    git_dir.write_text("gitdir: /no/such/gitdir\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="git could not"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not (tmp_path / rel).exists()


def test_repo_with_no_commits_mints_restic(tmp_path, monkeypatch):
    """A fresh `git init` with nothing committed records nothing, so bootstrap mints instead of refusing."""
    mod, sops_bin, rel = _record_restic(tmp_path, monkeypatch)
    (tmp_path / rel).unlink()
    shutil.rmtree(tmp_path / ".git")
    _git(tmp_path, "init", "-q")
    mod.bootstrap("env-test", sops_bin=sops_bin)
    assert (tmp_path / rel).is_file()


def test_git_location_env_does_not_hide_a_recorded_restic(tmp_path, monkeypatch):
    """A git-location variable pointing at another repository does not let bootstrap mint."""
    mod, sops_bin, rel = _record_restic(tmp_path, monkeypatch)
    (tmp_path / rel).unlink()
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    with pytest.raises(SystemExit, match="refusing to recreate backups-restic"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not (tmp_path / rel).exists()


def test_k3s_tokens_are_minted_only_for_a_new_env(tmp_path, monkeypatch):
    """A new env gets distinct server and agent tokens. An env with secrets is never minted over."""
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=sops_bin)
    token = tmp_path / "envs" / "env-test" / "secrets" / "k3s-token.sops.yaml"
    child = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    data = yaml.safe_load(subprocess.check_output([sops_bin, "-d", str(token)], env=child, text=True))
    server, agent = data["stringData"]["server_token"], data["stringData"]["agent_token"]
    assert len(server) >= 32 and len(agent) >= 32 and server != agent
    sealed = token.read_bytes()
    mod.bootstrap("env-test", sops_bin=sops_bin)
    assert token.read_bytes() == sealed
    token.unlink()
    with pytest.raises(SystemExit, match="refusing to mint k3s tokens"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not token.exists()


def test_make_rotate_restic_maps_only_one() -> None:
    """ROTATE_RESTIC=1 is the only value that adds --rotate-restic."""
    def dry(*args: str) -> str:
        result = subprocess.run(
            ["make", "-n", "-C", str(REPO_ROOT), "bootstrap-secrets", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert "--rotate-restic" in dry("ENV=env-test", "ROTATE_RESTIC=1")
    assert "--rotate-restic" not in dry("ENV=env-test")
    assert "--rotate-restic" not in dry("ENV=env-test", "ROTATE_RESTIC=0")
    assert '--app "app-shop"' in dry("ENV=env-test", "APP=app-shop")
    assert "--app" not in dry("ENV=env-test")


def test_ca_regeneration_refuses_a_mismatched_root(tmp_path, monkeypatch):
    """A CA-only regeneration refuses while ca.crt is still present."""
    mod = _load_module()
    public, _keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=sops_bin)
    secret = tmp_path / "envs" / "env-test" / "secrets" / "cert-manager-ca.sops.yaml"
    root = tmp_path / "envs" / "env-test" / "ca.crt"
    root_bytes = root.read_bytes()
    secret.unlink()
    with pytest.raises(SystemExit, match="refusing to mint a CA"):
        mod.bootstrap("env-test", sops_bin=sops_bin)
    assert not secret.exists()
    assert root.read_bytes() == root_bytes
    root.unlink()
    mod.bootstrap("env-test", sops_bin=sops_bin)
    assert secret.is_file()
    assert b"BEGIN CERTIFICATE" in root.read_bytes()
    assert b"PRIVATE KEY" not in root.read_bytes()


def test_new_ca_permits_only_the_dns_suffix(tmp_path: Path) -> None:
    """A new CA allows a name under the suffix, including the wildcard, and rejects a name outside it."""
    mod = _load_module()
    suffix = "app.example"
    cert_pem, key_pem = mod._generate_ca_material(suffix)
    ca_cert = tmp_path / "ca.crt"
    ca_key = tmp_path / "ca.key"
    ca_cert.write_text(cert_pem, encoding="utf-8")
    ca_key.write_text(key_pem, encoding="utf-8")
    ca_key.chmod(0o600)
    text = subprocess.check_output(["openssl", "x509", "-in", str(ca_cert), "-noout", "-text"], text=True)
    assert "Name Constraints: critical" in text
    assert f"DNS:{suffix}" in text
    assert "IP:0.0.0.0/0.0.0.0" in text
    assert "email:none@nameconstraint.invalid" in text
    for dns_name, stem, ok, san in (
        (suffix, "apex", True, None),
        (f"app.{suffix}", "child", True, None),
        (f"*.{suffix}", "wild", True, None),
        ("outside.example", "outside", False, None),
        ("192.0.2.10", "ip", False, "IP:192.0.2.10"),
        ("ip6", "ip6", False, "IP:2001:db8::1"),
        ("user@app.example", "mail", False, "email:user@app.example"),
        ("uri-in", "uriin", True, "URI:https://app.example/x"),
        ("uri-out", "uriout", False, "URI:https://outside.example/x"),
    ):
        leaf = _sign_leaf(tmp_path, ca_cert, ca_key, dns_name, stem, san=san)
        verified = subprocess.run(
            ["openssl", "verify", "-x509_strict", "-CAfile", str(ca_cert), str(leaf)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert (verified.returncode == 0) is ok, verified.stderr


def test_single_label_dns_suffix_is_not_a_ca_constraint(tmp_path: Path) -> None:
    """A single label would permit every name under it, so the CA is not minted."""
    mod = _load_module()
    with pytest.raises(SystemExit, match="DNS suffix"):
        mod._generate_ca_material("lan")
    assert list(tmp_path.iterdir()) == []


def test_age_key_file_is_used_when_sops_age_key_file_is_unset(tmp_path, monkeypatch):
    """A missing ca.crt is restored with age_key_file when SOPS_AGE_KEY_FILE is unset."""
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path, age_key_file=str(keyfile))
    mod.bootstrap("env-test", sops_bin=sops_bin)
    root = tmp_path / "envs" / "env-test" / "ca.crt"
    secret = tmp_path / "envs" / "env-test" / "secrets" / "cert-manager-ca.sops.yaml"
    expected = root.read_text(encoding="utf-8")
    root.unlink()
    mod.bootstrap("env-test", sops_bin=sops_bin)
    assert root.read_text(encoding="utf-8") == expected
    assert secret.is_file()


def test_generated_secret_values_stay_ciphertext(tmp_path, monkeypatch):
    """Every generated secret's data values are ciphertext, including in metadata."""
    mod = _load_module()
    public, keyfile = _generate_throwaway_age_keypair(tmp_path)
    _write_test_sops_yaml(tmp_path, public)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    sops_bin = _require_tool("sops")
    _prepare_env(tmp_path)
    mod.bootstrap("env-test", sops_bin=sops_bin)
    secrets = tmp_path / "envs" / "env-test" / "secrets"
    filenames = {spec.filename for spec in mod._specs("env-test")}
    assert {path.name for path in secrets.glob("*.sops.yaml")} == filenames
    child = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    for name in filenames:
        path = secrets / name
        raw = path.read_text(encoding="utf-8")
        encrypted = yaml.safe_load(raw)
        for section in ("data", "stringData"):
            block = encrypted.get(section) or {}
            assert isinstance(block, dict)
            for value in block.values():
                assert isinstance(value, str) and value.startswith("ENC[")
        decrypted = yaml.safe_load(
            subprocess.check_output([sops_bin, "-d", str(path)], env=child, text=True)
        )
        meta = decrypted.get("metadata") or {}
        identity = [meta.get("name"), meta.get("namespace")]
        identity = [item for item in identity if isinstance(item, str)]
        for section in ("data", "stringData"):
            for value in _scalars(decrypted.get(section) or {}):
                if any(value in item for item in identity):
                    continue
                assert value not in raw

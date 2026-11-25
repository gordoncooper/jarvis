"""jarvis secrets' mint (bootstrap): create-if-missing and never overwrite, the CA root and its name constraints,
each Secret's shape, and the refusals that keep a recorded secret from being minted again."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import bcrypt
import pytest
import yaml
from conftest import require_cli
from cryptography import x509

import support
from jarvis.secrets import mint

ENV = "env-test"
SUFFIX = "app.example"
SECRETS = "envs/env-test/secrets"
CA_ROOT = "envs/env-test/ca.crt"
CA = f"{SECRETS}/cert-manager-ca.sops.yaml"
K3S = f"{SECRETS}/k3s-token.sops.yaml"
RESTIC = f"{SECRETS}/backups-restic.sops.yaml"
APP = f"{SECRETS}/app-shop/backups-restic-app-shop.sops.yaml"
# What a new env's run writes, in order: the k3s tokens first, so a run that fails later leaves them in place.
CREATED = [K3S, CA, CA_ROOT, RESTIC]
PRUNE = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}
RESTIC_REFUSAL = (
    "refusing to recreate backups-restic because git already records it; rotation is in docs/backups.md; "
    "pass --new-restic-password to mint a new one, which opens no existing repository"
)
# The file's one private-key PEM: gitleaks' private-key rule reads from its BEGIN line to any later "KEY-----".
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nYQ==\n-----END PRIVATE KEY-----\n"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The instance at tmp_path: env-test sealed to a throwaway key, its cluster-vars naming the DNS suffix and its
    secrets folder empty, as jarvis init leaves it, and SOPS_AGE_KEY_FILE unset, so only an encrypt needs no key."""
    require_cli("sops")
    support.sealing_root(tmp_path, monkeypatch, ENV)
    (support.write_env_marker(tmp_path, ENV, dns_suffix=SUFFIX) / "secrets").mkdir()
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    return tmp_path.resolve()


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under root/envs, by its path from root."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "envs").rglob("*") if path.is_file()}


def _opened(root: Path, rel: str) -> dict:
    """The sealed file at rel, decrypted with the instance's throwaway key."""
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(root / ".age" / "keys.txt")}
    done = subprocess.run(["sops", "decrypt", rel], cwd=root, env=env, capture_output=True, text=True, check=True)
    return yaml.safe_load(done.stdout)


def _refused(run, *args, **kwargs) -> str:
    """The message run(*args, **kwargs) exits with."""
    with pytest.raises(SystemExit) as refused:
        run(*args, **kwargs)
    return str(refused.value)


def _logging_sops(tmp_path: Path) -> tuple[str, Path]:
    """(sops, log): a sops that writes each run's arguments to log, one line per run, then runs the real one."""
    wrapper, log = tmp_path / "sops-logged", tmp_path / "sops-args"
    real = shutil.which("sops") or "sops"
    wrapper.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\nexec {shlex.quote(real)} \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper), log


def _record_restic(root: Path) -> None:
    """Mint the env, then commit its backups-restic file in a new repository at root."""
    mint.bootstrap(ENV)
    support.git(root, "init", "-q")
    support.git(root, "add", "--", RESTIC)
    support.git(root, "commit", "-q", "-m", "record restic")


def _assert_restic_login(data: dict[str, str], repository: str, user: str) -> None:
    """data is a restic credential on repository: its own password, and a REST login for user whose htpasswd line
    is one bcrypt hash of that login's password."""
    hashed = data["htpasswd"].removeprefix(f"{user}:").removesuffix("\n")
    password, rest_password = data["RESTIC_PASSWORD"], data["RESTIC_REST_PASSWORD"]
    assert data == {"RESTIC_PASSWORD": password, "RESTIC_REPOSITORY": repository, "RESTIC_REST_USERNAME": user,
                    "RESTIC_REST_PASSWORD": rest_password, "htpasswd": f"{user}:{hashed}\n"}  # fmt: skip
    assert "\n" not in hashed and bcrypt.checkpw(rest_password.encode(), hashed.encode())
    assert min(len(password), len(rest_password)) >= 32 and password != rest_password


def test_second_run_is_byte_identical_and_skips_existing(root: Path) -> None:
    """Rule: bootstrap creates only what is missing and never overwrites: a second run changes no byte, and a file
    changed since is left as it is."""
    assert mint.bootstrap(ENV) == CREATED
    first = _tree(root)
    assert sorted(first) == sorted([*CREATED, "envs/env-test/values/cluster-vars.yaml"])
    assert mint.bootstrap(ENV) == []
    assert _tree(root) == first
    for rel in (RESTIC, CA_ROOT):
        (root / rel).write_bytes(b"stale\n")
    stale = _tree(root)
    assert mint.bootstrap(ENV) == []
    assert _tree(root) == stale


def test_new_ca_writes_the_root_without_decrypting(root: Path, tmp_path: Path) -> None:
    """Rule: a new CA's ca.crt is written from memory: sops only encrypts, and ca.crt is the sealed tls.crt, with
    no key in it."""
    sops, log = _logging_sops(tmp_path)
    assert mint.bootstrap(ENV, sops_bin=sops) == CREATED
    assert log.read_text().splitlines() == [f"encrypt --filename-override={rel} /dev/stdin" for rel in (K3S, CA, RESTIC)]
    cert = (root / CA_ROOT).read_text()
    assert cert == _opened(root, CA)["stringData"]["tls.crt"]
    assert cert.startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE KEY" not in cert


def test_missing_root_is_recovered_from_tls_crt_only(root: Path, tmp_path: Path, monkeypatch) -> None:
    """Rule: a missing ca.crt is restored from the sealed CA's tls.crt alone; the key stays sealed and no other
    file changes."""
    mint.bootstrap(ENV)
    before = _tree(root)
    (root / CA_ROOT).unlink()
    sops, log = _logging_sops(tmp_path)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(root / ".age" / "keys.txt"))
    assert mint.bootstrap(ENV, sops_bin=sops) == [CA_ROOT]
    assert log.read_text().splitlines() == [f'decrypt --extract ["stringData"]["tls.crt"] {root / CA}']
    assert _tree(root) == before


@pytest.mark.parametrize(
    "pem",
    [
        pytest.param(KEY_PEM, id="key"),
        pytest.param("-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n" + KEY_PEM,
                     id="certificate-and-key"),
        pytest.param("YQ==\n", id="no-certificate"),
    ],
)  # fmt: skip
def test_public_root_refuses_a_private_key(tmp_path: Path, pem: str) -> None:
    """Rule: ca.crt is written only from a certificate, and never with a private key in it."""
    dest = tmp_path / "ca.crt"
    assert _refused(mint._write_public_root, dest, pem) == "refusing to write a CA root that is not a certificate"
    assert not dest.exists()


def test_secret_manifest_shape(root: Path) -> None:
    """Rule: each minted Secret is named for what reads it, of the type it is read as, and guarded from Flux's
    prune; backups-restic holds its own password and a REST login."""
    mint.bootstrap(ENV)
    docs = {rel: yaml.safe_load((root / rel).read_text()) for rel in (K3S, CA, RESTIC)}
    assert {rel: (doc["kind"], doc["type"], doc["metadata"]) for rel, doc in docs.items()} == {
        K3S: ("Secret", "Opaque", {"name": "k3s-token", "annotations": PRUNE}),
        CA: ("Secret", "kubernetes.io/tls", {"name": "cert-manager-ca", "namespace": "platform-cert-manager", "annotations": PRUNE}),
        RESTIC: ("Secret", "Opaque", {"name": "backups-restic", "namespace": "platform-backups", "annotations": PRUNE}),
    }
    _assert_restic_login(_opened(root, RESTIC)["stringData"], mint.RESTIC_REPOSITORY, mint.RESTIC_REST_USERNAME)


def test_sops_failure_leaves_no_target_file(root: Path, tmp_path: Path) -> None:
    """Rule: when sops fails, bootstrap stops and writes no file."""
    bin_dir, marker = support.marker_sops(tmp_path)
    before = _tree(root)
    with pytest.raises(subprocess.CalledProcessError) as failed:
        mint.bootstrap(ENV, sops_bin=str(bin_dir / "sops"))
    assert failed.value.returncode == 99 and marker.exists()
    assert _tree(root) == before


@pytest.mark.parametrize(
    ("env", "message"),
    [
        pytest.param("../x", "refusing env, namespace or name: only [a-z0-9-] segments are allowed", id="not-a-name"),
        pytest.param("env-missing", "env folder does not exist", id="no-env-folder"),
    ],
)
def test_bootstrap_refuses_a_bad_env_before_sops(root: Path, tmp_path: Path, env: str, message: str) -> None:
    """Rule: a bad env name, or an env with no folder, is refused before sops runs (test_sops_failure_leaves_no_target_file
    shows the same sops running for a good env)."""
    bin_dir, marker = support.marker_sops(tmp_path)
    assert _refused(mint.bootstrap, env, sops_bin=str(bin_dir / "sops")) == message
    assert not marker.exists()


def test_tracked_restic_is_not_recreated_without_rotate(root: Path) -> None:
    """Rule: a deleted backups-restic file git records is not minted again, unless --new-restic-password is set."""
    _record_restic(root)
    recorded = (root / RESTIC).read_bytes()
    (root / RESTIC).unlink()
    before = _tree(root)
    assert _refused(mint.bootstrap, ENV) == RESTIC_REFUSAL
    assert _tree(root) == before
    assert mint.bootstrap(ENV, new_restic_password=True) == [RESTIC]
    assert (root / RESTIC).read_bytes() != recorded


def test_app_credential_is_its_own_and_never_minted_twice(root: Path) -> None:
    """Rule: an app's run seals its own password and an apps-server login named for its namespace, in its own
    folder, once; an app the env does not list, or a credential git records, is refused before any write."""
    before = _tree(root)
    assert _refused(mint.bootstrap_app, ENV, "app-shop") == "refusing app-shop: the env does not list shop in values/apps.yaml"
    assert _tree(root) == before
    (root / "envs/env-test/values/apps.yaml").write_text("shop:\n  version: v0.1.0\n")
    assert mint.bootstrap_app(ENV, "app-shop") == [APP]
    assert mint.bootstrap_app(ENV, "app-shop") == []
    doc = _opened(root, APP)
    assert doc["metadata"] == {"name": "backups-restic-app-shop", "namespace": "platform-backups", "annotations": PRUNE}
    _assert_restic_login(doc["stringData"], "rest:http://restic-apps.platform-backups.svc.cluster.local:8000/", "ns-app-shop")
    support.git(root, "init", "-q")
    support.git(root, "add", "--", APP)
    support.git(root, "commit", "-q", "-m", "app")
    (root / APP).unlink()
    before = _tree(root)
    assert _refused(mint.bootstrap_app, ENV, "app-shop") == (
        f"refusing to recreate {APP} because git already records it; an app's repositories open only with its "
        "first password (core/feature/app-hosting/CONTRACT.md)"
    )
    assert _tree(root) == before


@pytest.mark.parametrize("commit", [pytest.param(False, id="staged"), pytest.param(True, id="committed")])
def test_staged_restic_deletion_is_still_recorded(root: Path, commit: bool) -> None:
    """Rule: a deletion of backups-restic, staged or committed, leaves it recorded in git, so bootstrap refuses."""
    _record_restic(root)
    support.git(root, "rm", "-q", "--", RESTIC)
    if commit:
        support.git(root, "commit", "-q", "-m", "drop restic")
    assert _refused(mint.bootstrap, ENV) == RESTIC_REFUSAL
    assert not (root / RESTIC).exists()


def test_git_error_does_not_mint_a_new_restic_password(root: Path) -> None:
    """Rule: a repository git cannot read refuses, and mints no new restic password."""
    _record_restic(root)
    (root / RESTIC).unlink()
    shutil.rmtree(root / ".git")
    (root / ".git").write_text("gitdir: /no/such/gitdir\n")
    assert _refused(mint.bootstrap, ENV) == (
        f"refusing to mint secrets because git could not say whether {RESTIC} is already recorded: "
        "fatal: not a git repository: /no/such/gitdir"
    )
    assert not (root / RESTIC).exists()


def test_repo_with_no_commits_mints_restic(root: Path) -> None:
    """Rule: a fresh git init with nothing committed records nothing, so bootstrap mints instead of refusing."""
    _record_restic(root)
    (root / RESTIC).unlink()
    shutil.rmtree(root / ".git")
    support.git(root, "init", "-q")
    assert mint.bootstrap(ENV) == [RESTIC]


def test_git_location_env_does_not_hide_a_recorded_restic(root: Path, monkeypatch) -> None:
    """Rule: a git-location variable pointing at another repository does not let bootstrap mint."""
    _record_restic(root)
    (root / RESTIC).unlink()
    other = root / "other-repo"
    other.mkdir()
    support.git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert _refused(mint.bootstrap, ENV) == RESTIC_REFUSAL
    assert not (root / RESTIC).exists()


def test_k3s_tokens_are_minted_only_for_a_new_env(root: Path) -> None:
    """Rule: a new env gets distinct server and agent tokens; an env that has other secrets never gets new ones."""
    mint.bootstrap(ENV)
    tokens = _opened(root, K3S)["stringData"]
    assert set(tokens) == {"server_token", "agent_token"}
    assert min(map(len, tokens.values())) >= 32 and tokens["server_token"] != tokens["agent_token"]
    (root / K3S).unlink()
    before = _tree(root)
    assert _refused(mint.bootstrap, ENV) == (
        "refusing to mint k3s tokens for an env that already has secrets; its cluster has a token already, so the "
        "owner seals it as k3s-token (docs/backups.md, Rebuild a dead node)"
    )
    assert _tree(root) == before


def test_ca_regeneration_refuses_a_mismatched_root(root: Path) -> None:
    """Rule: the sealed CA is not minted again while ca.crt is present, as the new CA would not match it; with
    both gone, a new CA and its ca.crt are."""
    mint.bootstrap(ENV)
    old = (root / CA_ROOT).read_text()
    (root / CA).unlink()
    before = _tree(root)
    assert _refused(mint.bootstrap, ENV) == (
        f"refusing to mint a CA while {CA_ROOT} is present; delete it with the sealed CA only as part of the CA rotation"
    )
    assert _tree(root) == before
    (root / CA_ROOT).unlink()
    assert mint.bootstrap(ENV) == [CA, CA_ROOT]
    assert old != (root / CA_ROOT).read_text() == _opened(root, CA)["stringData"]["tls.crt"]


@pytest.fixture(scope="module")
def ca(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """(certificate, key) of one new CA for SUFFIX."""
    folder = tmp_path_factory.mktemp("ca")
    cert_pem, key_pem = mint._generate_ca_material(SUFFIX)
    (folder / "ca.crt").write_text(cert_pem)
    (folder / "ca.key").write_text(key_pem)
    return folder / "ca.crt", folder / "ca.key"


@pytest.mark.parametrize(
    ("cn", "san", "error"),
    [
        pytest.param(SUFFIX, f"DNS:{SUFFIX}", None, id="apex"),
        pytest.param(f"app.{SUFFIX}", f"DNS:app.{SUFFIX}", None, id="child"),
        pytest.param(f"*.{SUFFIX}", f"DNS:*.{SUFFIX}", None, id="wildcard"),
        pytest.param("outside.example", "DNS:outside.example", "47 at 0 depth lookup: permitted subtree violation", id="outside"),
        # Not a dotted CN: openssl checks a hostname-like CN against the DNS subtree on its own.
        pytest.param("ip4", "IP:192.0.2.10", "48 at 0 depth lookup: excluded subtree violation", id="ipv4"),
        pytest.param("ip6", "IP:2001:db8::1", "48 at 0 depth lookup: excluded subtree violation", id="ipv6"),
        pytest.param("user@app.example", "email:user@app.example", "47 at 0 depth lookup: permitted subtree violation", id="mail"),
        pytest.param("uri-in", "URI:https://app.example/x", None, id="uri-in"),
        pytest.param("uri-child", "URI:https://www.app.example/x", None, id="uri-child"),
        pytest.param("uri-out", "URI:https://outside.example/x", "47 at 0 depth lookup: permitted subtree violation", id="uri-out"),
    ],
)  # fmt: skip
def test_new_ca_permits_only_the_dns_suffix(ca: tuple[Path, Path], tmp_path: Path, cn: str, san: str, error: str | None) -> None:
    """Rule: a new CA's critical name constraints let it sign a name under the DNS suffix, the wildcard included, and
    nothing else; a leaf it signs passes openssl's -x509_strict verify (a7c1dfa: the CA's keyUsage)."""
    cert, key = ca
    assert x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(x509.NameConstraints).critical
    csr, leaf = tmp_path / "leaf.csr", tmp_path / "leaf.crt"
    subprocess.run(["openssl", "req", "-new", "-nodes", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-keyout", str(tmp_path / "leaf.key"), "-out", str(csr), "-subj", f"/CN={cn}",
                    "-addext", f"subjectAltName={san}", "-batch"], capture_output=True, check=True)  # fmt: skip
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(cert), "-CAkey", str(key), "-set_serial", "1",
                    "-out", str(leaf), "-days", "1", "-copy_extensions", "copy"], capture_output=True, check=True)  # fmt: skip
    verified = subprocess.run(["openssl", "verify", "-x509_strict", "-CAfile", str(cert), str(leaf)], capture_output=True, text=True)
    if error is None:
        assert (verified.returncode, verified.stdout) == (0, f"{leaf}: OK\n"), verified.stderr
    else:
        assert (verified.returncode, verified.stderr) == (2, f"CN = {cn}\nerror {error}\nerror {leaf}: verification failed\n")


def test_single_label_dns_suffix_is_not_a_ca_constraint() -> None:
    """Rule: a single label would permit every name under it, so no CA is minted for it."""
    assert _refused(mint._generate_ca_material, "lan") == "refusing to mint a CA without a DNS suffix"


def test_age_key_file_is_used_when_sops_age_key_file_is_unset(root: Path) -> None:
    """Rule: with SOPS_AGE_KEY_FILE unset, a missing ca.crt is restored with the env's age_key_file. Each value is
    read as YAML reads it: quoted, with a comment after it."""
    (root / "envs/env-test/values/cluster-vars.yaml").write_text(
        f'dns_suffix: "{SUFFIX}"  # the suffix\nage_key_file: "{root / ".age" / "keys.txt"}"  # the working key\n'
    )
    mint.bootstrap(ENV)
    before = _tree(root)
    (root / CA_ROOT).unlink()
    assert mint.bootstrap(ENV) == [CA_ROOT]
    assert _tree(root) == before


def test_generated_secret_values_stay_ciphertext(root: Path) -> None:
    """Rule: every value bootstrap seals is ciphertext, and no plaintext value shows anywhere in its file, metadata
    included, but as part of the Secret's own name or namespace."""
    sealed = [rel for rel in mint.bootstrap(ENV) if rel != CA_ROOT]
    assert sealed == [K3S, CA, RESTIC]
    for rel in sealed:
        raw = (root / rel).read_text()
        doc, plain = yaml.safe_load(raw), _opened(root, rel)
        values = [value for section in ("data", "stringData") for value in (doc.get(section) or {}).values()]
        assert values and all(str(value).startswith("ENC[") for value in values), rel
        identity = [plain["metadata"].get("name", ""), plain["metadata"].get("namespace", "")]
        shown = [value for section in ("data", "stringData") for value in (plain.get(section) or {}).values()
                 if value in raw and not any(value in item for item in identity)]  # fmt: skip
        assert shown == [], rel

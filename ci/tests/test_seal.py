"""seal and bootstrap-secrets refuse a path that is not envs/<env>/secrets (J11)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT, load_tool

SEAL = REPO_ROOT / "tools" / "seal"
_PRUNE = "kustomize.toolkit.fluxcd.io/prune"


def _load_seal():
    return load_tool("seal_tool", SEAL)


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (required for this test)")
    return path


def _sops_rules(root: Path, public_key: str) -> None:
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


def _age_keypair(tmp_path: Path) -> tuple[str, Path]:
    age_keygen = _require_tool("age-keygen")
    keyfile = tmp_path / "age-key.txt"
    subprocess.check_call([age_keygen, "-o", str(keyfile)], stdout=subprocess.DEVNULL)
    keyfile.chmod(0o600)
    private = keyfile.read_text(encoding="utf-8")
    public = next(line for line in private.splitlines() if line.startswith("# public key:"))
    return public.split(":", 1)[1].strip(), keyfile


def _marker_sops(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "sops-ran"
    wrapper = tmp_path / "sops-wrap"
    wrapper.write_text(
        "#!/bin/sh\n" + f"touch {shlex.quote(str(marker))}\n" + "exit 99\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def _secret(name: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  namespace: demo\n"
        "type: Opaque\n"
        "stringData:\n"
        "  token: PLAINTEXT-MARKER\n"
    )


def _bind_repo(monkeypatch: pytest.MonkeyPatch, mod, root: Path) -> None:
    monkeypatch.setattr(mod, "REPO_ROOT", root)
    monkeypatch.setattr(mod, "_repo_root", lambda: root)


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ("Not-valid", "sample-secret"),
        ("..", "sample-secret"),
        ("a/b", "sample-secret"),
        ("env-test", ".."),
        ("env-test", "a/b"),
        ("env-test", "Not-Valid"),
    ],
)
def test_seal_refuses_bad_segments_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, name: str
) -> None:
    mod = _load_seal()
    _bind_repo(monkeypatch, mod, tmp_path)
    for folder in ("Not-valid", "env-test"):
        (tmp_path / "envs" / folder).mkdir(parents=True)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal(env, name, _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()


def test_seal_refuses_a_missing_env_folder_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_seal()
    _bind_repo(monkeypatch, mod, tmp_path)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal("env-missing", "sample-secret", _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()
    assert not (tmp_path / "envs" / "env-missing").exists()


def test_seal_refuses_a_symlinked_secrets_dir_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_seal()
    _bind_repo(monkeypatch, mod, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    env_dir = tmp_path / "envs" / "env-test"
    env_dir.mkdir(parents=True)
    (env_dir / "secrets").symlink_to(outside)
    wrapper, marker = _marker_sops(tmp_path)
    with pytest.raises(SystemExit):
        mod.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=str(wrapper))
    assert not marker.exists()
    assert list(outside.iterdir()) == []


def test_seal_writes_a_valid_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_seal()
    public, _keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    _bind_repo(monkeypatch, mod, tmp_path)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    out = mod.seal("env-test", "sample-secret", _secret("sample-secret"), sops_bin=_require_tool("sops"))
    text = out.read_text(encoding="utf-8")
    assert out.parent == tmp_path / "envs" / "env-test" / "secrets"
    assert "PLAINTEXT-MARKER" not in text
    assert "ENC[" in text
    loaded = yaml.safe_load(text)
    assert loaded["metadata"]["name"] == "sample-secret"
    assert loaded["stringData"]["token"].startswith("ENC[")


def test_sops_set_adds_prune_annotation_without_changing_data(tmp_path: Path) -> None:
    """Owner command for the CA annotation. Throwaway key and file only."""
    public, keyfile = _age_keypair(tmp_path)
    _sops_rules(tmp_path, public)
    secret = tmp_path / "envs" / "env-test" / "secrets" / "cert-manager-ca.sops.yaml"
    secret.parent.mkdir(parents=True)
    plain = textwrap.dedent(
        """
        apiVersion: v1
        kind: Secret
        metadata:
            name: cert-manager-ca
            namespace: platform-cert-manager
        type: kubernetes.io/tls
        stringData:
            tls.crt: CERT
            tls.key: KEY
        """
    ).lstrip()
    sops_bin = _require_tool("sops")
    ciphertext = subprocess.check_output(
        [sops_bin, "encrypt", "--filename-override=envs/env-test/secrets/cert-manager-ca.sops.yaml", "/dev/stdin"],
        cwd=tmp_path,
        input=plain,
        text=True,
    )
    secret.write_text(ciphertext, encoding="utf-8")
    before = yaml.safe_load(ciphertext)
    stamp = before["sops"]["lastmodified"]
    deadline = time.time() + 2
    while time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) == stamp and time.time() < deadline:
        time.sleep(0.05)
    env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
    subprocess.check_call(
        [
            sops_bin,
            "set",
            "envs/env-test/secrets/cert-manager-ca.sops.yaml",
            '["metadata"]["annotations"]',
            '{"kustomize.toolkit.fluxcd.io/prune":"disabled"}',
        ],
        cwd=tmp_path,
        env=env,
    )
    after = yaml.safe_load(secret.read_text(encoding="utf-8"))
    decrypted = yaml.safe_load(
        subprocess.check_output([sops_bin, "-d", str(secret)], env=env, text=True)
    )
    assert decrypted["stringData"] == {"tls.crt": "CERT", "tls.key": "KEY"}
    assert decrypted["metadata"]["annotations"][_PRUNE] == "disabled"
    assert before["stringData"] == after["stringData"]
    assert before["sops"]["mac"] != after["sops"]["mac"]
    assert before["sops"]["lastmodified"] != after["sops"]["lastmodified"]
    assert before["sops"]["version"] == after["sops"]["version"]
    assert before["sops"]["age"] == after["sops"]["age"]

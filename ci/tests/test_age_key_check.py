"""age-key-check prints PASS or FAIL and never the decrypted secret."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from support import REPO_ROOT, load_tool

SCRIPT = REPO_ROOT / "tools" / "age-key-check"
_SENTINEL = "sentinel-plaintext-9f3c2a7b"


def _load():
    return load_tool("age_key_check", SCRIPT)


def _require(name: str) -> str:
    path = __import__("shutil").which(name)
    if not path:
        pytest.fail(f"{name} not found on PATH (required for this test)")
    return path


def _key(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    subprocess.check_call(
        [_require("age-keygen"), "-o", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def _public(path: Path) -> str:
    return subprocess.check_output([_require("age-keygen"), "-y", str(path)], text=True).strip()


def _secret(root: Path, public: str) -> None:
    (root / ".sops.yaml").write_text(
        textwrap.dedent(
            f"""
            creation_rules:
              - path_regex: envs/.*/secrets/.*\\.sops\\.yaml$
                encrypted_regex: ^(data|stringData)$
                age: {public}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    env = root / "envs" / "env-test" / "secrets"
    env.mkdir(parents=True)
    plain = f"stringData:\n  token: {_SENTINEL}\n"
    sops = _require("sops")
    key = root / "right.txt"
    encrypted = subprocess.check_output(
        [sops, "encrypt", "--filename-override", "envs/env-test/secrets/app.sops.yaml", "/dev/stdin"],
        input=plain,
        text=True,
        cwd=root,
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(key)},
    )
    (env / "app.sops.yaml").write_text(encrypted, encoding="utf-8")


def _holding(root: Path, needle: bytes) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and needle in path.read_bytes()]


def test_right_key_passes_and_wrong_key_fails_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The right throwaway key prints PASS, a wrong key prints FAIL, and neither shows plaintext."""
    mod = _load()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    right = _key(tmp_path, "right.txt")
    wrong = _key(tmp_path, "wrong.txt")
    _secret(tmp_path, _public(right))
    needle = _SENTINEL.encode()
    assert _holding(tmp_path, needle) == []

    assert mod.main(["--env", "env-test", "--key-file", str(right)]) == 0
    passed = capfd.readouterr()
    assert passed.out == "PASS\n"
    assert passed.err == ""
    assert _SENTINEL not in f"{passed.out}{passed.err}"

    assert mod.main(["--env", "env-test", "--key-file", str(wrong)]) == 1
    failed = capfd.readouterr()
    assert failed.out == "FAIL\n"
    # capfd reads the process fds. A sops stderr leak, including plaintext, fails here.
    assert failed.err == ""
    assert _SENTINEL not in f"{failed.out}{failed.err}"
    assert _holding(tmp_path, needle) == []


def test_default_key_location_does_not_rescue_a_wrong_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A valid throwaway key in the default per-user location does not make a wrong KEY pass."""
    mod = _load()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    right = _key(tmp_path, "right.txt")
    wrong = _key(tmp_path, "wrong.txt")
    _secret(tmp_path, _public(right))
    home = tmp_path / "home"
    key_dir = home / ".config" / "sops" / "age"
    key_dir.mkdir(parents=True)
    (key_dir / "keys.txt").write_text(right.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    assert mod.main(["--env", "env-test", "--key-file", str(wrong)]) == 1
    failed = capfd.readouterr()
    assert failed.out == "FAIL\n"
    assert failed.err == ""
    assert _SENTINEL not in f"{failed.out}{failed.err}"
    assert mod.main(["--env", "env-test", "--key-file", str(right)]) == 0
    passed = capfd.readouterr()
    assert passed.out == "PASS\n"
    assert passed.err == ""


def test_env_with_no_secrets_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """An env with no secrets prints FAIL."""
    mod = _load()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    key = _key(tmp_path, "right.txt")
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    assert mod.main(["--env", "env-test", "--key-file", str(key)]) == 1
    captured = capfd.readouterr()
    assert captured.out == "FAIL\n"
    assert captured.err == ""

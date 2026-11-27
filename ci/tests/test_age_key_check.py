"""age-key-check prints PASS or FAIL and never the decrypted secret."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import require_cli

import support
from jarvis.secrets import age, age_key_check, sops

_SENTINEL = "sentinel-plaintext-9f3c2a7b"


def _holding(root: Path, needle: bytes) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and needle in path.read_bytes()]


@pytest.mark.parametrize("rescue", [pytest.param(None, id="no-default-key"), pytest.param("home", id="default-key-in-home"),
                                    pytest.param("environment", id="key-in-environment")])  # fmt: skip
def test_right_key_passes_and_wrong_key_fails_without_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], rescue: str | None
) -> None:
    """Rule: the env's key prints PASS and another key FAIL, even with the env's key in the per-user default location
    (5c736a5) or in the caller's environment, and neither prints anything else or leaves the plaintext anywhere."""
    right = support.sealing_root(tmp_path, monkeypatch)
    wrong = tmp_path / "wrong.txt"
    age.create_key(wrong)
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    sops.encrypt_secret_to_file(repo_root=tmp_path, env="env-test", name="app", sops_bin=require_cli("sops"),
                                document={"stringData": {"token": _SENTINEL}})  # fmt: skip
    if rescue == "environment":
        monkeypatch.setenv("SOPS_AGE_KEY", right.read_text(encoding="utf-8"))
    if rescue == "home":
        home = tmp_path / "home"
        (home / ".config" / "sops" / "age").mkdir(parents=True)
        (home / ".config" / "sops" / "age" / "keys.txt").write_bytes(right.read_bytes())
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    needle = _SENTINEL.encode()
    assert _holding(tmp_path, needle) == []

    assert age_key_check.main(["--env", "env-test", "--key-file", str(right)]) == 0
    assert capfd.readouterr() == ("PASS\n", "")
    assert age_key_check.main(["--env", "env-test", "--key-file", str(wrong)]) == 1
    # capfd reads the process fds. A sops stderr leak, including plaintext, fails here.
    assert capfd.readouterr() == ("FAIL\n", "")
    assert _holding(tmp_path, needle) == []


@pytest.mark.parametrize(
    ("mode", "other_owner", "secret", "sops_ran"),
    [
        *(pytest.param(mode, False, True, False, id=f"mode-{mode:o}")
          for mode in (0o640, 0o620, 0o604, 0o602, 0o700, 0o670, 0o601)),
        pytest.param(0o600, True, True, False, id="other-owner"),
        pytest.param(0o600, False, False, False, id="no-secrets"),
        pytest.param(None, False, True, False, id="no-key-file"),
        pytest.param(0o600, False, True, True, id="sops-runs"),
    ],
)  # fmt: skip
def test_key_check_fails_before_sops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], mode: int | None, other_owner: bool,
    secret: bool, sops_ran: bool,
) -> None:  # fmt: skip
    """Rule: a key file missing, not mode 0600 or not the runner's, or an env with no secret, prints FAIL before sops
    runs."""
    monkeypatch.setenv("INSTANCE", str(tmp_path))
    bin_dir, marker = support.marker_sops(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "envs" / "env-test").mkdir(parents=True)
    if secret:
        (tmp_path / "envs" / "env-test" / "secrets").mkdir()
        (tmp_path / "envs" / "env-test" / "secrets" / "app.sops.yaml").write_text("fake-ciphertext\n", encoding="utf-8")
    key = tmp_path / "key.txt"
    if mode is not None:
        age.create_key(key)
        key.chmod(mode)
    owner = os.getuid() + other_owner
    monkeypatch.setattr(age_key_check.os, "getuid", lambda: owner)

    assert age_key_check.main(["--env", "env-test", "--key-file", str(key)]) == 1

    assert capfd.readouterr() == ("FAIL\n", "")
    assert marker.exists() is sops_ran


def test_an_error_in_the_check_prints_only_fail(monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """Rule: an error inside the check prints FAIL and no traceback, which could quote decrypted bytes."""

    def check(env: str, key_file: Path) -> bool:
        raise RuntimeError(_SENTINEL)

    monkeypatch.setattr(age_key_check, "check", check)
    assert age_key_check.main(["--env", "env-test", "--key-file", "key.txt"]) == 1

    assert capfd.readouterr() == ("FAIL\n", "")

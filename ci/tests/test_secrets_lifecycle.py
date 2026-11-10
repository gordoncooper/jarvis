"""An env's age recipients over its life: the recovery key, re-encrypting, and rotating keys."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import sample

from jarvis.env.validate import CHECKS
from jarvis.secrets import age, mint, recipients

ENV = "env-00"


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sample instance whose secrets open for one key only, as an env made before v0.5.0."""
    root = sample.build(tmp_path / "instance", with_apps=False, with_addons=False)
    monkeypatch.setenv("INSTANCE", str(root))
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(root / ".age" / "keys.txt"))
    return root


def _opens(path: Path, key_file: Path) -> bool:
    done = subprocess.run(
        ["sops", "decrypt", str(path)], env={**os.environ, "SOPS_AGE_KEY_FILE": str(key_file)}, capture_output=True
    )
    return done.returncode == 0


def _schema_ok(root: Path) -> bool:
    return CHECKS["env-schema"](root) == []


def _one_identity(tmp_path: Path, secret: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_the_recovery_key_is_added_once_and_opens_every_file(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: --add-recovery-key adds a second recipient, shows its private half once and writes it
    nowhere, and re-encrypts every file so either key opens it. A second run changes nothing."""
    assert len(age.recipients(instance / ".sops.yaml", ENV)) == 1
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    out = capsys.readouterr().out
    secret = next(line.strip() for line in out.splitlines() if line.strip().startswith("AGE-SECRET-KEY-"))
    assert not [path for path in instance.rglob("*") if path.is_file() and secret in path.read_text(errors="ignore")]
    assert len(age.recipients(instance / ".sops.yaml", ENV)) == 2
    recovery = _one_identity(tmp_path, secret, "recovery.txt")
    files = recipients.sealed_files(instance / "envs" / ENV)
    assert files and all(_opens(path, recovery) for path in files)
    assert _schema_ok(instance)
    before = {path: path.read_bytes() for path in files}
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    assert "already has a second recipient" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in files} == before


def test_update_recipients_follows_the_rule(instance: Path) -> None:
    """Rule: after the rule changes, the schema names every file left behind, and
    --update-recipients re-encrypts exactly those."""
    _secret, extra = age.new_key_in_memory()
    keys = age.recipients(instance / ".sops.yaml", ENV)
    recipients.set_rule(instance / ".sops.yaml", ENV, [*keys, extra])
    problems = recipients.problems(instance, instance / "envs" / ENV)
    assert problems and all("--update-recipients" in line for line in problems)
    assert mint.main(["--env", ENV, "--update-recipients"]) == 0
    assert recipients.problems(instance, instance / "envs" / ENV) == []
    assert recipients.update(instance, ENV) == []


def test_rotating_the_working_key_retires_the_old_one_from_every_file(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: --rotate age puts a new identity first in the key file, swaps it for the old public
    key in the rule (the recovery key stays), and re-encrypts: the old key alone opens nothing."""
    key_file = instance / ".age" / "keys.txt"
    old_secret = next(line for line in key_file.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-"))
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    recovery_public = age.recipients(instance / ".sops.yaml", ENV)[1]
    old_public = age.public_key(key_file)
    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    assert "kubectl -n flux-system delete secret sops-age" in capsys.readouterr().out
    held = age.public_keys(key_file)
    assert len(held) == 2 and held[1] == old_public
    rule = age.recipients(instance / ".sops.yaml", ENV)
    assert rule == [held[0], recovery_public]
    new_secret = next(line for line in key_file.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-"))
    new_only = _one_identity(tmp_path, new_secret, "new.txt")
    old_only = _one_identity(tmp_path, old_secret, "old.txt")
    for path in recipients.sealed_files(instance / "envs" / ENV):
        assert _opens(path, new_only) and not _opens(path, old_only), path
    assert _schema_ok(instance)


def test_a_key_that_is_not_a_recipient_cannot_rotate(instance: Path, tmp_path: Path) -> None:
    stranger = tmp_path / "stranger.txt"
    age.create_key(stranger)
    with pytest.raises(recipients.RecipientsError, match="not one of"):
        recipients.rotate(instance, ENV, stranger)


def test_rotating_the_deploy_key_seals_a_new_one(instance: Path, capsys) -> None:
    sealed = instance / "envs" / ENV / "secrets" / "flux-deploy-key.sops.yaml"
    assert not sealed.exists()
    assert mint.main(["--env", ENV, "--rotate", "deploy-key"]) == 0
    first = sealed.read_bytes()
    out = capsys.readouterr().out
    assert "ssh-ed25519 " in out and "remove the old deploy key from the forge" in out
    assert mint.main(["--env", ENV, "--rotate", "deploy-key"]) == 0
    assert sealed.read_bytes() != first
    assert _opens(sealed, instance / ".age" / "keys.txt")


def test_doctor_asks_for_the_recovery_key_until_the_env_has_one(instance: Path) -> None:
    from jarvis.cli.doctor import check_recovery

    env_dir = instance / "envs" / ENV
    assert [level for level, _ in check_recovery(instance, env_dir)] == ["WARN"]
    assert "--add-recovery-key" in check_recovery(instance, env_dir)[0][1]
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    assert [level for level, _ in check_recovery(instance, env_dir)] == ["PASS"]


def test_prep_accepts_a_key_file_that_holds_the_old_and_new_keys(instance: Path, capsys) -> None:
    """Rule: mid-rotation the key file holds two identities; node prep passes when any one of
    them is a recipient, and names that one."""
    from jarvis.node import prep

    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    capsys.readouterr()
    prep.check_age_key(instance, ENV)
    assert age.public_keys(instance / ".age" / "keys.txt")[0] in capsys.readouterr().out


def test_a_lost_working_key_is_replaced_from_the_recovery_key(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: with only the recovery key at hand, --rotate age replaces the lost working key (the
    rule's first recipient) and keeps the recovery key; the lost key opens nothing afterwards."""
    key_file = instance / ".age" / "keys.txt"
    lost = _one_identity(
        tmp_path,
        next(line for line in key_file.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-")),
        "lost.txt",
    )
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    secret = next(
        line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("AGE-SECRET-KEY-")
    )
    lost_public, recovery_public = age.recipients(instance / ".sops.yaml", ENV)
    key_file.write_text(secret + "\n", encoding="utf-8")
    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    new_public = age.public_keys(key_file)[0]
    assert age.recipients(instance / ".sops.yaml", ENV) == [new_public, recovery_public]
    assert lost_public not in age.recipients(instance / ".sops.yaml", ENV)
    for path in recipients.sealed_files(instance / "envs" / ENV):
        assert not _opens(path, lost), path
    assert _schema_ok(instance)


def test_a_key_that_opens_nothing_changes_nothing_and_shows_no_recovery_key(instance: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    """Rule: every check comes before the first change. A working key that cannot open the env's
    files (the wrong user's, another env's, none at all) is refused before a recovery key is made,
    printed or added to the rule."""
    rule = (instance / ".sops.yaml").read_text()
    stranger = tmp_path / "stranger.txt"
    age.create_key(stranger)
    for key_file, message in ((stranger, "does not open"), (tmp_path / "absent.txt", "no age key at")):
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))
        for flags in (["--add-recovery-key"], ["--update-recipients"], ["--rotate", "age"]):
            assert mint.main(["--env", ENV, *flags]) == 1
            out = capsys.readouterr().out
            assert message in out and "nothing was changed" in out and "AGE-SECRET-KEY-" not in out
            assert (instance / ".sops.yaml").read_text() == rule
    assert stranger.read_text().count("AGE-SECRET-KEY-") == 1


def test_a_failure_after_the_rule_changed_puts_it_back(instance: Path, capsys, monkeypatch) -> None:
    """Rule: if re-encrypting fails partway, .sops.yaml is restored and the files already done are
    named, so the repository goes back to what was committed."""
    rule = (instance / ".sops.yaml").read_text()

    def fail(_instance, _env):
        raise recipients.RecipientsError("sops updatekeys failed", ["envs/env-00/secrets/k3s-token.sops.yaml"])

    monkeypatch.setattr(recipients, "update", fail)
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 1
    out = capsys.readouterr().out
    assert (instance / ".sops.yaml").read_text() == rule
    assert "never used: discard it" in out and "git checkout -- envs/env-00/secrets/k3s-token.sops.yaml" in out

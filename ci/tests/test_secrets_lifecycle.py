"""An env's age recipients over its life: the recovery key, re-encrypting, and rotating keys."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization

import support
from jarvis.cli.doctor import check_recovery
from jarvis.env.runtime import kubeconfig_for
from jarvis.env.validate import CHECKS
from jarvis.node import prep
from jarvis.secrets import age, mint, recipients

ENV = "env-00"
OPERATIONS = [
    pytest.param(["--add-recovery-key"], id="add-recovery-key"),
    pytest.param(["--update-recipients"], id="update-recipients"),
    pytest.param(["--rotate", "age"], id="rotate-age"),
]


@pytest.fixture
def instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the core-only sample instance, whose secrets open for one key only, as an env made before v0.5.0."""
    root = support.copy_sample(tmp_path / "instance")
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


def _identity(text: str) -> str:
    """The first age private key line in text."""
    return next(line.strip() for line in text.splitlines() if line.strip().startswith("AGE-SECRET-KEY-"))


def _one_identity(tmp_path: Path, secret: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def test_the_recovery_key_is_added_once_and_opens_every_file(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: --add-recovery-key adds a second recipient, shows its private half once and writes it
    nowhere, and re-encrypts every file so either key opens it. A second run changes nothing."""
    assert len(age.recipients(instance / ".sops.yaml", ENV)) == 1
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    secret = _identity(capsys.readouterr().out)
    assert not [path for path in instance.rglob("*") if path.is_file() and secret in path.read_text(errors="ignore")]
    assert len(age.recipients(instance / ".sops.yaml", ENV)) == 2
    recovery = _one_identity(tmp_path, secret, "recovery.txt")
    files = recipients.sealed_files(instance / "envs" / ENV)
    assert files and all(_opens(path, recovery) for path in files)
    assert _schema_ok(instance)
    before = _snapshot([instance / ".sops.yaml", *files])
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    assert capsys.readouterr().out == (
        f"PASS envs/{ENV}/secrets/ already has a second recipient; nothing changed\n"
        f"PASS every sealed file of {ENV} opens for exactly its recipients; nothing changed\n"
    )
    assert _snapshot([instance / ".sops.yaml", *files]) == before


def test_update_recipients_follows_the_rule(instance: Path, capsys) -> None:
    """Rule: after the rule changes, the schema names every file left behind, and
    --update-recipients re-encrypts exactly those."""
    _secret, extra = age.new_key_in_memory()
    keys = age.recipients(instance / ".sops.yaml", ENV)
    recipients.set_rule(instance / ".sops.yaml", ENV, [*keys, extra])
    files = [path.relative_to(instance).as_posix() for path in recipients.sealed_files(instance / "envs" / ENV)]
    assert files and recipients.problems(instance, instance / "envs" / ENV) == [
        f"{rel}: encrypted to 1 age recipient(s) that are not the 2 in .sops.yaml's rule for envs/{ENV}/secrets/;"
        " run jarvis secrets --update-recipients"
        for rel in files
    ]
    assert mint.main(["--env", ENV, "--update-recipients"]) == 0
    assert capsys.readouterr().out == "".join(f"PASS re-encrypted {rel}\n" for rel in files) + (
        "Next: commit and push the re-encrypted files.\n"
    )
    assert recipients.problems(instance, instance / "envs" / ENV) == []
    assert recipients.update(instance, ENV) == []


def test_rotating_the_working_key_retires_the_old_one_from_every_file(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: --rotate age puts a new identity first in the key file, swaps it for the old public
    key in the rule (the recovery key stays), and re-encrypts: the old key alone opens nothing."""
    key_file = instance / ".age" / "keys.txt"
    old_secret = _identity(key_file.read_text())
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    capsys.readouterr()
    recovery_public = age.recipients(instance / ".sops.yaml", ENV)[1]
    old_public = age.public_key(key_file)
    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    held = age.public_keys(key_file)
    assert len(held) == 2 and held[1] == old_public
    rule = age.recipients(instance / ".sops.yaml", ENV)
    assert rule == [held[0], recovery_public]
    files = recipients.sealed_files(instance / "envs" / ENV)
    # The printed step names this env's cluster, never the controller's default kubeconfig.
    assert capsys.readouterr().out == (
        f"PASS new working key {held[0]} in {key_file}, in place of {old_public} in .sops.yaml\n"
        + "".join(f"PASS re-encrypted {path.relative_to(instance).as_posix()}\n" for path in files)
        + "Next: commit and push, then put the new key in the cluster: "
        f"kubectl --kubeconfig {kubeconfig_for(ENV)} -n flux-system delete secret sops-age && jarvis site --tags "
        f"flux-operator (it recreates sops-age from {key_file}). Once Flux is Ready, delete every other key's lines "
        "from that file, so it holds the new working key only.\n"
    )
    new_only = _one_identity(tmp_path, _identity(key_file.read_text()), "new.txt")
    old_only = _one_identity(tmp_path, old_secret, "old.txt")
    for path in files:
        assert _opens(path, new_only) and not _opens(path, old_only), path
    assert _schema_ok(instance)


def test_a_key_that_is_not_a_recipient_cannot_rotate(instance: Path, tmp_path: Path) -> None:
    """Rule: rotate refuses a key file that holds none of the env's recipients, before it writes anything."""
    stranger = tmp_path / "stranger.txt"
    age.create_key(stranger)
    before = _snapshot([instance / ".sops.yaml", stranger])
    with pytest.raises(recipients.RecipientsError) as refused:
        recipients.rotate(instance, ENV, stranger)
    assert str(refused.value) == f"{stranger} is not one of envs/{ENV}/secrets/'s recipients; it cannot rotate them"
    assert _snapshot([instance / ".sops.yaml", stranger]) == before


def test_rotating_the_deploy_key_seals_a_new_one(instance: Path, capsys) -> None:
    """Rule: each --rotate deploy-key seals a new deploy key in place of the last, and prints the public half of
    the one it sealed with the steps that swap it on the forge."""
    sealed = instance / "envs" / ENV / "secrets" / "flux-deploy-key.sops.yaml"
    assert not sealed.exists()
    seen = []
    for _ in range(2):
        assert mint.main(["--env", ENV, "--rotate", "deploy-key"]) == 0
        seen.append(sealed.read_bytes())
        identity = yaml.safe_load(subprocess.check_output(["sops", "decrypt", str(sealed)], text=True))["stringData"]["identity"]
        public = serialization.load_ssh_private_key(identity.encode(), None).public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        assert public.startswith(b"ssh-ed25519 ")
        assert capsys.readouterr().out == (
            f"PASS sealed a new envs/{ENV}/secrets/flux-deploy-key.sops.yaml\n"
            "Next: add this read-only deploy key to the repository on the forge, commit and push, then "
            f"kubectl --kubeconfig {kubeconfig_for(ENV)} -n flux-system delete secret flux-deploy-key && jarvis site "
            "--tags flux-operator; once Flux reads the repo again, remove the old deploy key from the forge.\n"
            f"  {public.decode()} flux-{ENV}\n"
        )
    assert seen[0] != seen[1]


def test_doctor_asks_for_the_recovery_key_until_the_env_has_one(instance: Path) -> None:
    """Rule: doctor WARNs, naming --add-recovery-key, while the env's secrets open for one key only,
    and PASSes once they open for the recovery key too."""
    env_dir = instance / "envs" / ENV
    assert check_recovery(instance, env_dir) == [
        ("WARN", f"envs/{ENV}/secrets/ open for one age key only; add the recovery key: jarvis secrets --add-recovery-key")
    ]
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    assert check_recovery(instance, env_dir) == [("PASS", f"envs/{ENV}/secrets/ open for 2 age keys (working and recovery)")]


@pytest.mark.parametrize("which", [pytest.param(0, id="rule-has-the-new-key"), pytest.param(1, id="rule-has-the-old-key")])
def test_prep_accepts_a_key_file_that_holds_the_old_and_new_keys(instance: Path, capsys, which: int) -> None:
    """Rule: mid-rotation the key file holds two identities; node prep passes when any one of
    them is a recipient, and names that one."""
    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    held = age.public_keys(instance / ".age" / "keys.txt")
    recipients.set_rule(instance / ".sops.yaml", ENV, [held[which]])
    capsys.readouterr()
    prep.check_age_key(instance, ENV)
    assert capsys.readouterr().out == f"Age public key: {held[which]}\n"


def test_a_lost_working_key_is_replaced_from_the_recovery_key(instance: Path, tmp_path: Path, capsys) -> None:
    """Rule: with only the recovery key at hand, --rotate age replaces the lost working key (the
    rule's first recipient) and keeps the recovery key; the lost key opens nothing afterwards."""
    key_file = instance / ".age" / "keys.txt"
    lost = _one_identity(tmp_path, _identity(key_file.read_text()), "lost.txt")
    assert mint.main(["--env", ENV, "--add-recovery-key"]) == 0
    secret = _identity(capsys.readouterr().out)
    lost_public, recovery_public = age.recipients(instance / ".sops.yaml", ENV)
    key_file.write_text(secret + "\n", encoding="utf-8")
    assert mint.main(["--env", ENV, "--rotate", "age"]) == 0
    new_public = age.public_keys(key_file)[0]
    assert age.recipients(instance / ".sops.yaml", ENV) == [new_public, recovery_public]
    assert lost_public not in age.recipients(instance / ".sops.yaml", ENV)
    for path in recipients.sealed_files(instance / "envs" / ENV):
        assert not _opens(path, lost), path
    assert _schema_ok(instance)


@pytest.mark.parametrize("flags", OPERATIONS)
@pytest.mark.parametrize("key", ["stranger", "absent"])
def test_a_key_that_opens_nothing_changes_nothing_and_shows_no_recovery_key(
    instance: Path, tmp_path: Path, capsys, monkeypatch, key: str, flags: list[str]
) -> None:
    """Rule: every check comes before the first change: a working key that opens none of the env's files, or none
    at all, is refused before a recovery key is made, printed or added to the rule."""
    key_file = tmp_path / f"{key}.txt"
    if key == "stranger":
        age.create_key(key_file)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))
    files = recipients.sealed_files(instance / "envs" / ENV)
    before = _snapshot([instance / ".sops.yaml", key_file, *files])
    assert mint.main(["--env", ENV, *flags]) == 1
    assert capsys.readouterr().out == "FAIL secrets: " + (
        f"{key_file} does not open {len(files)} of envs/{ENV}'s sealed files ({files[0].relative_to(instance)} and"
        " more): it is not this env's working key; nothing was changed\n"
        if key == "stranger"
        else f"no age key at {key_file} (age_key_file, or SOPS_AGE_KEY_FILE): run this as the user that holds the"
        " env's working key; nothing was changed\n"
    )
    assert _snapshot([instance / ".sops.yaml", key_file, *files]) == before


@pytest.mark.parametrize("flags", [OPERATIONS[0], OPERATIONS[2]])
def test_a_failure_partway_puts_everything_back(instance: Path, capsys, monkeypatch, flags: list[str]) -> None:
    """Rule: if re-encrypting fails partway, .sops.yaml, every sealed file already re-encrypted and the key file (a
    rotation's new identity) are put back as they were, with nothing left for the owner to undo by hand."""
    key_file = instance / ".age" / "keys.txt"
    sealed = recipients.sealed_files(instance / "envs" / ENV)
    rules = []

    def fail_after_one(repo: Path, env: str) -> list[str]:
        # The first file is re-encrypted for the new rule, then sops fails on the next.
        rules.append(age.recipients(repo / ".sops.yaml", env))
        sealed[0].write_bytes(sealed[0].read_bytes() + b"# re-encrypted\n")
        raise recipients.RecipientsError("sops updatekeys failed")

    monkeypatch.setattr(recipients, "update", fail_after_one)
    files = [instance / ".sops.yaml", key_file, *sealed]
    before = _snapshot(files)
    old = age.public_key(key_file)
    assert mint.main(["--env", ENV, *flags]) == 1
    printed, failure = capsys.readouterr().out.split("FAIL secrets: ")
    assert _snapshot(files) == before
    if flags == ["--rotate", "age"]:
        assert printed == f"PASS new working key {rules[0][0]} in {key_file}, in place of {old} in .sops.yaml\n"
        put_back, discard = f".sops.yaml, .age/keys.txt, {sealed[0].relative_to(instance)}", ""
    else:
        line = "=" * 78
        assert printed == (
            f"\n{line}\nThe RECOVERY KEY. It is shown this once and kept nowhere else. Store it offline now\n(paper, or a "
            "password manager entry you do not use day to day). With it, the secrets\nopen even if the working key is "
            f"lost.\n\n  {_identity(printed)}\n\n  public key: {rules[0][1]}\n{line}\n\n"
        )
        put_back = f".sops.yaml, {sealed[0].relative_to(instance)}"
        discard = "     The recovery key printed above was never used: discard it.\n"
    assert failure == f"sops updatekeys failed\n     Put back as they were: {put_back}.\n{discard}"

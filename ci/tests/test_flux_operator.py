"""flux-operator Ansible role: template rules and check-mode proof."""

from __future__ import annotations

import base64
import getpass
import grp
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    env_name,
    example_flux_entry,
    load_cluster_vars,
    load_role_defaults,
    playbook_recap_field,
)
from conftest import require_cli

from support import sample_root

SITE_YML = ANSIBLE_DIR / "site.yml"


_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)


def _role_names(play: dict[str, object]) -> list[str]:
    names: list[str] = []
    for role in play.get("roles") or []:
        if isinstance(role, str):
            names.append(role)
        elif isinstance(role, dict) and isinstance(role.get("role"), str):
            names.append(role["role"])
    for task in play.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        for key in _ROLE_KEYS:
            included = task.get(key)
            if isinstance(included, dict) and isinstance(included.get("name"), str):
                names.append(included["name"])
            elif isinstance(included, str):
                names.append(included)
    return names








def _site_plays() -> list[dict[str, object]]:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def _task_documents() -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for path in sorted(FLUX_OPERATOR_ROLE_TASKS.glob("*.yml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            docs.extend(item for item in loaded if isinstance(item, dict))
    return docs

def _task_text(task: dict[str, object]) -> str:
    return yaml.safe_dump(task, sort_keys=False)

def test_sops_age_tasks_use_no_log() -> None:
    needles = (
        "SOPS_AGE_KEY_FILE",
        "flux_sops_age_key",
        "age private key",
        "age.agekey",
        "flux_sops_age_existing_json",
        "flux_sops_age_file_identities",
        "flux_sops_age_publics",
    )
    for task in _task_documents():
        blob = _task_text(task).lower()
        if not any(needle.lower() in blob for needle in needles):
            continue
        assert task.get("no_log") is True, task.get("name", task)
    stat = next(
        task for task in _task_documents() if task.get("name") == "Stat the age private key file"
    )
    assert stat.get("delegate_to") == "localhost"
    assert stat.get("become") is False
    assert stat.get("run_once") is True


def test_untagged_check_run_skips_the_flux_operator_play() -> None:
    """The flux-operator role's condition is false for an untagged --check run (run tags
    ['all']) and true once TAGS=flux-operator is named or the run is not a check."""
    import jinja2

    play = next(p for p in _site_plays() if "flux-operator" in _role_names(p))
    role = next(r for r in play["roles"] if r["role"] == "flux-operator")
    applies = jinja2.Environment().compile_expression(role["when"])
    assert not applies(ansible_check_mode=True, ansible_run_tags=["all"])
    assert applies(ansible_check_mode=True, ansible_run_tags=["flux-operator"])
    assert applies(ansible_check_mode=False, ansible_run_tags=["all"])


def _ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env.pop("SOPS_AGE_KEY_FILE", None)
    return env


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _flux_operator_extra(binary: Path) -> list[str]:
    cluster = load_cluster_vars()
    return [
        "-e",
        f"k3s_binary_path={binary}",
        "-e",
        f"jarvis_env={cluster['jarvis_env']}",
        "-e",
        f"flux_git_repo_url={cluster['flux_git_repo_url']}",
        "-e",
        f"flux_git_ref={cluster['flux_git_ref']}",
    ]


def _backup_inventory(tmp_path: Path) -> Path:
    path = tmp_path / "backup-hosts.yml"
    path.write_text(
        "all:\n"
        "  children:\n"
        "    backup:\n"
        "      hosts:\n"
        "        backup-host:\n"
        "          ansible_connection: local\n",
        encoding="utf-8",
    )
    return path


def _run_localhost_role(
    play_text: str,
    tmp_path: Path,
    extra: list[str],
    *,
    env: dict[str, str],
    check: bool = False,
    inventory: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    play = tmp_path / "play.yml"
    play.write_text(play_text, encoding="utf-8")
    cmd = ["ansible-playbook", str(play)]
    for source in inventory or ["localhost,"]:
        cmd.extend(["-i", source])
    cmd.extend(
        [
            "-e",
            "ansible_connection=local",
            *extra,
        ]
    )
    if check:
        cmd.append("--check")
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_flux_operator_check_mode_idempotent_when_done(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            case "$joined" in
              *"get deployment flux-operator"*)
                echo "deployment.apps/flux-operator"
                exit 0
                ;;
              *"get gitrepositories.source.toolkit.fluxcd.io flux-system"*)
                echo "gitrepository.source.toolkit.fluxcd.io/flux-system"
                exit 0
                ;;
              *"get secret sops-age"*)
                echo "secret/sops-age"
                exit 0
                ;;
              *" wait "*)
                exit 0
                ;;
            esac
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Flux operator already installed
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
        """
    )
    extra = _flux_operator_extra(binary)
    missing_key = tmp_path / "missing-age-key"
    extra.extend(["-e", f"age_key_file={missing_key}"])
    env = _ansible_env()
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    first = _run_localhost_role(play, tmp_path, extra, env=env, check=True, inventory=inventory)
    second = _run_localhost_role(play, tmp_path, extra, env=env, check=True, inventory=inventory)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert playbook_recap_field(second.stdout + second.stderr, "changed") == 0
    # A re-run with the operator present still waits for the CRD and the rollout.
    invocations = log.read_text(encoding="utf-8")
    assert invocations.count("crd/fluxinstances.fluxcd.controlplane.io") == 2
    assert invocations.count("--for=condition=Available deployment/flux-operator") == 2


def test_existing_sops_age_skips_key_when_unset(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    binary.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'if [[ "$*" == *"get secret sops-age -o name" ]]; then\n'
        "  echo secret/sops-age\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected k3s: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            """\
            ---
            - name: Re-run sops-age without the private key
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: sops_age.yml
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("SOPS_AGE_KEY_FILE", None)
    env["ANSIBLE_BECOME"] = "false"
    proc = subprocess.run(
        [
            "ansible-playbook",
            str(play),
            "-i",
            "localhost,",
            "-e",
            "ansible_connection=local",
            "-e",
            f"k3s_binary_path={binary}",
            "-e",
            f"age_key_file={tmp_path / 'missing-age-key'}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    invocations = log.read_text(encoding="utf-8")
    assert "get secret sops-age -o name" in invocations
    assert "-o json" not in invocations
    assert "create" not in invocations


_SOPS_ENV = "env-test"
_SOPS_PLAY = textwrap.dedent(
    """\
    ---
    - name: sops-age checks, then the bootstrap secret
      hosts: localhost
      gather_facts: false
      become: false
      tasks:
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age.yml
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age_secret.yml
    """
)


def _failed_task(output: str) -> str | None:
    """The name of the task whose result is the first `fatal:` line."""
    task = None
    for line in output.splitlines():
        header = re.match(r"TASK \[(?:[^:\]]+ : )?(.*)\] \**$", line)
        if header:
            task = header.group(1)
        elif line.startswith("fatal:"):
            return task
    return None


def _age_identity(tmp_path: Path, name: str) -> tuple[str, str]:
    """A throwaway age identity line and its public key. Nothing is printed."""
    require_cli("age-keygen")
    path = tmp_path / f"{name}.agekey"
    proc = subprocess.run(
        ["age-keygen", "-o", str(path)], capture_output=True, text=True, check=True
    )
    public = re.search(r"age1[0-9a-z]+", proc.stderr)
    assert public is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    identity = next(line for line in lines if line.startswith("AGE-SECRET-KEY-1"))
    return identity, public.group(0)


def _key_file(tmp_path: Path, *identities: tuple[str, str]) -> Path:
    """A key file in age-keygen's layout: comment lines, then each identity."""
    path = tmp_path / "keys.txt"
    body = "".join(
        f"# created: 2026-01-01T00:00:00Z\n# public key: {public}\n{identity}\n"
        for identity, public in identities
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _sops_config(tmp_path: Path, env: str, *publics: str, as_list: bool = False) -> Path:
    """A SOPS config whose rule for env names publics, as one comma string or a YAML list."""
    path = tmp_path / "sops-config.yaml"
    rule = {
        "path_regex": f"envs/{env}/secrets/.*\\.sops\\.yaml$",
        "encrypted_regex": "^(data|stringData)$",
        "age": list(publics) if as_list else ",".join(publics),
    }
    path.write_text(yaml.safe_dump({"creation_rules": [rule]}), encoding="utf-8")
    return path


def _sops_k3s(tmp_path: Path, existing: str | None) -> tuple[Path, Path, Path]:
    """Fake k3s for the sops-age tasks: (binary, log, created manifest).

    existing is the secret's age.agekey, or None when the secret is absent.
    The create branch reads all of stdin before it exits.
    """
    log = tmp_path / "k3s.log"
    created = tmp_path / "created.yaml"
    secret = tmp_path / "secret.json"
    if existing is None:
        lookup = "echo 'Error from server (NotFound): secrets \"sops-age\" not found' >&2; exit 1"
    else:
        encoded = base64.b64encode(existing.encode("utf-8")).decode("ascii")
        secret.write_text(json.dumps({"data": {"age.agekey": encoded}}), encoding="utf-8")
        lookup = "echo secret/sops-age; exit 0"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'joined="$*"\n'
        f'if [[ "$joined" == *"get secret sops-age -o name"* ]]; then {lookup}; fi\n'
        f'if [[ "$joined" == *"get secret sops-age -o json"* ]]; then cat {secret}; exit 0; fi\n'
        f'if [[ "$joined" == *"kubectl create -f -"* ]]; then cat > {created}; exit 0; fi\n'
        'echo "unexpected k3s invocation: $*" >&2\n'
        "exit 1\n",
    )
    return binary, log, created


def _run_sops_age(
    tmp_path: Path, binary: Path, keyfile: Path, sops_config: Path, *, play: str = _SOPS_PLAY,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    args = [
        "-e", f"k3s_binary_path={binary}",
        "-e", f"jarvis_env={_SOPS_ENV}",
        "-e", f"flux_sops_config_file={sops_config}",
        *(extra or []),
    ]
    return _run_localhost_role(play, tmp_path, args, env=env)


@pytest.mark.parametrize("as_list", [False, True], ids=["comma-string", "yaml-list"])
def test_new_sops_age_secret_holds_only_the_env_identity(tmp_path: Path, as_list: bool) -> None:
    """A new flux-system/sops-age is created (never applied) with only the identity for the env's recipients."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    second_recipient = _age_identity(tmp_path, "second")
    keyfile = _key_file(tmp_path, other, mine)
    binary, log, created = _sops_k3s(tmp_path, existing=None)
    sops_config = _sops_config(
        tmp_path, _SOPS_ENV, second_recipient[1], mine[1], as_list=as_list
    )
    proc = _run_sops_age(tmp_path, binary, keyfile, sops_config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    invocations = log.read_text(encoding="utf-8")
    assert "kubectl create -f -" in invocations
    assert "kubectl apply" not in invocations
    secret = yaml.safe_load(created.read_text(encoding="utf-8"))
    assert (secret["kind"], secret["metadata"]["namespace"], secret["metadata"]["name"]) == (
        "Secret",
        "flux-system",
        "sops-age",
    )
    assert secret["stringData"] == {"age.agekey": mine[0]}


@pytest.mark.parametrize("case", ["full-key-file", "env-identity-only"])
def test_existing_secret_with_the_env_identity_passes_untouched(tmp_path: Path, case: str) -> None:
    """The refusal compares identities: a secret holding the env's one, with or without other lines, is left alone."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    keyfile = _key_file(tmp_path, other, mine)
    existing = keyfile.read_text(encoding="utf-8") if case == "full-key-file" else mine[0]
    binary, log, created = _sops_k3s(tmp_path, existing=existing)
    proc = _run_sops_age(tmp_path, binary, keyfile, _sops_config(tmp_path, _SOPS_ENV, mine[1]))
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert "get secret sops-age -o json" in log.read_text(encoding="utf-8")
    assert not created.exists()


def test_two_matching_identities_are_one_line_each(tmp_path: Path) -> None:
    """Two identities for the env's recipients land on two lines, and the set check uses both."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    first = _age_identity(tmp_path, "first")
    second = _age_identity(tmp_path, "second")
    keyfile = _key_file(tmp_path, other, first, second)
    sops_config = _sops_config(tmp_path, _SOPS_ENV, first[1], second[1])

    created_dir = tmp_path / "create"
    created_dir.mkdir()
    binary, _log, created = _sops_k3s(created_dir, existing=None)
    proc = _run_sops_age(created_dir, binary, keyfile, sops_config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    secret = yaml.safe_load(created.read_text(encoding="utf-8"))
    assert secret["stringData"]["age.agekey"].splitlines() == [first[0], second[0]]

    # The whole key file, comments and a third identity included, holds both.
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    binary, _log, created = _sops_k3s(full_dir, existing=keyfile.read_text(encoding="utf-8"))
    proc = _run_sops_age(full_dir, binary, keyfile, sops_config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not created.exists()

    # A secret with only one of the two does not hold the env's identities.
    partial_dir = tmp_path / "partial"
    partial_dir.mkdir()
    binary, _log, created = _sops_k3s(partial_dir, existing=first[0])
    proc = _run_sops_age(partial_dir, binary, keyfile, sops_config)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == (
        "Refuse to overwrite an existing sops-age secret with different content"
    ), output
    assert not created.exists()


def test_existing_secret_with_another_identity_refuses(tmp_path: Path) -> None:
    """A secret without the env's identity may be the only copy of another key, so the run refuses."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    binary, _log, created = _sops_k3s(tmp_path, existing=f"# public key: {other[1]}\n{other[0]}")
    keyfile = _key_file(tmp_path, mine)
    proc = _run_sops_age(tmp_path, binary, keyfile, _sops_config(tmp_path, _SOPS_ENV, mine[1]))
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == (
        "Refuse to overwrite an existing sops-age secret with different content"
    ), output
    assert mine[0] not in output and other[0] not in output
    assert not created.exists()


@pytest.mark.parametrize(
    ("rule_env", "refusal"),
    [
        (None, "Refuse a key file with no identity for the env's age recipient"),
        ("env-other", "Require an age recipient for this env"),
    ],
    ids=["no-matching-identity", "no-recipient-for-env"],
)
def test_key_file_without_the_env_identity_refuses_before_the_operator_install(
    tmp_path: Path, rule_env: str | None, refusal: str
) -> None:
    """The full role stops at the age key check, before Helm or any cluster write."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    recipient = _age_identity(tmp_path, "recipient")
    keyfile = _key_file(tmp_path, other)
    binary, log, created = _sops_k3s(tmp_path, existing=None)
    helm_log = tmp_path / "helm.log"
    helm = tmp_path / "helm"
    _write_executable(helm, f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {helm_log}\nexit 0\n")
    cluster_env = str(load_cluster_vars()["jarvis_env"])
    play = textwrap.dedent(
        """\
        ---
        - name: Full flux-operator role
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
        """
    )
    sops_config = _sops_config(tmp_path, rule_env or cluster_env, recipient[1])
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    extra = [
        *_flux_operator_extra(binary),
        "-e", f"helm_binary_path={helm}",
        "-e", f"flux_sops_config_file={sops_config}",
    ]
    proc = _run_localhost_role(play, tmp_path, extra, env=env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == refusal, output
    assert other[0] not in output
    assert "get deployment" not in log.read_text(encoding="utf-8")
    assert not helm_log.exists()
    assert not created.exists()


def _assert_key_was_not_read(proc: subprocess.CompletedProcess[str], sentinel: str) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Refuse an age private key" in output
    assert sentinel not in output
    assert "Derive the public key of each identity" not in output


def test_missing_age_keygen_is_named_before_no_log(tmp_path: Path) -> None:
    """A missing age-keygen fails in a visible task that names the binary."""
    playbook = Path(require_cli("ansible-playbook"))
    keyfile = tmp_path / "age.key"
    keyfile.write_text("not-read\n", encoding="utf-8")
    keyfile.chmod(0o600)
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'if [[ "$*" == *"get secret sops-age -o name"* ]]; then\n'
        "  echo 'Error from server (NotFound): secrets \"sops-age\" not found' >&2\n"
        "  exit 1\nfi\n"
        'echo "unexpected k3s: $*" >&2\nexit 1\n',
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Hide age-keygen only. ansible-playbook stays, including when it lives
    # outside /bin and /usr/bin, which is where CI's venv puts it.
    for directory in (Path("/bin"), Path("/usr/bin"), playbook.parent):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            dest = bindir / entry.name
            # /bin is often /usr/bin, so each entry comes twice; is_symlink, unlike exists, also
            # sees a link already made to a dangling one (ubuntu-26.04's grub-ntldr-img).
            if entry.name == "age-keygen" or dest.is_symlink():
                continue
            dest.symlink_to(entry)
    if not (bindir / "ansible-playbook").exists():
        (bindir / "ansible-playbook").symlink_to(playbook)
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    env["PATH"] = str(bindir)
    proc = _run_localhost_role(
        textwrap.dedent(
            """\
            ---
            - name: Missing age-keygen
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: sops_age.yml
            """
        ),
        tmp_path,
        ["-e", f"k3s_binary_path={binary}"],
        env=env,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Require age-keygen on the controller", output
    assert "age-keygen is not on PATH" in output
    assert "not-read" not in output
    assert "Derive the public key" not in output


def test_sops_age_refuses_mode_0644_without_reading_the_key(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o644)
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get secret sops-age -o name"* ]]; then
              echo 'Error from server (NotFound): secrets "sops-age" not found' >&2
              exit 1
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Refuse a world-readable age key
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: sops_age.yml
        """
    )
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    proc = _run_localhost_role(play, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
    _assert_key_was_not_read(proc, sentinel)
    invocations = log.read_text(encoding="utf-8")
    assert "get secret sops-age -o name" in invocations
    assert "create" not in invocations
    assert "-o json" not in invocations


def test_sops_age_refuses_wrong_owner_from_stat(tmp_path: Path) -> None:
    """Stat uid is faked when this user cannot chown a file to someone else."""
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    other = os.getuid() + 1
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o600)
    try:
        os.chown(keyfile, other, os.getgid())
    except OSError:
        chowned = False
    else:
        chowned = keyfile.stat().st_uid == other
    if chowned:
        log = tmp_path / "k3s.log"
        binary = tmp_path / "k3s"
        _write_executable(
            binary,
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> {log}\n"
            'if [[ "$*" == *"get secret sops-age -o name"* ]]; then\n'
            '  echo \'Error from server (NotFound): secrets "sops-age" not found\' >&2\n'
            "  exit 1\n"
            "fi\n"
            'echo "unexpected k3s: $*" >&2\nexit 1\n',
        )
        play = textwrap.dedent(
            """\
            ---
            - name: Refuse an age key owned by someone else
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: sops_age.yml
            """
        )
        env = _ansible_env()
        env["SOPS_AGE_KEY_FILE"] = str(keyfile)
        proc = _run_localhost_role(play, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
        _assert_key_was_not_read(proc, sentinel)
        assert "create" not in log.read_text(encoding="utf-8")
        return
    play = textwrap.dedent(
        f"""\
        ---
        - name: Fake age key stat for a different owner
          hosts: localhost
          gather_facts: false
          become: false
          vars:
            sops_age_key_stat:
              stat:
                exists: true
                mode: "0600"
                uid: {other}
          tasks:
            - ansible.builtin.include_role:
                name: flux-operator
                tasks_from: sops_age_key_mode.yml
        """
    )
    proc = _run_localhost_role(play, tmp_path, [], env=_ansible_env())
    _assert_key_was_not_read(proc, sentinel)


def test_rerun_with_root_source_present_does_not_set_rebuild_hold(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get gitrepositories"* ]]; then
              echo "gitrepository.source.toolkit.fluxcd.io/flux-system"
              exit 0
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Root GitRepository already present
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    extra = ["-e", f"k3s_binary_path={binary}"]
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    first = _run_localhost_role(play, tmp_path, extra, env=env, inventory=inventory)
    second = _run_localhost_role(play, tmp_path, extra, env=env, inventory=inventory)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    invocations = log.read_text(encoding="utf-8")
    assert invocations.count("get gitrepositories") == 2
    assert "apply" not in invocations
    assert "backup-rebuild-hold" not in invocations
    assert "configmap" not in invocations.lower()


def _become_exe(tmp_path: Path) -> Path:
    path = tmp_path / "sudo"
    _write_executable(
        path,
        textwrap.dedent(
            """\
            #!/bin/bash
            # Ansible become: sudo -H -S -n -u root <command>
            skip=0
            args=()
            for arg in "$@"; do
              if [[ "$skip" -eq 1 ]]; then
                skip=0
                continue
              fi
              case "$arg" in
                -u) skip=1 ;;
                -H|-S|-n) ;;
                *) args+=("$arg") ;;
              esac
            done
            exec "${args[@]}"
            """
        ),
    )
    return path


def test_fresh_cluster_sets_the_hold_then_applies_the_flux_instance_and_root_source(
    tmp_path: Path,
) -> None:
    """On a new cluster: the hold, then the FluxInstance, then sync.yaml with the root source."""
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    stdin_path = tmp_path / "apply.stdin"
    flux_stdin_path = tmp_path / "flux-apply.stdin"
    sync_stdin_path = tmp_path / "sync-apply.stdin"
    sync_file = sample_root() / "envs" / env_name() / "flux" / "sync.yaml"
    env_file = example_flux_entry(tmp_path).instance_file
    binary = tmp_path / "k3s"
    restic = tmp_path / "backup" / "restic"
    (restic / "ns-demo" / "pvc-drill").mkdir(parents=True)
    (restic / "ns-demo" / "pvc-drill" / "config").write_text("repo\n", encoding="utf-8")
    (restic / "ns-decoy" / "notpvc").mkdir(parents=True)
    (restic / "ns-decoy" / "notpvc" / "config").write_text("nope\n", encoding="utf-8")
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get gitrepositories"* ]]; then
              echo 'Error from server (NotFound): gitrepositories.source.toolkit.fluxcd.io "flux-system" not found' >&2
              exit 1
            fi
            if [[ "$joined" == *" wait "* ]]; then
              exit 0
            fi
            if [[ "$joined" == *"kubectl apply -f -"* ]]; then
              body="$(cat)"
              if [[ "$body" == *"kind: FluxInstance"* ]]; then
                printf '%s\\n' "$body" > {flux_stdin_path}
                echo "applied fluxinstance" >> {log}
                echo "fluxinstance.fluxcd.controlplane.io/flux created"
              elif [[ "$body" == *"kind: GitRepository"* ]]; then
                printf '%s\\n' "$body" > {sync_stdin_path}
                echo "applied sync" >> {log}
              else
                printf '%s\\n' "$body" > {stdin_path}
                echo "applied hold" >> {log}
                echo "configmap/backup-rebuild-hold created"
              fi
              exit 0
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Fresh cluster
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    extra = [
        *_flux_operator_extra(binary),
        "-e",
        f"backup_path={tmp_path / 'backup'}",
        "-e",
        f"ansible_become_exe={_become_exe(tmp_path)}",
        "-e",
        f"flux_instance_file={env_file}",
        "-e",
        f"flux_sync_file={sync_file}",
    ]
    proc = _run_localhost_role(
        play,
        tmp_path,
        extra,
        env=env,
        inventory=["localhost,", str(_backup_inventory(tmp_path))],
    )
    output = proc.stdout + proc.stderr
    # changed_when used to fail the play here, before the FluxInstance apply.
    assert "'stdout' is undefined" not in output
    assert "Apply the rebuild hold" in output
    rendered = stdin_path.read_text(encoding="utf-8")
    assert "ns-demo/pvc-drill" in rendered
    assert "ns-decoy" not in rendered
    assert proc.returncode == 0, output
    invocations = log.read_text(encoding="utf-8").splitlines()
    assert (
        invocations.index("applied hold")
        < invocations.index("applied fluxinstance")
        < invocations.index("applied sync")
    )
    applied = flux_stdin_path.read_text(encoding="utf-8")
    assert applied.strip() == env_file.read_text(encoding="utf-8").strip()
    assert sync_stdin_path.read_text(encoding="utf-8").strip() == sync_file.read_text(encoding="utf-8").strip()


def test_root_source_lookup_error_does_not_set_the_hold(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            echo "The connection to the server 127.0.0.1:6443 was refused" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: API error
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    proc = _run_localhost_role(
        play,
        tmp_path,
        ["-e", f"k3s_binary_path={binary}"],
        env=env,
        inventory=["localhost,", str(_backup_inventory(tmp_path))],
    )
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "Check whether the root GitRepository exists" in output
    assert "Stat restic data on the backup host" not in output
    invocations = log.read_text(encoding="utf-8")
    assert "backup-rebuild-hold" not in invocations
    assert "apply" not in invocations


_DEPLOY_KEY_PLAY = textwrap.dedent(
    """\
    ---
    - name: Deploy key
      hosts: localhost
      gather_facts: false
      become: false
      tasks:
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age_key_file.yml
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: deploy_key.yml
    """
)


def test_fresh_cluster_creates_the_deploy_key_secret_once(tmp_path: Path) -> None:
    """An ssh:// root's sealed deploy key becomes flux-system/flux-deploy-key once, with GitHub's host key."""
    require_cli("ansible-playbook")
    require_cli("sops")
    from jarvis.secrets.sops import encrypt_secret_to_file

    identity, public = _age_identity(tmp_path, "env")
    keyfile = _key_file(tmp_path, (identity, public))
    instance = tmp_path / "instance"
    (instance / "envs" / _SOPS_ENV).mkdir(parents=True)
    _sops_config(tmp_path, _SOPS_ENV, public).rename(instance / ".sops.yaml")
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
    encrypt_secret_to_file(
        repo_root=instance, env=_SOPS_ENV, name="flux-deploy-key", sops_bin="sops",
        document={"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "flux-deploy-key", "namespace": "flux-system"},
                  "stringData": {"identity": private}},
    )
    sealed = instance / "envs" / _SOPS_ENV / "secrets" / "flux-deploy-key.sops.yaml"
    log, created, present = tmp_path / "k3s.log", tmp_path / "created.yaml", tmp_path / "present"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'joined="$*"\n'
        'if [[ "$joined" == *"get secret flux-deploy-key -o name"* ]]; then\n'
        f"  if [[ -e {present} ]]; then echo secret/flux-deploy-key; exit 0; fi\n"
        "  echo 'Error from server (NotFound): secrets \"flux-deploy-key\" not found' >&2; exit 1\n"
        "fi\n"
        f'if [[ "$joined" == *"kubectl create -f -"* ]]; then cat > {created}; touch {present}; exit 0; fi\n'
        'echo "unexpected k3s invocation: $*" >&2\nexit 1\n',
    )
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    extra = ["-e", f"k3s_binary_path={binary}", "-e", f"flux_deploy_key_file={sealed}"]
    for _ in range(2):
        proc = _run_localhost_role(_DEPLOY_KEY_PLAY, tmp_path, extra, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert private not in proc.stdout + proc.stderr
    assert log.read_text(encoding="utf-8").count("kubectl create -f -") == 1
    secret = yaml.safe_load(created.read_text(encoding="utf-8"))
    assert (secret["metadata"]["namespace"], secret["metadata"]["name"]) == ("flux-system", "flux-deploy-key")
    assert secret["metadata"]["annotations"] == {"kustomize.toolkit.fluxcd.io/reconcile": "disabled"}
    assert secret["stringData"] == {
        "identity": private,
        "known_hosts": load_role_defaults(FLUX_OPERATOR_ROLE_DIR)["flux_known_hosts"],
    }


def test_known_hosts_is_a_github_published_host_key() -> None:
    """flux_known_hosts pins a key GitHub publishes (api.github.com/meta), so Flux trusts github.com only."""
    import urllib.error
    import urllib.request

    # Unauthenticated calls share the runner's address limit; CI passes the workflow's read-only token.
    request = urllib.request.Request("https://api.github.com/meta")
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            published = json.load(response)["ssh_keys"]
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise
        pytest.skip(f"api.github.com answers {error.code}; GitHub is down, not the pin")
    host, key = load_role_defaults(FLUX_OPERATOR_ROLE_DIR)["flux_known_hosts"].split(" ", 1)
    assert host == "github.com"
    assert key in published


def _pinned_helm_version() -> str:
    return str(load_role_defaults(FLUX_OPERATOR_ROLE_DIR)["helm_version"])


def _fake_helm(log: Path, version: str) -> str:
    """A helm that reports version like `helm version --short` and logs every call."""
    return (
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        f'if [[ "$1" == version ]]; then echo "v{version}+gfixture"; fi\n'
        "exit 0\n"
    )


def _helm_archive(tmp_path: Path, binary: str) -> tuple[Path, str]:
    """A release-shaped archive holding linux-amd64/helm, and its sha256."""
    path = tmp_path / "helm.tar.gz"
    data = binary.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("linux-amd64/helm")
        member.size = len(data)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_helm_cli_with_another_version_is_replaced_then_left_alone(tmp_path: Path) -> None:
    """A helm reporting another version is replaced from the pinned archive; the next run downloads nothing."""
    require_cli("ansible-playbook")
    log = tmp_path / "helm.log"
    installed = tmp_path / "bin" / "helm"
    installed.parent.mkdir()
    _write_executable(installed, _fake_helm(log, "3.0.0"))
    pinned = _fake_helm(log, _pinned_helm_version())
    archive, digest = _helm_archive(tmp_path, pinned)
    play = textwrap.dedent(
        """\
        ---
        - name: Helm CLI
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: helm_cli.yml
        """
    )

    def run(url: str) -> subprocess.CompletedProcess[str]:
        return _run_localhost_role(
            play,
            tmp_path,
            [
                "-e", f"helm_binary_path={installed}",
                "-e", f"helm_download_url={url}",
                "-e", f"helm_linux_amd64_tarball_sha256={digest}",
                # Defaults are root:root. This play does not become root.
                "-e", f"helm_binary_owner={getpass.getuser()}",
                "-e", f"helm_binary_group={grp.getgrgid(os.getgid()).gr_name}",
            ],
            env=_ansible_env(),
        )

    first = run(archive.as_uri())
    assert first.returncode == 0, first.stdout + first.stderr
    assert installed.read_text(encoding="utf-8") == pinned
    # The archive URL now points at nothing, so a download would fail the run.
    second = run((tmp_path / "absent.tar.gz").as_uri())
    output = second.stdout + second.stderr
    assert second.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0


def _lookup_binary(log: Path, *, kind: str) -> str:
    """kind is not_found or error. The absent case prints NotFound; any other error does not."""
    if kind == "not_found":
        message = "echo 'Error from server (NotFound): missing' >&2"
    else:
        message = 'echo "The connection to the server 127.0.0.1:6443 was refused" >&2'
    return textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail
        printf '%s\\n' "$*" >> {log}
        joined="$*"
        if [[ "$joined" == *"get deployment flux-operator"* || "$joined" == *"get secret sops-age -o name"* ]]; then
          {message}
          exit 1
        fi
        if [[ "$joined" == *" wait "* ]]; then
          exit 0
        fi
        if [[ "$joined" == *"kubectl create"* ]]; then
          cat > /dev/null
          exit 0
        fi
        echo "unexpected k3s invocation: $*" >&2
        exit 1
        """
    )


@pytest.mark.parametrize("kind", ["not_found", "error"])
def test_operator_lookup_classifies_not_found(tmp_path: Path, kind: str) -> None:
    """NotFound installs the operator chart. Any other lookup error does not."""
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    helm_log = tmp_path / "helm.log"
    binary = tmp_path / "k3s"
    helm = tmp_path / "helm"
    _write_executable(binary, _lookup_binary(log, kind=kind))
    if kind == "not_found":
        _write_executable(helm, _fake_helm(helm_log, _pinned_helm_version()))
    else:
        _write_executable(helm, "#!/bin/bash\nexit 99\n")
    proc = _run_localhost_role(
        textwrap.dedent(
            """\
            ---
            - name: Operator lookup
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: operator.yml
            """
        ),
        tmp_path,
        [
            "-e",
            f"k3s_binary_path={binary}",
            "-e",
            f"helm_binary_path={helm}",
            "-e",
            "flux_operator_chart_version=0.0.0-test",
            "-e",
            f"k3s_kubeconfig_path={tmp_path / 'kubeconfig'}",
        ],
        env=_ansible_env(),
    )
    output = proc.stdout + proc.stderr
    k3s_text = log.read_text(encoding="utf-8")
    if kind == "not_found":
        assert proc.returncode == 0, output
        assert "upgrade --install" in helm_log.read_text(encoding="utf-8")
        assert "kubectl wait" in k3s_text
        return
    assert proc.returncode != 0, output
    assert "Check whether Flux Operator deployment exists" in output
    assert "Bootstrap Flux Operator when absent" not in output
    assert not helm_log.exists()
    assert "wait" not in k3s_text


def test_sops_age_lookup_error_does_not_bootstrap(tmp_path: Path) -> None:
    """An API error is not NotFound, so the sops-age secret is not created."""
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o600)
    _write_executable(binary, _lookup_binary(log, kind="error"))
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    proc = _run_localhost_role(_SOPS_PLAY, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Check whether flux-system sops-age secret exists" in output
    assert "Create flux-system sops-age secret once" not in output
    assert "Stat the age private key file" not in output
    assert sentinel not in output
    assert "create" not in log.read_text(encoding="utf-8")



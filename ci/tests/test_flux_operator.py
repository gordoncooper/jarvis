"""flux-operator Ansible role: template rules and check-mode proof (p0a-13)."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    example_flux_entry,
    load_cluster_vars,
    playbook_recap_field,
)
from conftest import require_cli
from schema.env import DEFAULT_SSH_USER

SITE_YML = ANSIBLE_DIR / "site.yml"


def test_sops_age_secret_template_not_reconciled_from_git() -> None:
    key_body = "AGE-SECRET-KEY-1TEST\n"
    rendered = ansible_render_template(
        "sops-age-secret.yaml.j2",
        {
            "flux_system_namespace": "flux-system",
            "flux_sops_age_key_content": key_body,
        },
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    )
    assert "name: sops-age" in rendered
    assert "namespace: flux-system" in rendered
    assert "kustomize.toolkit.fluxcd.io/reconcile: disabled" in rendered


_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)
_TASK_KEYS = (
    "ansible.builtin.import_tasks",
    "import_tasks",
    "ansible.builtin.include_tasks",
    "include_tasks",
)
_READ_ONLY_MODULES = {
    "ansible.builtin.assert",
    "assert",
    "ansible.builtin.debug",
    "debug",
    "ansible.builtin.fail",
    "fail",
    "ansible.builtin.setup",
    "setup",
    "ansible.builtin.set_fact",
    "set_fact",
    "ansible.builtin.stat",
    "stat",
    "ansible.builtin.slurp",
    "slurp",
    "ansible.builtin.find",
    "find",
    "ansible.builtin.command",
    "command",
    "ansible.builtin.shell",
    "shell",
    *_ROLE_KEYS,
    *_TASK_KEYS,
}
_NON_MODULE_KEYS = {
    "name",
    "when",
    "vars",
    "tags",
    "become",
    "register",
    "changed_when",
    "failed_when",
    "check_mode",
    "args",
    "loop",
    "loop_control",
    "notify",
    "ignore_errors",
    "any_errors_fatal",
    "delegate_to",
    "run_once",
    "environment",
    "no_log",
    "timeout",
    "block",
    "rescue",
    "always",
    "listen",
    "retries",
    "delay",
    "until",
    "diff",
    "module_defaults",
    "ignore_unreachable",
}


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


def _k3s_modes(play: dict[str, object]) -> list[str]:
    modes: list[str] = []
    for role in play.get("roles") or []:
        if isinstance(role, dict) and role.get("role") == "k3s":
            mode = (role.get("vars") or {}).get("k3s_mode")
            if isinstance(mode, str):
                modes.append(mode)
    for task in play.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        for key in _ROLE_KEYS:
            included = task.get(key)
            if isinstance(included, dict) and included.get("name") == "k3s":
                mode = (included.get("vars") or {}).get("k3s_mode")
                if isinstance(mode, str):
                    modes.append(mode)
    return modes


def _is_fact_play(play: dict[str, object]) -> bool:
    tags = play.get("tags") or []
    return (
        play.get("hosts") == "all"
        and isinstance(tags, list)
        and "always" in tags
        and play.get("any_errors_fatal") is True
    )


def _imported_task_file(task: dict[str, object], module: str) -> Path | None:
    if module in _TASK_KEYS:
        raw = task[module]
        file = raw if isinstance(raw, str) else raw.get("file") if isinstance(raw, dict) else None
        if not isinstance(file, str):
            return None
        return ANSIBLE_DIR / file
    if module in _ROLE_KEYS:
        raw = task[module]
        if isinstance(raw, str):
            name, tasks_from = raw, "main.yml"
        elif isinstance(raw, dict) and isinstance(raw.get("name"), str):
            name = raw["name"]
            tasks_from = raw.get("tasks_from") or "main.yml"
            if not isinstance(tasks_from, str):
                return None
        else:
            return None
        if not tasks_from.endswith(".yml"):
            tasks_from = f"{tasks_from}.yml"
        return ANSIBLE_DIR / "roles" / name / "tasks" / tasks_from
    return None


def _task_modules(task: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    found: list[tuple[str, dict[str, object]]] = []
    for key, value in task.items():
        if key in {"block", "rescue", "always"} and isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    found.extend(_task_modules(child))
            continue
        if key in _NON_MODULE_KEYS:
            continue
        found.append((str(key), task))
    return found


def _read_only_errors(tasks: object, seen: set[Path]) -> list[str]:
    if not isinstance(tasks, list):
        return ["fact-assert play tasks are not a list"]
    errors: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("fact-assert play has a task that is not a mapping")
            continue
        for module, owner in _task_modules(task):
            if module not in _READ_ONLY_MODULES:
                errors.append(f"fact-assert play runs {module}")
                continue
            if module in {"ansible.builtin.command", "command", "ansible.builtin.shell", "shell"}:
                if owner.get("changed_when") is not False:
                    errors.append(f"{module} in the fact-assert play can report a change")
            imported = _imported_task_file(owner, module)
            if imported is None:
                continue
            if imported in seen:
                continue
            seen.add(imported)
            if not imported.is_file():
                errors.append(f"fact-assert play imports missing {imported.name}")
                continue
            loaded = yaml.safe_load(imported.read_text(encoding="utf-8"))
            errors.extend(_read_only_errors(loaded, seen))
    return errors


def _operator_play_tags() -> set[str]:
    """Play tags the Ansible README also gives as operator ``--tags`` commands."""
    readme = (ANSIBLE_DIR / "README.md").read_text(encoding="utf-8")
    commands = set(re.findall(r"--tags\s+([A-Za-z][A-Za-z0-9_-]*)", readme))
    named: set[str] = set()
    for line in readme.splitlines():
        if "are play tags" not in line:
            continue
        head = line.split("are play tags", 1)[0]
        named.update(re.findall(r"`([A-Za-z][A-Za-z0-9_-]*)`", head))
    assert named, "README names no play tags"
    missing = named - commands
    assert not missing, f"README play tags have no --tags command: {sorted(missing)}"
    return named


def _assert_play_order(plays: list[dict[str, object]]) -> None:
    """Play order, host targets, and documented play tags are the safety fact."""
    fact_indexes = [index for index, play in enumerate(plays) if _is_fact_play(play)]
    assert fact_indexes == [0], plays
    fact = plays[0]
    assert not fact.get("roles"), "fact-assert play applies a role"
    seen: set[Path] = set()
    errors = _read_only_errors(fact.get("tasks"), seen)
    for section in ("pre_tasks", "post_tasks", "handlers"):
        body = fact.get(section)
        if body is None:
            continue
        errors.extend(_read_only_errors(body, seen))
    assert errors == []

    def indexes(role: str) -> list[int]:
        return [
            index
            for index, play in enumerate(plays)
            if index != 0 and role in _role_names(play)
        ]

    base_os = indexes("base-os")
    k3s = indexes("k3s")
    kubeconfig = indexes("kubeconfig")
    flux = indexes("flux-operator")
    server = [index for index, play in enumerate(plays) if "server" in _k3s_modes(play)]
    agent = [index for index, play in enumerate(plays) if "agent" in _k3s_modes(play)]
    assert base_os, "base-os play is missing"
    assert k3s, "k3s play is missing"
    assert len(server) == 1, server
    assert len(agent) == 1, agent
    assert kubeconfig, "kubeconfig play is missing"
    assert flux, "flux-operator play is missing"
    assert plays[server[0]].get("hosts") == "control_plane"
    assert plays[agent[0]].get("hosts") == "all:!control_plane"
    assert all(plays[index].get("hosts") == "control_plane" for index in kubeconfig)
    assert all(plays[index].get("hosts") == "control_plane" for index in flux)
    for tag in sorted(_operator_play_tags()):
        found = set(indexes(tag))
        if tag == "k3s":
            found.update(server)
            found.update(agent)
        assert found, tag
        for index in sorted(found):
            tags = plays[index].get("tags")
            assert isinstance(tags, list) and tag in tags, (plays[index].get("name"), tag)
    assert all(index < min(k3s) for index in base_os)
    assert server[0] < agent[0]
    assert all(index > server[0] for index in kubeconfig)
    assert all(index > server[0] for index in flux)

    safety = [fact, *[plays[index] for index in sorted({*base_os, *k3s, *kubeconfig, *flux})]]
    shared = fact.get("vars")
    for play in safety:
        assert play.get("become") is True
        assert "ssh_user | default(" in str((play.get("vars") or {}).get("ansible_user"))
        assert play.get("vars") == shared


def _site_plays() -> list[dict[str, object]]:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def test_site_play_order_is_the_safety_fact() -> None:
    """Fact-assert play first and read-only; base-os before k3s; server before agent; kubeconfig and flux-operator after the server, on control_plane; the agent on every host except control_plane."""
    _assert_play_order(_site_plays())


def test_play_before_the_fact_assert_play_fails() -> None:
    """A play inserted before the fact-assert play is not the required order."""
    plays = _site_plays()
    plays.insert(0, {"name": "early", "hosts": "all", "tasks": [{"ansible.builtin.debug": {"msg": "x"}}]})
    with pytest.raises(AssertionError):
        _assert_play_order(plays)


def test_copy_before_tasks_fails() -> None:
    """A copy task in the fact-assert play's pre_tasks is a change."""
    plays = _site_plays()
    plays[0]["pre_tasks"] = [
        {"ansible.builtin.copy": {"dest": "/tmp/x", "content": "x"}}
    ]
    with pytest.raises(AssertionError):
        _assert_play_order(plays)


def test_dropping_a_documented_tag_fails() -> None:
    """A k3s play without the tag from the operator commands is refused."""
    plays = _site_plays()
    server = next(play for play in plays if "server" in _k3s_modes(play))
    server["tags"] = [tag for tag in server.get("tags") or [] if tag != "k3s"]
    with pytest.raises(AssertionError):
        _assert_play_order(plays)


def test_unrelated_play_at_the_end_passes() -> None:
    """A play that is not part of the safety order does not fail it."""
    plays = _site_plays()
    plays.append(
        {
            "name": "notes",
            "hosts": "localhost",
            "tasks": [{"ansible.builtin.debug": {"msg": "x"}}],
        }
    )
    _assert_play_order(plays)


def test_site_yml_ssh_user_default_matches_schema() -> None:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    expr = plays[0]["vars"]["ansible_user"]
    literal = expr.split("default(", 1)[1].split(")", 1)[0].strip("\"'")
    assert literal == DEFAULT_SSH_USER


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
        "flux_sops_age_existing_key_content",
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
              *"get fluxinstance flux"*)
                echo "fluxinstance.fluxcd.controlplane.io/flux"
                exit 0
                ;;
              *"get secret sops-age"*)
                echo "secret/sops-age"
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


def test_sops_age_secret_task_uses_kubectl_create_not_apply(tmp_path: Path) -> None:
    """NotFound on flux-system/sops-age takes the bootstrap path (kubectl create)."""
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    keyfile = tmp_path / "age.key"
    keyfile.write_text("fixture-age-key\n", encoding="utf-8")
    keyfile.chmod(0o600)
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
            if [[ "$joined" == *"kubectl create"* ]]; then
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
        - name: Create sops-age secret
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
    assert proc.returncode == 0, proc.stdout + proc.stderr
    invocations = log.read_text(encoding="utf-8")
    assert "kubectl create" in invocations
    assert "kubectl apply" not in invocations


def _assert_key_was_not_read(proc: subprocess.CompletedProcess[str], sentinel: str) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Refuse an age private key" in output
    assert sentinel not in output
    assert "Load age private key from the controller" not in output


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


def test_rerun_with_fluxinstance_present_does_not_set_rebuild_hold(tmp_path: Path) -> None:
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
            if [[ "$joined" == *"get fluxinstance"* ]]; then
              echo "fluxinstance.fluxcd.controlplane.io/flux"
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
        - name: FluxInstance already present
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
    assert invocations.count("get fluxinstance") == 2
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


def test_fresh_cluster_sets_the_hold_then_applies_the_env_flux_instance(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    stdin_path = tmp_path / "apply.stdin"
    flux_stdin_path = tmp_path / "flux-apply.stdin"
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
            if [[ "$joined" == *"get fluxinstance"* ]]; then
              echo 'Error from server (NotFound): fluxinstances.fluxcd.controlplane.io "flux" not found' >&2
              exit 1
            fi
            if [[ "$joined" == *"kubectl apply -f -"* ]]; then
              body="$(cat)"
              if [[ "$body" == *"kind: FluxInstance"* ]]; then
                printf '%s\\n' "$body" > {flux_stdin_path}
                echo "applied fluxinstance" >> {log}
                echo "fluxinstance.fluxcd.controlplane.io/flux created"
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
    assert invocations.index("applied hold") < invocations.index("applied fluxinstance")
    applied = flux_stdin_path.read_text(encoding="utf-8")
    assert applied.strip() == env_file.read_text(encoding="utf-8").strip()


def test_fluxinstance_lookup_error_does_not_set_the_hold(tmp_path: Path) -> None:
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
    assert "Check whether FluxInstance exists" in output
    assert "Stat restic data on the backup host" not in output
    invocations = log.read_text(encoding="utf-8")
    assert "backup-rebuild-hold" not in invocations
    assert "apply" not in invocations


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
        if [[ "$joined" == *" wait "* || "$joined" == *"kubectl create"* ]]; then
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
        _write_executable(
            helm,
            textwrap.dedent(
                f"""\
                #!/bin/bash
                printf '%s\\n' "$*" >> {helm_log}
                exit 0
                """
            ),
        )
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
            "-e",
            '{"ansible_facts":{"architecture":"x86_64"}}',
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
    play = textwrap.dedent(
        """\
        ---
        - name: sops-age lookup error
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
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Check whether flux-system sops-age secret exists" in output
    assert "Create flux-system sops-age secret once" not in output
    assert "Stat the age private key file" not in output
    assert sentinel not in output
    assert "create" not in log.read_text(encoding="utf-8")

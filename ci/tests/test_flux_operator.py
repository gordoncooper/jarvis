"""flux-operator Ansible role: template rules and check-mode proof."""

from __future__ import annotations

import base64
import functools
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
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    env_name,
    example_flux_entry,
    load_cluster_vars,
    load_role_defaults,
    playbook_recap_field,
)
from conftest import require_cli
from schema.env import DEFAULT_SSH_USER
from support import REPO_ROOT

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


_TAG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_PLAY_TAG_LINE = re.compile(r"^  play #\d+ \([^)]*\):.*\tTAGS: \[(.*)\]\s*$")
_TASK_TAG_LINE = re.compile(r"^      TASK TAGS: \[(.*)\]\s*$")
_LIST_TAGS_INVENTORY = REPO_ROOT / "ci/tests/fixtures/ansible-list-tasks/hosts.yml"
_TAGS_MARK = "--tags"
_MAKE_TAGS_MARK = "TAGS="


def _tag_list(body: str) -> set[str]:
    return {part.strip() for part in body.split(",") if part.strip()}


def _skip_tag_gap(text: str, index: int) -> int:
    """Spaces, newlines, and backslash continuations between tag tokens."""
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char != "\\":
            break
        nxt = index + 1
        while nxt < length and text[nxt] in " \t":
            nxt += 1
        if nxt < length and text[nxt] == "\n":
            index = nxt + 1
            continue
        if nxt < length and text[nxt] == "\r":
            index = nxt + 2 if nxt + 1 < length and text[nxt + 1] == "\n" else nxt + 1
            continue
        break
    return index


def _parse_tags_argument(text: str, index: int) -> tuple[set[str], int] | None:
    """Names after ``--tags``, or None when this occurrence is not a command."""
    length = len(text)
    if index < length and (text[index].isalnum() or text[index] in "-_"):
        return None
    index = _skip_tag_gap(text, index)
    if index < length and text[index] == "=":
        index = _skip_tag_gap(text, index + 1)
    quote = ""
    if index < length and text[index] in "\"'":
        quote = text[index]
        index = _skip_tag_gap(text, index + 1)
    match = _TAG_NAME.match(text, index)
    if match is None:
        return None
    names = [match.group(0)]
    index = match.end()
    while True:
        if quote:
            gap = _skip_tag_gap(text, index)
            if gap < length and text[gap] == ",":
                index = _skip_tag_gap(text, gap + 1)
                match = _TAG_NAME.match(text, index)
                if match is None:
                    return None
                names.append(match.group(0))
                index = match.end()
                continue
            if gap < length and text[gap] == quote:
                index = gap + 1
                break
            return None
        gap = index
        while gap < length and text[gap] in " \t":
            gap += 1
        if gap < length and text[gap] == ",":
            index = _skip_tag_gap(text, gap + 1)
            match = _TAG_NAME.match(text, index)
            if match is None:
                break
            names.append(match.group(0))
            index = match.end()
            continue
        break
    return set(names), index


def _operator_play_tags(readme: str | None = None) -> set[str]:
    """Every ``--tags`` or ``TAGS=`` name the Ansible README documents.

    A comma list may wrap across lines or a backslash continuation. ``=`` and
    quotes are part of the command. A backtick after ``--tags`` is not a command.
    """
    if readme is None:
        readme = (ANSIBLE_DIR / "README.md").read_text(encoding="utf-8")
    names: set[str] = set()
    index = 0
    while True:
        found_flag = readme.find(_TAGS_MARK, index)
        found_make = readme.find(_MAKE_TAGS_MARK, index)
        found = min((pos for pos in (found_flag, found_make) if pos >= 0), default=-1)
        if found < 0:
            break
        mark = _MAKE_TAGS_MARK if found == found_make else _TAGS_MARK
        parsed = _parse_tags_argument(readme, found + len(mark.rstrip("=")))
        if parsed is None:
            index = found + len(mark)
            continue
        parsed_names, index = parsed
        names.update(parsed_names)
    assert names, "README documents no --tags names"
    return names


def _tags_from_list_output(output: str) -> tuple[set[str], set[str]]:
    """All tags, and the tags written on a task rather than inherited from the play."""
    listed: set[str] = set()
    task_tags: set[str] = set()
    play_tags: set[str] = set()
    for line in output.splitlines():
        play = _PLAY_TAG_LINE.match(line)
        if play:
            play_tags = _tag_list(play.group(1))
            listed.update(play_tags)
            continue
        task = _TASK_TAG_LINE.match(line)
        if task:
            tags = _tag_list(task.group(1))
            listed.update(tags)
            task_tags.update(tags - play_tags)
    return listed, task_tags


@functools.lru_cache(maxsize=1)
def _listed_tag_sets() -> tuple[frozenset[str], frozenset[str]]:
    """Tags from ``ansible-playbook site.yml --list-tags``, and those set on a task."""
    require_cli("ansible-playbook")
    run = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "-i",
            str(_LIST_TAGS_INVENTORY),
            "-e",
            f"jarvis_cluster_vars=../../envs/{env_name()}/values/cluster-vars.yaml",
            "--list-tags",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    listed, task_tags = _tags_from_list_output(run.stdout)
    assert listed, run.stdout
    return frozenset(listed), frozenset(task_tags)


def _assert_play_order(
    plays: list[dict[str, object]],
    *,
    readme: str | None = None,
) -> None:
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
    documented = _operator_play_tags(readme)
    listed, task_tags = _listed_tag_sets()
    missing = sorted(documented - listed)
    assert not missing, f"README --tags names are not playbook tags: {missing}"
    for tag in sorted(documented - task_tags):
        found = set(indexes(tag))
        if tag == "k3s":
            found.update(server)
            found.update(agent)
        if tag == "labels":
            found.update(index for index in k3s if plays[index].get("tasks"))
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


def test_readme_tag_with_no_matching_playbook_tag_fails() -> None:
    """A README --tags name that site.yml does not list fails."""
    readme = (ANSIBLE_DIR / "README.md").read_text(encoding="utf-8")
    readme += "\nansible-playbook site.yml --tags not-a-real-tag\n"
    with pytest.raises(AssertionError, match="not-a-real-tag"):
        _assert_play_order(_site_plays(), readme=readme)


@pytest.mark.parametrize(
    ("readme", "single"),
    [
        (
            "ansible-playbook site.yml --tags time,\njournald\n",
            "ansible-playbook site.yml --tags time,journald\n",
        ),
        (
            "ansible-playbook site.yml --tags time,\\\njournald\n",
            "ansible-playbook site.yml --tags time,journald\n",
        ),
        (
            "ansible-playbook site.yml --tags\ntime\n",
            "ansible-playbook site.yml --tags time\n",
        ),
        (
            "ansible-playbook site.yml --tags=time\n",
            "ansible-playbook site.yml --tags time\n",
        ),
        (
            'ansible-playbook site.yml --tags "time,journald"\n',
            "ansible-playbook site.yml --tags time,journald\n",
        ),
    ],
)
def test_wrapped_tag_command_matches_the_single_line_form(readme: str, single: str) -> None:
    """A wrapped, continued, assigned, or quoted --tags command yields the one-line names."""
    assert _operator_play_tags(readme) == _operator_play_tags(single)


def test_readme_with_no_tags_commands_fails() -> None:
    """A README that documents no --tags names fails instead of checking nothing."""
    with pytest.raises(AssertionError, match="no --tags"):
        _operator_play_tags("Narrow runs are described here without commands.\n")


def test_comma_separated_tags_commands_are_each_a_name() -> None:
    """A documented --tags list yields every name, including a comma-separated list."""
    readme = (
        "ansible-playbook site.yml --tags time,journald\n"
        "ansible-playbook site.yml --tags apt-policy, resolved\n"
    )
    assert _operator_play_tags(readme) == {"time", "journald", "apt-policy", "resolved"}


def test_play_tag_inherited_by_tasks_is_not_a_task_tag() -> None:
    """A tag that appears on tasks only because the play has it is not set on a task."""
    output = (
        "\n"
        "  play #1 (all): Base OS\tTAGS: []\n"
        "      TASK TAGS: [base-os, time]\n"
        "\n"
        "  play #2 (control_plane): Install k3s server\tTAGS: [k3s]\n"
        "      TASK TAGS: [k3s]\n"
    )
    listed, task_tags = _tags_from_list_output(output)
    assert listed == {"base-os", "time", "k3s"}
    assert task_tags == {"base-os", "time"}


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
    assert f"default('{DEFAULT_SSH_USER}')" in expr or f'default("{DEFAULT_SSH_USER}")' in expr


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


def test_age_keygen_check_runs_only_when_the_key_file_exists() -> None:
    """The visible check is skipped when there is no key file to read."""
    names = {
        "Check whether age-keygen is on PATH",
        "Require age-keygen on the controller",
    }
    tasks = [task for task in _task_documents() if task.get("name") in names]
    assert {task["name"] for task in tasks} == names
    for task in tasks:
        assert task.get("when") == "sops_age_key_stat.stat.exists | default(false)"
        assert task.get("no_log") is not True


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
            if entry.name == "age-keygen" or dest.exists():
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
    sync_file = REPO_ROOT / "envs" / env_name() / "flux" / "sync.yaml"
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
    from _sops import encrypt_secret_to_file

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

    try:
        with urllib.request.urlopen("https://api.github.com/meta", timeout=30) as response:
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

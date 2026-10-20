"""Operator Ansible tags: list-tasks expectations and per-role tag coverage."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ansible_role_proof import ANSIBLE_DIR, REPO_ROOT, env_name

LIST_TASKS_INVENTORY = (
    REPO_ROOT / "tools/tests/fixtures/ansible-list-tasks/hosts.yml"
)
CLUSTER_VARS_REL = f"../../envs/{env_name()}/values/cluster-vars.yaml"

OPERATOR_TAGS = (
    "base-os",
    "kubeconfig",
    "k3s-upgrade",
    "k3s-config",
    "time",
    "apt-policy",
    "journald",
)

ROLE_TAGS = frozenset({"base-os", "k3s", "flux-operator"})

GUARD_TASKS = frozenset(
    {
        "k3s : Require jarvis_env from cluster-vars",
        "k3s : Probe whether k3s service is active",
        "k3s : Stat k3s binary",
        "k3s : Stat k3s data directory",
        "k3s : Determine whether k3s is present on the host",
        "k3s : Stat jarvis env marker",
        "k3s : Read jarvis env marker",
        "k3s : Refuse host with foreign or unmarked k3s",
        "k3s : Ensure jarvis marker directory exists",
        "k3s : Write jarvis env marker",
    }
)

REQUIRED_VAR_ASSERTS = frozenset(
    {
        "base-os : Require time_zone from cluster-vars",
        "k3s : Require k3s cluster-vars",
        "flux-operator : Require flux-operator cluster-vars",
    }
)

EXPECTED_TASKS_BY_TAG: dict[str, frozenset[str]] = {
    "base-os": frozenset(
        {
            "base-os : Require time_zone from cluster-vars",
            "base-os : Install base OS packages",
            "base-os : Ensure journald drop-in directory exists",
            "base-os : Install journald disk cap",
            "base-os : Install chrony configuration with NTS",
            "base-os : Enable and start chrony",
            "base-os : Configure periodic apt (list refresh only)",
            "base-os : Keep unattended-upgrades package policy inert",
            "base-os : Keep apt-daily timer enabled for package list refresh",
            "base-os : Disable automatic package upgrades timer",
            "base-os : Set node timezone from cluster-vars",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "kubeconfig": frozenset(
        {
            "k3s : Install kubeconfig for the site.yml runner",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "k3s-upgrade": frozenset(
        {
            "k3s : Install k3s server config",
            "k3s : Download pinned k3s binary",
            "k3s : Install k3s systemd unit",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "k3s-config": frozenset(
        {
            "k3s : Install k3s server config",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "time": frozenset(
        {
            "base-os : Install chrony configuration with NTS",
            "base-os : Enable and start chrony",
            "base-os : Set node timezone from cluster-vars",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "apt-policy": frozenset(
        {
            "base-os : Configure periodic apt (list refresh only)",
            "base-os : Keep unattended-upgrades package policy inert",
            "base-os : Keep apt-daily timer enabled for package list refresh",
            "base-os : Disable automatic package upgrades timer",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
    "journald": frozenset(
        {
            "base-os : Ensure journald drop-in directory exists",
            "base-os : Install journald disk cap",
        }
        | GUARD_TASKS
        | REQUIRED_VAR_ASSERTS
    ),
}

TASK_LINE = re.compile(
    r"^\s+(?P<role>[a-z0-9_-]+) : (?P<task>.+?)\s+TAGS: \[(?P<tags>.*)\]\s*$"
)

ALWAYS_ONLY_BASE_OS = "base-os : Require time_zone from cluster-vars"
ALWAYS_ONLY_FLUX = "flux-operator : Require flux-operator cluster-vars"


def list_tasks_for_tag(tag: str) -> list[str]:
    run = subprocess.run(
        [
            "ansible-playbook",
            "site.yml",
            "-i",
            str(LIST_TASKS_INVENTORY),
            "-e",
            f"jarvis_cluster_vars={CLUSTER_VARS_REL}",
            "--list-tasks",
            "--tags",
            tag,
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    names: list[str] = []
    for line in run.stdout.splitlines():
        match = TASK_LINE.match(line)
        if match:
            names.append(f"{match.group('role')} : {match.group('task')}")
    return names


def parse_tags_list(raw: str) -> frozenset[str]:
    if not raw.strip():
        return frozenset()
    return frozenset(ast.literal_eval(f"[{raw}]"))


@pytest.mark.parametrize("tag", OPERATOR_TAGS)
def test_operator_tag_list_tasks_matches_expected(tag: str) -> None:
    listed = frozenset(list_tasks_for_tag(tag))
    expected = EXPECTED_TASKS_BY_TAG[tag]
    assert listed == expected, f"tag={tag}\nextra={listed - expected}\nmissing={expected - listed}"


@pytest.mark.parametrize("tag", OPERATOR_TAGS)
def test_operator_tag_includes_guard_and_required_asserts(tag: str) -> None:
    listed = frozenset(list_tasks_for_tag(tag))
    assert GUARD_TASKS <= listed, tag
    assert REQUIRED_VAR_ASSERTS <= listed, tag


def test_k3s_upgrade_excludes_untagged_base_os_and_flux_operator_work() -> None:
    listed = list_tasks_for_tag("k3s-upgrade")
    for name in listed:
        if name.startswith("base-os :") and name != ALWAYS_ONLY_BASE_OS:
            pytest.fail(f"unexpected base-os task on k3s-upgrade: {name}")
        if name.startswith("flux-operator :") and name != ALWAYS_ONLY_FLUX:
            pytest.fail(f"unexpected flux-operator task on k3s-upgrade: {name}")


def _merge_tags(
    inherited: frozenset[str], explicit: Any
) -> frozenset[str]:
    if explicit is None:
        return inherited
    if isinstance(explicit, str):
        return inherited | {explicit}
    if isinstance(explicit, list):
        return inherited | frozenset(str(t) for t in explicit)
    return inherited


def _walk_task_entries(
    entries: list[Any],
    *,
    inherited_tags: frozenset[str],
    role: str,
    path: Path,
    tasks_dir: Path,
    found: list[tuple[str, Path, frozenset[str]]],
) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_tags = _merge_tags(inherited_tags, entry.get("tags"))

        if "import_tasks" in entry or "ansible.builtin.import_tasks" in entry:
            rel = entry.get("import_tasks") or entry.get("ansible.builtin.import_tasks")
            assert isinstance(rel, str), entry
            child = tasks_dir / rel
            child_entries = yaml.safe_load(child.read_text(encoding="utf-8"))
            assert isinstance(child_entries, list)
            _walk_task_entries(
                child_entries,
                inherited_tags=entry_tags,
                role=role,
                path=child,
                tasks_dir=tasks_dir,
                found=found,
            )
            continue

        if "include_tasks" in entry or "ansible.builtin.include_tasks" in entry:
            rel = entry.get("include_tasks") or entry.get("ansible.builtin.include_tasks")
            assert isinstance(rel, str), entry
            child = tasks_dir / rel
            child_entries = yaml.safe_load(child.read_text(encoding="utf-8"))
            assert isinstance(child_entries, list)
            _walk_task_entries(
                child_entries,
                inherited_tags=entry_tags,
                role=role,
                path=child,
                tasks_dir=tasks_dir,
                found=found,
            )
            continue

        name = entry.get("name")
        if not isinstance(name, str):
            continue

        for section in ("block", "rescue", "always"):
            block = entry.get(section)
            if isinstance(block, list):
                _walk_task_entries(
                    block,
                    inherited_tags=entry_tags,
                    role=role,
                    path=path,
                    tasks_dir=tasks_dir,
                    found=found,
                )

        found.append((name, path, entry_tags))


def _collect_role_tasks(role: str) -> list[tuple[str, Path, frozenset[str]]]:
    tasks_dir = ANSIBLE_DIR / "roles" / role / "tasks"
    main = tasks_dir / "main.yml"
    entries = yaml.safe_load(main.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    found: list[tuple[str, Path, frozenset[str]]] = []
    _walk_task_entries(
        entries,
        inherited_tags=frozenset(),
        role=role,
        path=main,
        tasks_dir=tasks_dir,
        found=found,
    )
    handlers = ANSIBLE_DIR / "roles" / role / "handlers" / "main.yml"
    if handlers.is_file():
        handler_entries = yaml.safe_load(handlers.read_text(encoding="utf-8"))
        if isinstance(handler_entries, list):
            for entry in handler_entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                tags = _merge_tags(frozenset(), entry.get("tags"))
                found.append((name, handlers, tags))
    return found


@pytest.mark.parametrize("role", sorted(ROLE_TAGS))
def test_every_role_task_carries_a_role_tag(role: str) -> None:
    missing: list[str] = []
    for name, path, tags in _collect_role_tasks(role):
        if not (tags & ROLE_TAGS):
            missing.append(f"{path.relative_to(REPO_ROOT)}: {name} tags={sorted(tags)}")
    assert not missing, "\n".join(missing)


K3S_RESTART_HANDLER = "Restart k3s"
K3S_NOTIFY_TASKS = frozenset(
    {
        "Install k3s server config",
        "Download pinned k3s binary",
        "Install k3s systemd unit",
    }
)


def _task_notify(entry: dict[str, Any]) -> str | list[str] | None:
    notify = entry.get("notify")
    if notify is not None:
        return notify
    for key in entry:
        if key.endswith(".template") or key.endswith(".get_url"):
            module = entry[key]
            if isinstance(module, dict):
                return module.get("notify")
    return None


def test_k3s_upgrade_tasks_notify_restart_handler() -> None:
    main = ANSIBLE_DIR / "roles" / "k3s" / "tasks" / "main.yml"
    entries = yaml.safe_load(main.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    missing: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name not in K3S_NOTIFY_TASKS:
            continue
        notify = _task_notify(entry)
        if notify == K3S_RESTART_HANDLER:
            continue
        if isinstance(notify, list) and K3S_RESTART_HANDLER in notify:
            continue
        missing.append(f"{name}: notify={notify!r}")
    assert not missing, "\n".join(missing)


def test_k3s_restart_handler_carries_operator_tags() -> None:
    handlers = ANSIBLE_DIR / "roles" / "k3s" / "handlers" / "main.yml"
    entries = yaml.safe_load(handlers.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    handler = next(
        (e for e in entries if isinstance(e, dict) and e.get("name") == K3S_RESTART_HANDLER),
        None,
    )
    assert handler is not None
    tags = _merge_tags(frozenset(), handler.get("tags"))
    assert "k3s-upgrade" in tags
    assert "k3s-config" in tags
    module = handler.get("ansible.builtin.systemd")
    assert isinstance(module, dict)
    assert module.get("daemon_reload") is True


def test_no_legacy_k3s_kubeconfig_tag_in_repo() -> None:
    hits = []
    for path in (ANSIBLE_DIR).rglob("*"):
        if path.suffix not in {".yml", ".yaml", ".md", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "k3s-kubeconfig" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, hits

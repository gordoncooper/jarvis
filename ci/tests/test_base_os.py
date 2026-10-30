"""base-os Ansible role: template rules and site syntax (p0a-11)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from conftest import require_cli
from ansible_role_proof import (
    ANSIBLE_DIR,
    ansible_render_template,
    cluster_vars_playbook_path,
    env_inventory_args,
    load_cluster_vars,
    load_role_defaults,
)

ROLE_TEMPLATES = ANSIBLE_DIR / "roles" / "base-os" / "templates"


def test_base_os_templates_encode_manual_upgrade_policy() -> None:
    cluster = load_cluster_vars()
    pools = cluster["ntp_pools"]
    assert isinstance(pools, list)
    chrony = ansible_render_template(
        "chrony.conf.j2", {"ntp_pools": pools}, templates_dir=ROLE_TEMPLATES
    )
    assert " nts" in chrony
    for pool in pools:
        assert f"pool {pool}" in chrony

    journald_cap = load_role_defaults(ANSIBLE_DIR / "roles" / "base-os")[
        "jarvis_journald_system_max_use"
    ]
    journald = ansible_render_template(
        "journald-jarvis.conf.j2",
        {"jarvis_journald_system_max_use": journald_cap},
        templates_dir=ROLE_TEMPLATES,
    )
    assert f"SystemMaxUse={journald_cap}" in journald

    auto_upgrades_path = ROLE_TEMPLATES / "20auto-upgrades.j2"
    assert "departure from framework" not in auto_upgrades_path.read_text(encoding="utf-8")
    auto_upgrades = ansible_render_template(
        "20auto-upgrades.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert 'Unattended-Upgrade "0"' in auto_upgrades

    unattended = ansible_render_template(
        "50unattended-upgrades.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert 'Automatic-Reboot "false"' in unattended


def test_resolv_conf_link_does_not_touch_its_target(tmp_path: Path) -> None:
    """The resolv.conf task manages the symlink. resolved owns the target file."""
    params = _resolv_link_params()
    assert params.get("follow") is False
    for key in ("owner", "group", "mode"):
        assert key not in params

    target = tmp_path / "resolv.conf"
    target.write_text("nameserver upstream\n", encoding="utf-8")
    link = tmp_path / "resolv.conf.link"
    link.symlink_to(target)
    before = target.stat()

    first = _run_file_module(params, src=target, dest=link)
    assert first["changed"] is False
    assert _same_file(target.stat(), before)

    # resolved rewrites the target by rename. A later run must leave that file alone.
    target.unlink()
    target.write_text("nameserver replaced\n", encoding="utf-8")
    replaced = target.stat()
    second = _run_file_module(params, src=target, dest=link)
    assert second["changed"] is False
    assert _same_file(target.stat(), replaced)
    assert os.readlink(link) == str(target)


def _resolv_link_params() -> dict[str, object]:
    block = _edge_resolved_block()["block"]
    links = [
        item["ansible.builtin.file"]
        for item in block
        if isinstance(item, dict)
        and isinstance(item.get("ansible.builtin.file"), dict)
        and item["ansible.builtin.file"].get("state") == "link"
    ]
    assert len(links) == 1
    return links[0]


def _run_file_module(params: dict[str, object], *, src: Path, dest: Path) -> dict[str, object]:
    args = dict(params)
    args["src"] = str(src)
    args["dest"] = str(dest)
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    run = subprocess.run(
        [
            "ansible",
            "localhost",
            "-c",
            "local",
            "-m",
            "ansible.builtin.file",
            "-a",
            json.dumps(args),
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    body = run.stdout.split("=>", 1)[1]
    result = json.loads(body)
    assert isinstance(result, dict)
    return result


def _same_file(after: os.stat_result, before: os.stat_result) -> bool:
    return (
        after.st_ino == before.st_ino
        and after.st_uid == before.st_uid
        and after.st_gid == before.st_gid
        and after.st_mode == before.st_mode
        and after.st_mtime_ns == before.st_mtime_ns
    )


def test_edge_hosts_do_not_use_the_resolved_stub() -> None:
    """Edge host DNS uses resolved's upstream list, not the 127.0.0.53 stub."""
    drop_in = ansible_render_template(
        "resolved-stub.conf.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert "DNSStubListener=no" in drop_in

    matched = _edge_resolved_block()
    notified = [
        item
        for item in matched["block"]
        if isinstance(item, dict) and item.get("notify") == "Restart systemd-resolved"
    ]
    assert len(notified) == 1
    assert notified[0]["ansible.builtin.template"]["src"] == "resolved-stub.conf.j2"
    link_tasks = [
        item
        for item in matched["block"]
        if isinstance(item, dict) and "ansible.builtin.file" in item
    ]
    assert all(item.get("notify") is None for item in link_tasks)

    handlers = yaml.safe_load(
        (ANSIBLE_DIR / "roles" / "base-os" / "handlers" / "main.yml").read_text(encoding="utf-8")
    )
    assert any(
        isinstance(handler, dict)
        and handler.get("name") == "Restart systemd-resolved"
        and handler["ansible.builtin.service"]["name"] == "systemd-resolved"
        for handler in handlers
    )


def _edge_resolved_block() -> dict[str, object]:
    tasks = yaml.safe_load(
        (ANSIBLE_DIR / "roles" / "base-os" / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    blocks = [task for task in tasks if isinstance(task, dict) and "block" in task]
    matched = [
        task
        for task in blocks
        if "edge" in str(task.get("when"))
        and _block_text(task, "resolved-stub.conf.j2")
        and _block_text(task, "/run/systemd/resolve/resolv.conf")
    ]
    assert len(matched) == 1
    return matched[0]


def _block_text(task: dict[str, object], needle: str) -> bool:
    return needle in yaml.safe_dump(task.get("block"))


def test_site_yml_syntax_check() -> None:
    require_cli("ansible-playbook")
    cmd = [
        "ansible-playbook",
        "--syntax-check",
        "site.yml",
        *env_inventory_args(),
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

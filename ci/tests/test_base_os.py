"""base-os Ansible role: template rules and site syntax (p0a-11)."""

from __future__ import annotations

import getpass
import grp
import json
import os
import re
import subprocess
import textwrap
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
    playbook_recap_field,
)

ROLE_TEMPLATES = ANSIBLE_DIR / "roles" / "base-os" / "templates"


def test_base_os_templates_encode_security_only_upgrade_policy() -> None:
    cluster = load_cluster_vars()
    pools = cluster["ntp_pools"]
    assert isinstance(pools, list)
    chrony = ansible_render_template(
        "chrony.conf.j2", {"ntp_pools": pools, "chrony_nts": True}, templates_dir=ROLE_TEMPLATES
    )
    for pool in pools:
        assert f"pool {pool} iburst maxsources 4 nts\n" in chrony

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
    assert 'Unattended-Upgrade "1"' in auto_upgrades

    unattended = ansible_render_template(
        "50unattended-upgrades.j2", {}, templates_dir=ROLE_TEMPLATES
    )
    assert 'Automatic-Reboot "false"' in unattended
    origins = re.search(r"Allowed-Origins \{(.*?)\};\n", unattended, re.DOTALL)
    assert origins is not None
    assert re.findall(r'"([^"]+)";', origins.group(1)) == ["${distro_id}:${distro_codename}-security"]


def test_upgrade_timer_is_enabled() -> None:
    """Security updates need apt-daily-upgrade.timer enabled and started."""
    tasks = yaml.safe_load(
        (ANSIBLE_DIR / "roles" / "base-os" / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    timers = [
        task["ansible.builtin.systemd"]
        for task in tasks
        if isinstance(task, dict)
        and task.get("ansible.builtin.systemd", {}).get("name") == "apt-daily-upgrade.timer"
    ]
    assert len(timers) == 1
    assert timers[0]["enabled"] is True
    assert timers[0]["state"] == "started"


def test_chrony_nts_off_renders_pools_without_nts() -> None:
    """chrony_nts false lets a LAN time source without NTS be used."""
    chrony = ansible_render_template(
        "chrony.conf.j2",
        {"ntp_pools": ["time.example", "lan.example"], "chrony_nts": False},
        templates_dir=ROLE_TEMPLATES,
    )
    assert "pool time.example iburst maxsources 4\n" in chrony
    assert "pool lan.example iburst maxsources 4\n" in chrony
    assert "nts" not in chrony.replace("ntsdumpdir", "")


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
        and handler["ansible.builtin.systemd"]["name"] == "systemd-resolved"
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


def _restarted_units(text: str) -> list[str]:
    units: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if "restart" not in parts:
            continue
        unit = parts[parts.index("restart") + 1]
        units.append(unit.removesuffix(".service"))
    return units


def test_chrony_template_notifies_restart_only_when_changed(tmp_path: Path) -> None:
    """A changed chrony.conf notifies Restart chrony. An unchanged file does not."""
    require_cli("ansible-playbook")
    config_dir = tmp_path / "chrony"
    config_dir.mkdir()
    log = tmp_path / "systemctl.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "chronyd").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "chronyd").chmod(0o755)
    (bin_dir / "systemctl").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys
            from pathlib import Path
            log = Path({str(log)!r})
            with log.open("a", encoding="utf-8") as handle:
                handle.write(" ".join(sys.argv[1:]) + "\\n")
            command = sys.argv[1] if len(sys.argv) > 1 else ""
            if command == "show":
                sys.stdout.write("LoadState=loaded\\nActiveState=active\\n")
                sys.exit(0)
            if command == "restart":
                sys.exit(0)
            sys.stderr.write("unexpected systemctl\\n")
            sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "systemctl").chmod(0o755)
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            f"""\
            ---
            - name: Chrony config
              hosts: localhost
              gather_facts: false
              become: false
              environment:
                PATH: "{bin_dir}:/usr/bin:/bin"
              tasks:
                - ansible.builtin.import_role:
                    name: base-os
                    tasks_from: chrony.yml
            """
        ),
        encoding="utf-8",
    )
    pools = load_cluster_vars()["ntp_pools"]
    extra = json.dumps(
        {
            "chrony_config_dir": str(config_dir),
            # Defaults are root:root. This play does not become root, and the
            # template module chowns before the dest write.
            "chrony_config_owner": getpass.getuser(),
            "chrony_config_group": grp.getgrgid(os.getgid()).gr_name,
            "ntp_pools": pools,
        }
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")

    def run(*, check: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = [
            "ansible-playbook",
            str(play),
            "-i",
            "localhost,",
            "-e",
            "ansible_connection=local",
            "-e",
            extra,
        ]
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

    first = run()
    output = first.stdout + first.stderr
    assert first.returncode == 0, output
    assert "RUNNING HANDLER" in output
    assert "Restart chrony" in output
    written = (config_dir / "chrony.conf").read_text(encoding="utf-8")
    assert " nts" in written
    for pool in pools:
        assert f"pool {pool}" in written
    assert _restarted_units(log.read_text(encoding="utf-8")) == ["chrony"]

    second = run()
    second_out = second.stdout + second.stderr
    assert second.returncode == 0, second_out
    assert playbook_recap_field(second_out, "changed") == 0
    assert "RUNNING HANDLER" not in second_out
    assert _restarted_units(log.read_text(encoding="utf-8")) == ["chrony"]

    checked = run(check=True)
    checked_out = checked.stdout + checked.stderr
    assert checked.returncode == 0, checked_out
    assert playbook_recap_field(checked_out, "changed") == 0
    assert "RUNNING HANDLER" not in checked_out
    assert _restarted_units(log.read_text(encoding="utf-8")) == ["chrony"]


def test_pending_reboot_warns_only_the_host_that_needs_one(tmp_path: Path) -> None:
    """A host with the reboot-required file is named in a warning, also under --check. A host without it is not."""
    require_cli("ansible-playbook")
    flagged = tmp_path / "reboot-required"
    flagged.write_text("", encoding="utf-8")
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            """\
            ---
            - name: Reboot signal
              hosts: all
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: base-os
                    tasks_from: reboot-required.yml
            """
        ),
        encoding="utf-8",
    )
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        yaml.safe_dump(
            {
                "all": {
                    "hosts": {
                        "stale-node": {
                            "ansible_connection": "local",
                            "base_os_reboot_required_path": str(flagged),
                        },
                        "fresh-node": {
                            "ansible_connection": "local",
                            "base_os_reboot_required_path": str(tmp_path / "absent"),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    for extra_args in ([], ["--check"]):
        run = subprocess.run(
            ["ansible-playbook", str(play), "-i", str(inventory), *extra_args],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        output = run.stdout + run.stderr
        assert run.returncode == 0, output
        assert "REBOOT PENDING on stale-node" in output
        assert "REBOOT PENDING on fresh-node" not in output

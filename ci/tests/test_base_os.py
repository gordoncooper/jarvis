"""base-os Ansible role: chrony's NTS switch and restart, the pending-reboot warning, and the edge resolv.conf link."""

from __future__ import annotations

import getpass
import grp
import os
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    LOCALHOST,
    ansible_render_template,
    changed,
    fake_systemctl,
    fake_tool,
    local_play,
    playbook,
    ran,
    role_tasks,
)
from conftest import require_cli

ROLE_DIR = ANSIBLE_DIR / "roles" / "base-os"


def _chrony(nts: bool | str) -> str:
    variables = {"ntp_pools": ["time.example", "lan.example"], "chrony_nts": nts}
    return ansible_render_template("chrony.conf.j2", variables, templates_dir=ROLE_DIR / "templates")


def _pools(chrony: str) -> list[str]:
    return [line for line in chrony.splitlines() if line.startswith("pool ")]


def test_chrony_nts_off_renders_pools_without_nts() -> None:
    """Rule: chrony_nts off, for a LAN time source without NTS, renders the pools without nts, no 4460/tcp note and no
    other NTS mention but ntsdumpdir; the string false (ansible-playbook -e chrony_nts=false) renders exactly as off."""
    on, off, string_off = _chrony(True), _chrony(False), _chrony("false")
    assert _pools(on) == ["pool time.example iburst maxsources 4 nts", "pool lan.example iburst maxsources 4 nts"]
    assert "4460/tcp" in on, on
    assert _pools(off) == ["pool time.example iburst maxsources 4", "pool lan.example iburst maxsources 4"]
    assert "nts" not in off.lower().replace("ntsdumpdir", "") and "4460/tcp" not in off, off
    assert string_off == off


def test_chrony_template_notifies_restart_only_when_changed(tmp_path: Path) -> None:
    """Rule: a changed chrony.conf, written where chronyd reads it, restarts chrony and changes no other unit; a run
    that changes nothing, --check included, calls no systemctl."""
    require_cli("ansible-playbook")
    bin_dir = tmp_path / "bin"
    fake_tool(bin_dir, "chronyd", "#!/bin/sh\nexit 0\n")
    log = fake_systemctl(bin_dir)
    # The template module chowns the file it writes, and the role's root:root would need become.
    owner = {"chrony_config_owner": getpass.getuser(), "chrony_config_group": grp.getgrgid(os.getgid()).gr_name}
    variables = {"chrony_config_dir": str(tmp_path), **owner, "ntp_pools": ["time.example"], "chrony_nts": True}
    play = local_play(tmp_path, role_tasks("base-os", "chrony.yml"), bin_dir=bin_dir, vars=variables)

    first = playbook(play, *LOCALHOST, json=True)
    assert first.returncode == 0, first.output
    assert _pools((tmp_path / "chrony.conf").read_text(encoding="utf-8")) == ["pool time.example iburst maxsources 4 nts"]
    calls = log.read_text(encoding="utf-8").splitlines()
    # show is the systemd module reading the unit's state; every other call changes a unit.
    assert [call for call in calls if not call.startswith("show ")] == ["restart chrony"], calls

    for flags in ((), ("--check",)):
        again = playbook(play, *LOCALHOST, *flags, json=True)
        assert again.returncode == 0 and changed(again) == {"localhost": 0}, again.output
    assert log.read_text(encoding="utf-8").splitlines() == calls


@pytest.mark.parametrize("flags", [pytest.param((), id="apply"), pytest.param(("--check",), id="check")])
def test_pending_reboot_warns_only_the_host_that_needs_one(tmp_path: Path, flags: tuple[str, ...]) -> None:
    """Rule: every run, --check included, prints REBOOT PENDING on <host> for each host that has the reboot-required
    file and for no other."""
    require_cli("ansible-playbook")
    (tmp_path / "stale-node").touch()
    reboot_required = {"base_os_reboot_required_path": str(tmp_path / "{{ inventory_hostname }}")}
    play = local_play(tmp_path, role_tasks("base-os", "reboot-required.yml"), hosts="all", vars=reboot_required)

    run = playbook(play, "-i", "stale-node,fresh-node,", "-e", "ansible_connection=local", *flags, json=True)
    assert run.returncode == 0, run.output
    assert ran(run, "Warn that this host needs a reboot") == {"stale-node": "ok", "fresh-node": "skipped"}
    assert "REBOOT PENDING on stale-node:" in run.output and "REBOOT PENDING on fresh-node" not in run.output, run.output


def test_resolv_conf_link_does_not_touch_its_target() -> None:
    """Rule: the edge block that turns resolved's stub off holds one link, resolv.conf to resolved's upstream list
    (follow false), with no owner, group or mode: resolved replaces that file, so touching it leaves every run changed."""
    tasks = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text(encoding="utf-8"))
    [edge] = [task["block"] for task in tasks if "edge" in str(task.get("when")) and "resolved-stub.conf.j2" in str(task.get("block"))]
    [link] = [task["ansible.builtin.file"] for task in edge if task.get("ansible.builtin.file", {}).get("state") == "link"]
    assert link.get("src") == "/run/systemd/resolve/resolv.conf" and link.get("follow") is False, link
    assert not link.keys() & {"owner", "group", "mode"}, link

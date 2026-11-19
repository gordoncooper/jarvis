"""base-os Ansible role: template rules and site syntax."""

from __future__ import annotations

import getpass
import grp
import json
import os
import subprocess
import textwrap
from pathlib import Path

import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    LOCALHOST,
    ansible_render_template,
    cluster_vars_playbook_path,
    env_inventory_args,
    load_cluster_vars,
    local_play,
    playbook,
    playbook_recap_field,
    role_tasks,
)
from conftest import require_cli

ROLE_TEMPLATES = ANSIBLE_DIR / "roles" / "base-os" / "templates"


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


def test_chrony_nts_string_false_renders_pools_without_nts() -> None:
    """-e chrony_nts=false is the string false, and it must turn NTS off."""
    chrony = ansible_render_template(
        "chrony.conf.j2",
        {"ntp_pools": ["time.example"], "chrony_nts": "false"},
        templates_dir=ROLE_TEMPLATES,
    )
    assert "pool time.example iburst maxsources 4\n" in chrony
    assert " nts" not in chrony
    assert "with NTS" not in chrony
    assert "4460/tcp" not in chrony


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
    run = subprocess.run(
        ["ansible", "localhost", "-c", "local", "-m", "ansible.builtin.file", "-a", json.dumps(args)],
        cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False,
        env={**os.environ, "ANSIBLE_BECOME": "false", "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg")},
    )  # fmt: skip
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
    proc = playbook("--syntax-check", "site.yml", *env_inventory_args(), "-e", f"@{cluster_vars_playbook_path()}")
    assert proc.returncode == 0, proc.output


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
    play = local_play(tmp_path, role_tasks("base-os", "chrony.yml"), bin_dir=bin_dir)
    pools = load_cluster_vars()["ntp_pools"]
    # Defaults are root:root. This play does not become root, and the template module chowns before the dest write.
    extra = json.dumps({"chrony_config_dir": str(config_dir), "chrony_config_owner": getpass.getuser(),
                        "chrony_config_group": grp.getgrgid(os.getgid()).gr_name, "ntp_pools": pools})  # fmt: skip
    first = playbook(play, *LOCALHOST, "-e", extra)
    output = first.output
    assert first.returncode == 0, output
    assert "RUNNING HANDLER" in output
    assert "Restart chrony" in output
    written = (config_dir / "chrony.conf").read_text(encoding="utf-8")
    assert " nts" in written
    for pool in pools:
        assert f"pool {pool}" in written
    assert _restarted_units(log.read_text(encoding="utf-8")) == ["chrony"]

    for again in (playbook(play, *LOCALHOST, "-e", extra), playbook(play, *LOCALHOST, "-e", extra, "--check")):
        assert again.returncode == 0, again.output
        assert playbook_recap_field(again.output, "changed") == 0
        assert "RUNNING HANDLER" not in again.output
        assert _restarted_units(log.read_text(encoding="utf-8")) == ["chrony"]


def test_pending_reboot_warns_only_the_host_that_needs_one(tmp_path: Path) -> None:
    """A host with the reboot-required file is named in a warning, also under --check. A host without it is not."""
    require_cli("ansible-playbook")
    flagged = tmp_path / "reboot-required"
    flagged.write_text("", encoding="utf-8")
    play = local_play(tmp_path, role_tasks("base-os", "reboot-required.yml"), hosts="all")
    inventory = tmp_path / "hosts.yml"
    hosts = {"stale-node": flagged, "fresh-node": tmp_path / "absent"}
    inventory.write_text(yaml.safe_dump({"all": {"hosts": {
        name: {"ansible_connection": "local", "base_os_reboot_required_path": str(path)} for name, path in hosts.items()
    }}}), encoding="utf-8")  # fmt: skip
    for extra_args in ([], ["--check"]):
        run = playbook(play, "-i", inventory, *extra_args)
        output = run.output
        assert run.returncode == 0, output
        assert "REBOOT PENDING on stale-node" in output
        assert "REBOOT PENDING on fresh-node" not in output

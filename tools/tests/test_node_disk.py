"""node-disk role: wipe refusals and the confirmed mount path (p0a-11b)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    REPO_ROOT,
    docker_exec,
    non_example_env_names,
    playbook_recap_field,
    require_docker,
    roles_inventory_path,
    start_systemd_container,
    stop_systemd_container,
)
from conftest import require_cli

FIXTURE_DIR = REPO_ROOT / "tools" / "tests" / "fixtures" / "node-disk"
DISK_ID = "/dev/disk/by-id/test-node-disk"
IMAGE_PATH = "/var/tmp/node-disk.img"
CONTAINER_NAME = "jarvis-node-disk-test"
ROLE_TASKS = REPO_ROOT / "infra" / "ansible" / "roles" / "node-disk" / "tasks"


def _run_playbook(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def test_disk_refusal_cases() -> None:
    require_cli("ansible-playbook")
    proc = _run_playbook(
        [
            "ansible-playbook",
            str(FIXTURE_DIR / "refusals.yml"),
            "-i",
            str(FIXTURE_DIR / "hosts.yml"),
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _envs_without_data_disk() -> list[str]:
    found: list[str] = []
    for name in non_example_env_names():
        hosts = REPO_ROOT / "envs" / name / "inventory" / "hosts.yml"
        if "data_disk:" not in hosts.read_text(encoding="utf-8"):
            found.append(name)
    return found


def test_site_syntax_when_no_host_declares_a_data_disk() -> None:
    require_cli("ansible-playbook")
    envs = _envs_without_data_disk()
    assert envs
    for name in envs:
        proc = _run_playbook(
            [
                "ansible-playbook",
                "--syntax-check",
                "site.yml",
                "-i",
                str(REPO_ROOT / "envs" / name / "inventory" / "hosts.yml"),
                "-i",
                str(roles_inventory_path()),
                "-e",
                f"jarvis_cluster_vars=../../envs/{name}/values/cluster-vars.yaml",
            ]
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


def _task_tags(task: dict[str, object]) -> list[str]:
    raw = task.get("tags") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def test_wipe_tasks_are_not_on_the_always_tag() -> None:
    """A --tags k3s run still executes always tasks and does not select apply."""
    tasks = yaml.safe_load((ROLE_TASKS / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    tags_by_file: dict[str, list[str]] = {}
    for task in tasks:
        assert isinstance(task, dict)
        imported = task.get("ansible.builtin.import_tasks")
        if isinstance(imported, str):
            tags_by_file[imported] = _task_tags(task)
    assert "always" in tags_by_file["prechecks.yml"]
    assert "always" in tags_by_file["checks.yml"]
    assert "always" not in tags_by_file["apply.yml"]
    assert tags_by_file["apply.yml"] == ["node-disk"]


def _start_partition_nodes(container: str) -> None:
    """The test image has no udev. Create loop partition nodes the role waits for."""
    script = """
set -u
while true; do
  for sysdev in /sys/block/loop*/loop*p*; do
    [ -e "$sysdev/dev" ] || continue
    name=$(basename "$sysdev")
    if [ ! -b "/dev/$name" ]; then
      dev=$(cat "$sysdev/dev")
      mknod -m 660 "/dev/$name" b "${dev%%:*}" "${dev##*:}" || true
    fi
  done
  sleep 0.2
done
"""
    started = subprocess.run(
        ["docker", "exec", "-d", container, "bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr


def _stage_loop(container: str) -> tuple[str, str, str]:
    script = f"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends fdisk >/dev/null
# /usr/bin/lsblk is confined to sd/nvme/dm and cannot probe a loop. The container
# has no udev database, so the role's blkid probe must be this unconfined copy.
cp -a /usr/bin/lsblk /usr/local/bin/lsblk
if [ ! -e /dev/loop-control ]; then
  mknod -m 660 /dev/loop-control c 10 237
fi
for i in $(seq 0 63); do
  if [ ! -e "/dev/loop$i" ]; then
    mknod -m 660 "/dev/loop$i" b 7 "$i"
  fi
done
fallocate -l 128M {IMAGE_PATH}
loop=$(losetup -f --show -P {IMAGE_PATH})
sfdisk "$loop" >/dev/null <<'EOF'
label: gpt
,,
EOF
base=$(basename "$loop")
part=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [ -d "/sys/block/$base/${{base}}p1" ]; then
    part="${{base}}p1"
    break
  fi
  sleep 0.2
done
test -n "$part"
if [ ! -b "/dev/$part" ]; then
  dev=$(cat "/sys/block/$base/$part/dev")
  mknod -m 660 "/dev/$part" b "${{dev%%:*}}" "${{dev##*:}}"
fi
mkdir -p /dev/disk/by-id
ln -sfn "$loop" {DISK_ID}
resolved=$(readlink -f {DISK_ID})
test "$resolved" = "$loop"
case "$resolved" in
  /dev/loop*) ;;
  *) echo "refusing to run node-disk against $resolved" >&2; exit 1 ;;
esac
sync
printf '%s\\n%s\\n' "$loop" "$part"
"""
    staged = docker_exec(container, "bash", "-lc", script)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    loop, part = staged.stdout.strip().splitlines()
    if not loop.startswith("/dev/loop"):
        raise AssertionError(f"refusing to run node-disk against {loop}")
    return loop, part, _image_checksum(container)


def _image_checksum(container: str) -> str:
    summed = docker_exec(container, "sha256sum", IMAGE_PATH)
    assert summed.returncode == 0, summed.stderr
    return summed.stdout.split()[0]


def _release_loop(container: str) -> None:
    docker_exec(
        container,
        "bash",
        "-lc",
        """
umount /var/lib/jarvis || true
while read -r name back; do
  case "$back" in
    *node-disk.img) losetup -d "$name" || true ;;
  esac
done < <(losetup -ln -O NAME,BACK-FILE || true)
""",
    )


def _disk_playbook(
    container: str, *, confirm: str | None
) -> subprocess.CompletedProcess[str]:
    extra = {"data_disk": {"id": DISK_ID, "wipe_confirmed": True}}
    if confirm is not None:
        extra["jarvis_wipe_confirm"] = confirm
    return _run_playbook(
        [
            "ansible-playbook",
            str(FIXTURE_DIR / "apply.yml"),
            "-i",
            f"{container},",
            "-e",
            "ansible_connection=docker",
            "-e",
            "ansible_user=root",
            "-e",
            json.dumps(extra),
        ]
    )


@pytest.mark.container
def test_node_disk_confirms_before_it_mounts() -> None:
    require_docker()
    require_cli("ansible-playbook")
    start_systemd_container(CONTAINER_NAME)
    try:
        _loop, part, before = _stage_loop(CONTAINER_NAME)
        _start_partition_nodes(CONTAINER_NAME)
        refused = _disk_playbook(CONTAINER_NAME, confirm=None)
        refused_text = refused.stdout + refused.stderr
        assert refused.returncode != 0, refused_text
        assert "refusing to wipe" in refused_text
        assert part in refused_text
        assert re.search(r"size: \S+", refused_text), refused_text
        assert _image_checksum(CONTAINER_NAME) == before

        confirmed = _disk_playbook(
            CONTAINER_NAME,
            confirm=f"{CONTAINER_NAME}:{DISK_ID}",
        )
        confirmed_text = confirmed.stdout + confirmed.stderr
        assert confirmed.returncode == 0, confirmed_text
        mounted = docker_exec(CONTAINER_NAME, "findmnt", "-n", "-o", "TARGET", "/var/lib/jarvis")
        assert mounted.stdout.strip() == "/var/lib/jarvis", mounted.stdout + mounted.stderr
        label = docker_exec(
            CONTAINER_NAME, "blkid", "-o", "value", "-s", "LABEL", f"/dev/{part}"
        )
        assert label.stdout.strip() == "jarvis-data", label.stdout + label.stderr
        fstab = docker_exec(CONTAINER_NAME, "cat", "/etc/fstab")
        assert fstab.returncode == 0, fstab.stderr
        fstab_line = next(
            (line for line in fstab.stdout.splitlines() if "/var/lib/jarvis" in line),
            "",
        )
        assert "LABEL=jarvis-data" in fstab_line, fstab.stdout
        assert "nofail" in fstab_line, fstab.stdout
        for path in (
            "/var/lib/jarvis/local-path",
            "/var/lib/jarvis/backup/etcd-snapshots",
            "/var/lib/jarvis/backup/restic",
        ):
            exists = docker_exec(CONTAINER_NAME, "test", "-d", path)
            assert exists.returncode == 0, path
        drop_in = docker_exec(
            CONTAINER_NAME,
            "cat",
            "/etc/systemd/system/k3s.service.d/jarvis-mount.conf",
        )
        assert drop_in.returncode == 0, drop_in.stderr
        assert (
            "RequiresMountsFor=/var/lib/jarvis/local-path /var/lib/jarvis/backup"
            in drop_in.stdout
        )
        filesystem_id = docker_exec(
            CONTAINER_NAME, "blkid", "-s", "UUID", "-o", "value", f"/dev/{part}"
        )
        assert filesystem_id.returncode == 0, filesystem_id.stderr
        original_id = filesystem_id.stdout.strip()
        assert original_id

        marker = docker_exec(
            CONTAINER_NAME,
            "bash",
            "-lc",
            "echo marker-file > /var/lib/jarvis/local-path/marker",
        )
        assert marker.returncode == 0, marker.stderr
        digest = hashlib.sha256(b"marker-file\n").hexdigest()
        again = _disk_playbook(
            CONTAINER_NAME,
            confirm=f"{CONTAINER_NAME}:{DISK_ID}",
        )
        again_text = again.stdout + again.stderr
        assert again.returncode == 0, again_text
        assert playbook_recap_field(again_text, "changed") == 0, again_text
        again_id = docker_exec(
            CONTAINER_NAME, "blkid", "-s", "UUID", "-o", "value", f"/dev/{part}"
        )
        assert again_id.stdout.strip() == original_id, again_id.stdout + again_id.stderr
        kept = docker_exec(
            CONTAINER_NAME, "cat", "/var/lib/jarvis/local-path/marker"
        )
        assert kept.stdout == "marker-file\n", kept.stdout + kept.stderr
        assert hashlib.sha256(kept.stdout.encode()).hexdigest() == digest
    finally:
        _release_loop(CONTAINER_NAME)
        stop_systemd_container(CONTAINER_NAME)

"""Per-env kubeconfig rewrite stays check-mode safe and leaves ~/.kube/config alone (p0a-13)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from ansible_role_proof import ANSIBLE_DIR
from conftest import require_cli

_SOURCE = """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: CERTDATA-MARKER
    server: https://127.0.0.1:6443
  name: default
users:
- name: default
  user:
    client-certificate-data: CERTDATA-MARKER
    client-key-data: KEYDATA-MARKER
    token: TOKEN-MARKER
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
"""
_MARKERS = ("CERTDATA-MARKER", "KEYDATA-MARKER", "TOKEN-MARKER")
_LOOPBACK = "https://127.0.0.1:6443"
_MISSED = _SOURCE.replace(_LOOPBACK, "https://kube.example:6443")


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _recap_total(output: str, field: str) -> int:
    lines = [
        line
        for line in output.splitlines()
        if f"{field}=" in line and "PLAY RECAP" not in line
    ]
    assert lines, output
    total = 0
    for line in lines:
        match = re.search(rf"{field}=(\d+)", line)
        assert match is not None, output
        total += int(match.group(1))
    return total


def _run(
    tmp: Path,
    *,
    hostname: str,
    jarvis_env: str,
    quote: bool,
    source_text: str,
    home: Path,
    check: bool = False,
    diff: bool = False,
) -> subprocess.CompletedProcess[str]:
    source = tmp / "node-kubeconfig.yaml"
    source.write_text(source_text, encoding="utf-8")
    host = json.dumps(hostname) if quote else hostname
    env_extra = json.dumps({"jarvis_env": jarvis_env}) if quote else f"jarvis_env={jarvis_env}"
    inventory = tmp / "hosts.yml"
    inventory.write_text(
        f"all:\n  hosts:\n    {host}:\n      ansible_connection: local\n",
        encoding="utf-8",
    )
    play = tmp / "play.yml"
    play.write_text(
        "---\n"
        "- name: Install kubeconfig\n"
        "  hosts: all\n"
        "  gather_facts: false\n"
        "  become: false\n"
        "  roles:\n"
        "    - kubeconfig\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    cmd = [
        "ansible-playbook",
        str(play),
        "-i",
        str(inventory),
        "-e",
        env_extra,
        "-e",
        json.dumps({"k3s_kubeconfig_path": str(source)}),
    ]
    if check:
        cmd.append("--check")
    if diff:
        cmd.append("--diff")
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _dest(home: Path, jarvis_env: str) -> Path:
    return home / ".kube" / f"jarvis-{jarvis_env}.yaml"


def _assert_no_markers(output: str) -> None:
    leaked = [marker for marker in _MARKERS if marker in output]
    assert not leaked, output


@pytest.mark.parametrize(
    ("hostname", "jarvis_env", "quote"),
    [
        ("control-1", "fixture", False),
        ("control 1:odd+", "fix 01:odd+", True),
    ],
)
def test_kubeconfig_rewrites_server_and_hides_secrets(
    tmp_path: Path, hostname: str, jarvis_env: str, quote: bool
) -> None:
    """Check mode completes, the server line alone changes, and a second run is unchanged."""
    require_cli("ansible-playbook")
    home = tmp_path / "home"
    home.mkdir()
    dest = _dest(home, jarvis_env)
    expected = _SOURCE.replace(_LOOPBACK, f"https://{hostname}:6443")

    preview = _run(
        tmp_path,
        hostname=hostname,
        jarvis_env=jarvis_env,
        quote=quote,
        source_text=_SOURCE,
        home=home,
        check=True,
        diff=True,
    )
    preview_output = preview.stdout + preview.stderr
    assert preview.returncode == 0, preview_output
    assert _recap_total(preview_output, "failed") == 0, preview_output
    _assert_no_markers(preview_output)
    assert not dest.exists()
    assert not (home / ".kube" / "config").exists()

    first = _run(
        tmp_path,
        hostname=hostname,
        jarvis_env=jarvis_env,
        quote=quote,
        source_text=_SOURCE,
        home=home,
    )
    first_output = first.stdout + first.stderr
    assert first.returncode == 0, first_output
    _assert_no_markers(first_output)
    assert dest.read_text(encoding="utf-8") == expected
    assert _LOOPBACK not in dest.read_text(encoding="utf-8")
    assert _mode(dest.parent) == 0o700
    assert _mode(dest) == 0o600
    assert not (home / ".kube" / "config").exists()
    assert sorted(path.name for path in dest.parent.iterdir()) == [dest.name]

    second = _run(
        tmp_path,
        hostname=hostname,
        jarvis_env=jarvis_env,
        quote=quote,
        source_text=_SOURCE,
        home=home,
    )
    second_output = second.stdout + second.stderr
    assert second.returncode == 0, second_output
    assert _recap_total(second_output, "changed") == 0, second_output
    assert _recap_total(second_output, "failed") == 0, second_output
    _assert_no_markers(second_output)
    assert dest.read_text(encoding="utf-8") == expected
    assert _mode(dest.parent) == 0o700
    assert _mode(dest) == 0o600


def test_kubeconfig_fails_when_the_server_line_does_not_change(tmp_path: Path) -> None:
    """A missed rewrite fails and leaves no partial or world-readable kubeconfig."""
    require_cli("ansible-playbook")
    hostname = "control 1:odd+"
    jarvis_env = "fix 01:odd+"
    home = tmp_path / "home"
    kube_dir = home / ".kube"
    kube_dir.mkdir(parents=True)
    kube_dir.chmod(0o700)
    dest = _dest(home, jarvis_env)
    dest.write_text("sentinel\n", encoding="utf-8")
    dest.chmod(0o600)

    result = _run(
        tmp_path,
        hostname=hostname,
        jarvis_env=jarvis_env,
        quote=True,
        source_text=_MISSED,
        home=home,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    _assert_no_markers(output)
    assert dest.read_text(encoding="utf-8") == "sentinel\n"
    assert _mode(dest) == 0o600
    assert _mode(kube_dir) == 0o700
    assert sorted(path.name for path in kube_dir.iterdir()) == [dest.name]
    for path in kube_dir.rglob("*"):
        assert _mode(path) & 0o007 == 0, path

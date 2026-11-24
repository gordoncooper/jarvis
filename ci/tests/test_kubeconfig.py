"""Per-env kubeconfig: check-mode safe, only the server line rewritten, no secret printed, ~/.kube/config left alone."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from ansible_role_proof import changed, local_play, playbook, ran, role_tasks
from conftest import require_cli

_LOOPBACK = "https://127.0.0.1:6443"
_SECRET = "SECRET-MARKER"
_SOURCE = f"""\
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: {_SECRET}
    server: {_LOOPBACK}
  name: default
users:
- name: default
  user:
    client-certificate-data: {_SECRET}
    client-key-data: {_SECRET}
    token: {_SECRET}
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
"""


def _kubeconfig(tmp_path: Path, *flags: str, source: str = _SOURCE) -> subprocess.CompletedProcess[str]:
    """The kubeconfig role for env fixture on control-1, run on this machine with HOME at tmp_path/home and source as
    the node's kubeconfig."""
    node_kubeconfig = tmp_path / "k3s.yaml"
    node_kubeconfig.write_text(source, encoding="utf-8")
    play_vars = {"jarvis_env": "fixture", "k3s_kubeconfig_path": str(node_kubeconfig)}
    play = local_play(tmp_path, role_tasks("kubeconfig"), hosts="all", vars=play_vars)
    inventory = ("-i", "control-1,", "-e", "ansible_connection=local")
    return playbook(play, *inventory, *flags, env={"HOME": str(tmp_path / "home")}, json=True)


def _modes(kube: Path) -> dict[str, int]:
    return {path.name: stat.S_IMODE(path.stat().st_mode) for path in (kube, *kube.iterdir())}


def test_kubeconfig_rewrites_server_and_hides_secrets(tmp_path: Path) -> None:
    """Rule: --check writes nothing. A run writes ~/.kube/jarvis-<env>.yaml (0600, in a 0700 ~/.kube that holds
    nothing else) with only the server line changed, and a second run changes nothing. No run prints a secret."""
    require_cli("ansible-playbook")
    kube = tmp_path / "home" / ".kube"
    kube.parent.mkdir()

    preview = _kubeconfig(tmp_path, "--check", "--diff")
    assert preview.returncode == 0, preview.output
    assert not kube.exists()

    first = _kubeconfig(tmp_path)
    assert first.returncode == 0, first.output
    assert _modes(kube) == {".kube": 0o700, "jarvis-fixture.yaml": 0o600}
    assert (kube / "jarvis-fixture.yaml").read_text(encoding="utf-8") == _SOURCE.replace(_LOOPBACK, "https://control-1:6443")

    second = _kubeconfig(tmp_path)
    assert second.returncode == 0, second.output
    assert changed(second) == {"control-1": 0}
    for run in (preview, first, second):
        assert _SECRET not in run.output, run.output


def test_kubeconfig_fails_when_the_server_line_does_not_change(tmp_path: Path) -> None:
    """Rule: a node kubeconfig without the loopback server fails the run at the rewrite check, prints no secret, and
    leaves the runner's kubeconfig and ~/.kube as they were."""
    require_cli("ansible-playbook")
    kube = tmp_path / "home" / ".kube"
    kube.mkdir(parents=True)
    kube.chmod(0o700)
    runner_kubeconfig = kube / "jarvis-fixture.yaml"
    runner_kubeconfig.write_text("sentinel\n", encoding="utf-8")
    runner_kubeconfig.chmod(0o600)

    run = _kubeconfig(tmp_path, source=_SOURCE.replace(_LOOPBACK, "https://kube.example:6443"))
    assert run.returncode != 0, run.output
    assert ran(run, "Fail when the kubeconfig server line did not change") == {"control-1": "failed"}
    assert _SECRET not in run.output, run.output
    assert runner_kubeconfig.read_text(encoding="utf-8") == "sentinel\n"
    assert _modes(kube) == {".kube": 0o700, "jarvis-fixture.yaml": 0o600}

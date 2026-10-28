"""P0a verify: fake kubectl JSON, no cluster and no container."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from conftest import require_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_VALUE = "SHOULD_NOT_PRINT_secret_value"
DNS_SUFFIX = "test.example"
ENABLELB = "svccontroller.k3s.cattle.io/enablelb"

_LOADER = SourceFileLoader("verify_p0a", str(REPO_ROOT / "tools" / "verify" / "p0a"))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
verify_p0a = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(verify_p0a)


def _completed(args: Sequence[str], stdout: str, *, returncode: int = 0, stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(list(args), returncode, stdout, stderr)


def _write_env(root: Path) -> str:
    env = "env-test"
    inventory = root / "envs" / env / "inventory"
    values = root / "envs" / env / "values"
    inventory.mkdir(parents=True)
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text(f"dns_suffix: {DNS_SUFFIX}\n", encoding="utf-8")
    (inventory / "hosts.yml").write_text(
        "all:\n  hosts:\n    n1:\n      node_roles: [control_plane, edge]\n",
        encoding="utf-8",
    )
    return env


def _labels(*roles: str) -> dict[str, str]:
    labels = {f"role.{DNS_SUFFIX}/{role}": "true" for role in roles}
    labels["kubernetes.io/hostname"] = "n1"
    labels[ENABLELB] = "true"
    return labels


def _kubectl(node_labels: dict[str, str], *, flux: bool):
    flux_item = {"metadata": {"name": "platform", "namespace": "flux-system"}}

    def kubectl(args: Sequence[str]) -> CompletedProcess[str]:
        argv = list(args)
        if argv[:2] == ["get", "nodes"]:
            body = {"items": [{"metadata": {"name": "n1", "labels": node_labels}}]}
            return _completed(argv, json.dumps(body))
        if argv[:3] == ["get", "secret", "backups-restic"]:
            return _completed(argv, json.dumps({"data": {"RESTIC_PASSWORD": SECRET_VALUE}}))
        if argv[0] == "get" and "-A" in argv:
            # flux=False is the first-boot partial: Kustomizations exist, HelmRelease does not.
            has_items = flux or not argv[1].startswith("helmreleases.")
            items = [flux_item] if has_items else []
            return _completed(argv, json.dumps({"items": items}))
        if argv[:2] == ["wait", "--for=condition=Ready"] and "--all" in argv:
            return _completed(argv, "condition met\n")
        return _completed(argv, "", returncode=1, stderr=f"unexpected kubectl: {argv}")

    return kubectl


def test_verify_p0a_wrong_role_and_no_flux_objects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    require_cli("ansible-inventory")
    env = _write_env(tmp_path)
    matched = _kubectl(_labels("control_plane", "edge"), flux=True)
    wrong = _kubectl(_labels("control_plane", "edge", "workers"), flux=True)
    partial = _kubectl(_labels("control_plane", "edge"), flux=False)

    assert verify_p0a.run(tmp_path, env, matched, budget_s=0) == 0
    matched_out = capsys.readouterr().out
    assert SECRET_VALUE not in matched_out
    items = [line for line in matched_out.splitlines() if line.startswith("- ")]
    assert items
    assert matched_out.splitlines()[-1] == f"automatic checks passed; {len(items)} owner items remain"

    assert verify_p0a.run(tmp_path, env, wrong, budget_s=0) != 0
    wrong_out = capsys.readouterr().out
    assert SECRET_VALUE not in wrong_out
    assert "automatic checks passed" not in wrong_out

    assert verify_p0a.run(tmp_path, env, partial, budget_s=0) != 0
    partial_out = capsys.readouterr().out
    assert "FAIL flux Ready: missing helmreleases.helm.toolkit.fluxcd.io" in partial_out
    assert SECRET_VALUE not in partial_out
    assert "automatic checks passed" not in partial_out


def test_missing_binary_prints_deps_hint(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        verify_p0a._run(["jarvis-missing-binary"])
    assert raised.value.code == 1
    assert capsys.readouterr().out == "run make deps\n"

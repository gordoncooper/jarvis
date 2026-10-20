"""Tests for make verify PHASE=p0a and rollback-drill-wait (fake kubectl on PATH)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from _ci_rule_checks import expected_ci_rule_checks, phase_verify_scripts
from _verify_p0a import (
    DECRYPT_KUSTOMIZATION,
    DECRYPT_SECRET_NAME,
    DECRYPT_SECRET_NAMESPACE,
    EVIDENCE_BOOTSTRAP_SECRETS,
    EVIDENCE_CI_SEEDS,
    EVIDENCE_RENOVATE,
    EVIDENCE_ROLLBACK_LOG,
    EVIDENCE_SITE_YML,
    KubectlResult,
    REQUIRED_FLUX_API_NAMES,
    ROLLBACK_DRILL_BASELINE_VALUE,
    ROLLBACK_DRILL_NEW_VALUE,
    bootstrap_secrets_paste_ok,
    collect_flux_objects,
    discover_flux_api_resource_names,
    flux_groups_from_api_resource_list,
    github_pull_request_url_ok,
    inventory_hostnames,
    run_flux_ready_check,
    run_verify,
    site_yml_paste_ok,
    validate_rollback_log,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROLLBACK_WAIT = REPO_ROOT / "tools" / "rollback-drill-wait"
RUN_CHECKS = REPO_ROOT / "tools" / "run-checks"
EXAMPLE_SLUG = "example-org/example-repo"
EXAMPLE_PR_BASE = f"https://github.com/{EXAMPLE_SLUG}/pull"
REQUIRED_CHECKS = expected_ci_rule_checks(REPO_ROOT)

GROUP_API_NAMES: dict[str, list[str]] = {
    "kustomize.toolkit.fluxcd.io": ["kustomizations.kustomize.toolkit.fluxcd.io"],
    "source.toolkit.fluxcd.io": [
        "gitrepositories.source.toolkit.fluxcd.io",
        "helmcharts.source.toolkit.fluxcd.io",
        "helmrepositories.source.toolkit.fluxcd.io",
    ],
    "helm.toolkit.fluxcd.io": ["helmreleases.helm.toolkit.fluxcd.io"],
    "fluxcd.controlplane.io": ["fluxinstances.fluxcd.controlplane.io"],
    "notification.toolkit.fluxcd.io": [
        "alerts.notification.toolkit.fluxcd.io",
        "providers.notification.toolkit.fluxcd.io",
    ],
}


def _flux_item(kind: str, name: str, namespace: str, ready: bool) -> dict:
    status = "True" if ready else "False"
    return {
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
        "status": {
            "conditions": [{"type": "Ready", "status": status, "message": "ok"}],
        },
    }


def _secret_item(
    *,
    flux_kustomization: str = DECRYPT_KUSTOMIZATION,
    data: dict[str, str] | None = None,
) -> dict:
    return {
        "kind": "Secret",
        "metadata": {
            "name": DECRYPT_SECRET_NAME,
            "namespace": DECRYPT_SECRET_NAMESPACE,
            "labels": {"kustomize.toolkit.fluxcd.io/name": flux_kustomization},
        },
        "data": data or {"RESTIC_PASSWORD": "c2VjcmV0"},
    }


def _load_flux_api_resource_list() -> dict:
    return json.loads(
        (FIXTURES / "kubectl-api-resources-flux.json").read_text(encoding="utf-8")
    )


def _site_recap_for_repo(repo_root: Path) -> str:
    hosts = inventory_hostnames(repo_root)
    lines = ["PLAY RECAP"] + [
        f"{host} : ok=1 changed=0 unreachable=0 failed=0" for host in hosts
    ]
    return "\n".join(lines) + "\n"


def _rollback_log_ok() -> str:
    return "\n".join(
        [
            f"2026-01-01T00:00:00Z value={ROLLBACK_DRILL_NEW_VALUE} flux_kustomization_revision=rev1",
            f"2026-01-01T00:01:00Z reached value={ROLLBACK_DRILL_NEW_VALUE} flux_kustomization_revision=rev1",
            f"2026-01-01T00:02:00Z value={ROLLBACK_DRILL_BASELINE_VALUE} flux_kustomization_revision=rev2",
            f"2026-01-01T00:03:00Z reached value={ROLLBACK_DRILL_BASELINE_VALUE} flux_kustomization_revision=rev2",
        ]
    )


def _write_evidence(evidence_dir: Path, *, rollback_log: str | None = None) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    seed_lines = [
        f"{name} {EXAMPLE_PR_BASE}/{idx + 100}"
        for idx, name in enumerate(REQUIRED_CHECKS)
    ]
    (evidence_dir / EVIDENCE_CI_SEEDS).write_text(
        "\n".join(seed_lines) + "\n",
        encoding="utf-8",
    )
    (evidence_dir / EVIDENCE_RENOVATE).write_text(
        f"{EXAMPLE_PR_BASE}/30\n",
        encoding="utf-8",
    )
    (evidence_dir / EVIDENCE_ROLLBACK_LOG).write_text(
        rollback_log if rollback_log is not None else _rollback_log_ok(),
        encoding="utf-8",
    )
    (evidence_dir / EVIDENCE_SITE_YML).write_text(
        _site_recap_for_repo(REPO_ROOT),
        encoding="utf-8",
    )
    (evidence_dir / EVIDENCE_BOOTSTRAP_SECRETS).write_text(
        "bootstrap-secrets: all files present; nothing changed\n",
        encoding="utf-8",
    )


def _happy_flux_by_api() -> dict[str, list[dict]]:
    return {
        "kustomizations.kustomize.toolkit.fluxcd.io": [
            _flux_item("Kustomization", "platform-configs", "flux-system", True),
        ],
        "gitrepositories.source.toolkit.fluxcd.io": [
            _flux_item("GitRepository", "flux-system", "flux-system", True),
        ],
        "helmreleases.helm.toolkit.fluxcd.io": [],
        "helmrepositories.source.toolkit.fluxcd.io": [],
        "helmcharts.source.toolkit.fluxcd.io": [],
        "fluxinstances.fluxcd.controlplane.io": [
            _flux_item("FluxInstance", "flux", "flux-system", True),
        ],
    }


def _kubectl_router(
    *,
    flux_by_api: dict[str, list[dict]] | None = None,
    api_resource_list: dict | None = None,
    group_api_names: dict[str, list[str]] | None = None,
    secret: dict | None,
    api_resources_rc: int = 0,
    api_resources_error: str = "",
    omit_required_api: frozenset[str] = frozenset(),
) -> callable:
    api_doc = api_resource_list if api_resource_list is not None else _load_flux_api_resource_list()
    groups_map = group_api_names if group_api_names is not None else GROUP_API_NAMES
    flux = flux_by_api if flux_by_api is not None else _happy_flux_by_api()

    def _call(args: list[str]) -> KubectlResult:
        if args == ["api-resources", "-o", "json"]:
            if api_resources_rc != 0:
                return KubectlResult(api_resources_rc, "", api_resources_error)
            return KubectlResult(0, json.dumps(api_doc), "")
        if (
            args[0] == "api-resources"
            and len(args) >= 2
            and args[1].startswith("--api-group=")
            and args[-2:] == ["-o", "name"]
        ):
            group = args[1].split("=", 1)[1]
            names = [
                n
                for n in groups_map.get(group, [])
                if n not in omit_required_api
            ]
            return KubectlResult(0, "\n".join(names) + ("\n" if names else ""), "")
        if (
            args[:4] == ["get", "secret", DECRYPT_SECRET_NAME, "-n"]
            and args[4] == DECRYPT_SECRET_NAMESPACE
        ):
            if secret is None:
                return KubectlResult(1, "", "secret missing")
            return KubectlResult(0, json.dumps(secret), "")
        if args[:1] == ["get"] and len(args) >= 4 and args[-2:] == ["-o", "json"]:
            resource = args[1]
            return KubectlResult(0, json.dumps({"items": flux.get(resource, [])}), "")
        raise AssertionError(f"unexpected kubectl args: {args}")

    return _call


def test_github_pull_url_exact_match() -> None:
    assert github_pull_request_url_ok(EXAMPLE_SLUG, f"{EXAMPLE_PR_BASE}/42")
    assert not github_pull_request_url_ok(EXAMPLE_SLUG, f"{EXAMPLE_PR_BASE}/42/files")
    assert not github_pull_request_url_ok(EXAMPLE_SLUG, f"https://github.com/{EXAMPLE_SLUG}")


def test_site_yml_recap_rules() -> None:
    hosts = ("node-a.example", "node-b.example")
    ok = "PLAY RECAP\nnode-a.example : ok=1 changed=0 unreachable=0 failed=0\nnode-b.example : ok=1 changed=0 unreachable=0 failed=0\n"
    assert site_yml_paste_ok(ok, hosts)
    assert not site_yml_paste_ok(
        "PLAY RECAP\nnode-a.example : ok=1 changed=0 unreachable=0 failed=1\nnode-b.example : ok=1 changed=0 unreachable=0 failed=0\n",
        hosts,
    )
    assert not site_yml_paste_ok(
        "PLAY RECAP\nnode-a.example : ok=1 changed=0 unreachable=0 failed=0\n",
        hosts,
    )


def test_flux_groups_from_real_api_resource_list_shape() -> None:
    doc = _load_flux_api_resource_list()
    groups, errors = flux_groups_from_api_resource_list(doc)
    assert not errors
    assert "kustomize.toolkit.fluxcd.io" in groups
    assert "fluxcd.controlplane.io" in groups


def test_flux_groups_rejects_items_only_shape() -> None:
    groups, errors = flux_groups_from_api_resource_list({"items": []})
    assert not groups
    assert any("missing resources list" in e for e in errors)


def test_discover_flux_skips_alerts_in_name_output() -> None:
    router = _kubectl_router(flux_by_api={}, secret=_secret_item())
    discovered, notes, errors = discover_flux_api_resource_names(router)
    assert not errors
    assert "alerts.notification.toolkit.fluxcd.io" not in discovered
    assert REQUIRED_FLUX_API_NAMES.issubset(discovered)


def test_run_flux_ready_fails_on_zero_objects() -> None:
    router = _kubectl_router(
        flux_by_api={
            "kustomizations.kustomize.toolkit.fluxcd.io": [],
            "gitrepositories.source.toolkit.fluxcd.io": [],
            "helmreleases.helm.toolkit.fluxcd.io": [],
            "helmrepositories.source.toolkit.fluxcd.io": [],
            "fluxinstances.fluxcd.controlplane.io": [],
        },
        secret=_secret_item(),
    )
    _lines, errors = run_flux_ready_check(router)
    assert any("no Flux objects found" in e for e in errors)


def test_run_flux_ready_fails_missing_required_crd() -> None:
    router = _kubectl_router(
        flux_by_api=_happy_flux_by_api(),
        secret=_secret_item(),
        omit_required_api=frozenset({"fluxinstances.fluxcd.controlplane.io"}),
    )
    _lines, errors = run_flux_ready_check(router)
    assert any("required CRD missing: fluxinstances.fluxcd.controlplane.io" in e for e in errors)


def test_old_items_api_resources_shape_fails_verify(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    router = _kubectl_router(
        flux_by_api=_happy_flux_by_api(),
        api_resource_list={"kind": "APIResourceList", "items": []},
        secret=_secret_item(),
    )
    result = run_verify(
        REPO_ROOT,
        kubectl_run=router,
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
        required_checks=REQUIRED_CHECKS,
    )
    assert not result.ok
    assert any("missing resources list" in e for e in result.errors)


def test_collect_flux_skips_optional_missing_crd() -> None:
    def router(args: list[str]) -> KubectlResult:
        if len(args) >= 2 and args[1] == "widgets.widget.toolkit.fluxcd.io":
            return KubectlResult(1, "", "the server could not find the requested resource")
        return KubectlResult(0, json.dumps({"items": []}), "")

    objects, notes, errors = collect_flux_objects(
        {"widgets.widget.toolkit.fluxcd.io", "kustomizations.kustomize.toolkit.fluxcd.io"},
        router,
    )
    assert objects == []
    assert not errors
    assert any("widgets" in n for n in notes)


def test_flux_get_uses_qualified_helmcharts_not_bare_short_name() -> None:
    """Bare helmcharts resolves to k3s helm.cattle.io; verify must use the Flux FQ name."""
    cattle_traefik = {
        "kind": "HelmChart",
        "metadata": {"name": "traefik", "namespace": "kube-system"},
        "status": {},
    }
    cattle_crd = {
        "kind": "HelmChart",
        "metadata": {"name": "traefik-crd", "namespace": "kube-system"},
        "status": {},
    }
    flux_by_api = _happy_flux_by_api()
    flux_by_api["helmcharts.source.toolkit.fluxcd.io"] = []

    calls: list[str] = []

    def router(args: list[str]) -> KubectlResult:
        if args == ["api-resources", "-o", "json"]:
            return KubectlResult(0, json.dumps(_load_flux_api_resource_list()), "")
        if (
            args[0] == "api-resources"
            and args[1].startswith("--api-group=")
            and args[-2:] == ["-o", "name"]
        ):
            group = args[1].split("=", 1)[1]
            names = GROUP_API_NAMES.get(group, [])
            return KubectlResult(0, "\n".join(names) + ("\n" if names else ""), "")
        if args[:1] == ["get"] and len(args) >= 4 and args[-2:] == ["-o", "json"]:
            resource = args[1]
            calls.append(resource)
            if resource == "helmcharts":
                return KubectlResult(
                    0,
                    json.dumps({"items": [cattle_traefik, cattle_crd]}),
                    "",
                )
            return KubectlResult(0, json.dumps({"items": flux_by_api.get(resource, [])}), "")
        raise AssertionError(f"unexpected kubectl args: {args}")

    lines, errors = run_flux_ready_check(router)
    text = "\n".join(lines)
    assert "traefik" not in text
    assert "helmcharts" not in calls
    assert "helmcharts.source.toolkit.fluxcd.io" in calls
    assert not any("flux not ready" in e and "traefik" in e for e in errors)


def test_verify_happy_path(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
        required_checks=REQUIRED_CHECKS,
    )
    assert result.ok


def test_verify_fails_when_flux_not_ready(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    flux = _happy_flux_by_api()
    flux["kustomizations.kustomize.toolkit.fluxcd.io"] = [
        _flux_item("Kustomization", "services", "flux-system", False),
    ]
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(flux_by_api=flux, secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
    )
    assert not result.ok
    assert any("flux not ready" in e for e in result.errors)


def test_verify_fails_kubectl_error_on_flux_read(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    router = _kubectl_router(
        flux_by_api=_happy_flux_by_api(),
        secret=_secret_item(),
        api_resources_rc=1,
        api_resources_error="Unable to connect to the server",
    )
    result = run_verify(REPO_ROOT, kubectl_run=router, evidence_dir=evidence, slug=EXAMPLE_SLUG)
    assert not result.ok
    assert any("flux:" in e and "kubectl" in e for e in result.errors)
    assert "Traceback" not in "\n".join(result.lines)


def test_verify_fails_site_yml_failed_recap(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    hosts = inventory_hostnames(REPO_ROOT)
    assert hosts
    host = hosts[0]
    (evidence / EVIDENCE_SITE_YML).write_text(
        f"PLAY RECAP\n{host} : ok=1 changed=0 unreachable=0 failed=1\n",
        encoding="utf-8",
    )
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
    )
    assert not result.ok
    assert any("site-yml-second-run" in e for e in result.errors)


def test_verify_fails_bootstrap_created_line(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    (evidence / EVIDENCE_BOOTSTRAP_SECRETS).write_text(
        "bootstrap-secrets: all files present; nothing changed\ncreated envs/example-env/secrets/x.sops.yaml\n",
        encoding="utf-8",
    )
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
    )
    assert not result.ok
    assert any("bootstrap-secrets-second-run" in e for e in result.errors)


def test_verify_fails_missing_ci_seed_check(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(evidence)
    (evidence / EVIDENCE_CI_SEEDS).write_text(
        f"cli-deny {EXAMPLE_PR_BASE}/101\n",
        encoding="utf-8",
    )
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
        required_checks=REQUIRED_CHECKS,
    )
    assert not result.ok
    assert any("missing ci seed for check" in e for e in result.errors)


def test_verify_fails_rollback_only_new_value(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    _write_evidence(
        evidence,
        rollback_log=(
            f"2026-01-01T00:01:00Z reached value={ROLLBACK_DRILL_NEW_VALUE} "
            "flux_kustomization_revision=rev1\n"
        ),
    )
    result = run_verify(
        REPO_ROOT,
        kubectl_run=_kubectl_router(secret=_secret_item()),
        evidence_dir=evidence,
        slug=EXAMPLE_SLUG,
    )
    assert not result.ok
    assert any("baseline" in e for e in result.errors)


def test_run_checks_rejects_misnamed_verify_foo(tmp_path: Path) -> None:
    checks = tmp_path / "tools" / "checks"
    checks.mkdir(parents=True)
    (tmp_path / "tools").mkdir(exist_ok=True)
    for name in REQUIRED_CHECKS:
        script = checks / name
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (checks / "verify-p0a.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (checks / "verify-p0a.sh").chmod(stat.S_IXUSR)
    extra = checks / "verify-foo"
    extra.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    extra.chmod(stat.S_IXUSR)
    (tmp_path / "tools" / "ci-rule-checks.txt").write_text(
        "\n".join(REQUIRED_CHECKS) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "tools" / "phase-verify-scripts.txt").write_text("verify-p0a.sh\n", encoding="utf-8")
    subprocess.run(
        ["cp", str(REPO_ROOT / "tools" / "run-checks"), str(tmp_path / "tools" / "run-checks")],
        check=True,
    )
    script = textwrap.dedent(
        f"""
        set -euo pipefail
        repo_root="{tmp_path}"
        cd "$repo_root"
        export PATH="${{HOME}}/.local/bin:${{PATH}}"
        bash tools/run-checks
        """
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "verify-foo" in proc.stderr or "verify-foo" in proc.stdout


def test_phase_verify_scripts_matches_makefile() -> None:
    assert phase_verify_scripts(REPO_ROOT) == ("verify-p0a.sh",)


def test_run_checks_passes_in_repo() -> None:
    subprocess.run([str(RUN_CHECKS)], cwd=REPO_ROOT, check=True)


def test_rollback_drill_wait_times_out(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = textwrap.dedent(
        """
        #!/usr/bin/env bash
        if [[ "$1" == "get" && "$2" == "configmap" ]]; then
          echo "baseline-wrong"
          exit 0
        fi
        if [[ "$1" == "get" && "$2" == "kustomization" ]]; then
          echo "rev1"
          exit 0
        fi
        exit 1
        """
    )
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(shim, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    log_file = tmp_path / "rollback.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["JARVIS_ROLLBACK_TIMEOUT_SEC"] = "1"
    env["JARVIS_ROLLBACK_INTERVAL_SEC"] = "1"
    proc = subprocess.run(
        [str(ROLLBACK_WAIT), "expected-token", str(log_file)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "timed out" in proc.stderr


def test_rollback_drill_wait_writes_reached_marker(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = textwrap.dedent(
        """
        #!/usr/bin/env bash
        if [[ "$1" == "get" && "$2" == "configmap" ]]; then
          echo "target-value"
          exit 0
        fi
        if [[ "$1" == "get" && "$2" == "kustomization" ]]; then
          echo "sha256:deadbeef"
          exit 0
        fi
        exit 1
        """
    )
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(shim, encoding="utf-8")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    log_file = tmp_path / "rollback.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [str(ROLLBACK_WAIT), "target-value", str(log_file)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "reached value=target-value" in log_file.read_text(encoding="utf-8")

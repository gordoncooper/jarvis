"""manifest-lint CI check (p0b-06, R5)."""

from __future__ import annotations

from pathlib import Path

from _manifest_lint import scan_manifest_lint

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "manifest-lint" / "missing-memory-limit.yaml"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_r5_seeded_manifest_missing_memory_limit_fails(tmp_path: Path) -> None:
    target = tmp_path / "platform" / "seeded.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    violations = scan_manifest_lint(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/seeded.yaml"
    assert "memory limit is unset" in violations[0].message


def test_r5_cpu_limit_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/cpu.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seeded
  namespace: demo
spec:
  template:
    spec:
      containers:
        - name: app
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
          livenessProbe: {httpGet: {path: /, port: 80}}
          readinessProbe: {httpGet: {path: /, port: 80}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 100m, memory: 256Mi}
""",
    )
    violations = scan_manifest_lint(tmp_path)
    assert len(violations) == 1
    assert "cpu limit is set" in violations[0].message


def test_r5_null_cpu_limit_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/ok.yaml",
        """
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: k8s-gateway
  namespace: flux-system
spec:
  values:
    resources:
      requests: {cpu: 100m, memory: 128Mi}
      limits: {cpu: null, memory: 256Mi}
""",
    )
    assert scan_manifest_lint(tmp_path) == []


def test_r5_missing_readiness_probe_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/probe.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seeded
  namespace: demo
spec:
  template:
    spec:
      containers:
        - name: app
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
          livenessProbe: {httpGet: {path: /, port: 80}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {memory: 256Mi}
""",
    )
    violations = scan_manifest_lint(tmp_path)
    assert len(violations) == 1
    assert "readinessProbe is unset" in violations[0].message


def test_r5_cronjob_without_probes_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/job.yaml",
        """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: copy
  namespace: demo
spec:
  schedule: "0 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: copy
              image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
              resources:
                requests: {cpu: 100m, memory: 128Mi}
                limits: {memory: 256Mi}
""",
    )
    assert scan_manifest_lint(tmp_path) == []


def test_r5_init_container_needs_resources_not_probes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/init.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seeded
  namespace: demo
spec:
  template:
    spec:
      initContainers:
        - name: setup
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
          resources:
            requests: {cpu: 10m, memory: 32Mi}
            limits: {}
      containers:
        - name: app
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
          livenessProbe: {httpGet: {path: /, port: 80}}
          readinessProbe: {httpGet: {path: /, port: 80}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {memory: 256Mi}
""",
    )
    violations = scan_manifest_lint(tmp_path)
    assert len(violations) == 1
    assert "container setup" in violations[0].message
    assert "memory limit is unset" in violations[0].message


def test_r5_log_file_arg_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "platform/logs.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seeded
  namespace: demo
spec:
  template:
    spec:
      containers:
        - name: app
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
          args: ["--log-file=/var/log/app.log"]
          livenessProbe: {httpGet: {path: /, port: 80}}
          readinessProbe: {httpGet: {path: /, port: 80}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {memory: 256Mi}
""",
    )
    violations = scan_manifest_lint(tmp_path)
    assert len(violations) == 1
    assert "logs to a file" in violations[0].message


def test_r5_platform_manifests_pass() -> None:
    assert scan_manifest_lint(REPO) == []

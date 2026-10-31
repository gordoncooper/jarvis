"""loadbalancer CI check (p0b-06, R28)."""

from __future__ import annotations

from pathlib import Path

from _loadbalancer import scan_loadbalancer

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "loadbalancer" / "extra-service.yaml"


def _copy(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")


def test_r28_seeded_loadbalancer_fails(tmp_path: Path) -> None:
    _copy(tmp_path, "platform/extra/service.yaml")
    violations = scan_loadbalancer(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/extra/service.yaml"
    assert "LoadBalancer" in violations[0].message


def test_r28_extra_loadbalancer_under_an_allowed_folder_fails(tmp_path: Path) -> None:
    _copy(tmp_path, "platform/traefik/service.yaml")
    violations = scan_loadbalancer(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/traefik/service.yaml"
    assert "Service demo/extra" in violations[0].message


def test_r28_traefik_and_app_dns_owners_pass(tmp_path: Path) -> None:
    (tmp_path / "platform" / "app-dns").mkdir(parents=True)
    (tmp_path / "platform" / "traefik").mkdir(parents=True)
    (tmp_path / "platform" / "app-dns" / "helmrelease.yaml").write_text(
        """
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: k8s-gateway
  namespace: flux-system
spec:
  values:
    serviceType: LoadBalancer
""",
        encoding="utf-8",
    )
    (tmp_path / "platform" / "traefik" / "helmchartconfig.yaml").write_text(
        """
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |
    service:
      spec:
        type: LoadBalancer
""",
        encoding="utf-8",
    )
    assert scan_loadbalancer(tmp_path) == []


def test_r28_helmrelease_loadbalancer_outside_app_dns_fails(tmp_path: Path) -> None:
    path = tmp_path / "platform" / "other" / "helmrelease.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: other
  namespace: flux-system
spec:
  values:
    serviceType: LoadBalancer
""",
        encoding="utf-8",
    )
    violations = scan_loadbalancer(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/other/helmrelease.yaml"


def test_r28_unlisted_helmrelease_under_app_dns_fails(tmp_path: Path) -> None:
    path = tmp_path / "platform" / "app-dns" / "second.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: second
  namespace: flux-system
spec:
  values:
    serviceType: LoadBalancer
""",
        encoding="utf-8",
    )
    violations = scan_loadbalancer(tmp_path)
    assert len(violations) == 1
    assert "HelmRelease flux-system/second" in violations[0].message

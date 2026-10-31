"""digest-pin CI check (p0b-07, R33)."""

from __future__ import annotations

from pathlib import Path

from _digest_pin import scan_digest_pin

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "digest-pin" / "tag-without-digest.yaml"

_PINNED = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pinned
  namespace: demo
spec:
  template:
    spec:
      containers:
        - name: app
          image: example@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: charts
  namespace: flux-system
spec:
  values:
    image:
      registry: example.io
      repository: demo/app
      tag: v1.2.3@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    webhook:
      image:
        tag: v1.2.3
        digest: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    mover:
      image: example.io/demo/app@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: flux-operator
  namespace: flux-system
spec:
  url: oci://example.io/charts/flux-operator
  ref:
    tag: "0.60.0"
---
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |
    image:
      tag: v3.0.0@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
"""


def test_r33_seeded_image_tag_without_digest_fails(tmp_path: Path) -> None:
    target = tmp_path / "platform" / "seeded.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    violations = scan_digest_pin(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/seeded.yaml"
    assert "nginx:1.27" in violations[0].message
    assert "not pinned by digest" in violations[0].message


def test_r33_helm_tag_without_digest_fails(tmp_path: Path) -> None:
    path = tmp_path / "platform" / "release.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: demo
  namespace: flux-system
spec:
  values:
    image:
      repository: example.io/demo/app
      tag: v1.2.3
""",
        encoding="utf-8",
    )
    violations = scan_digest_pin(tmp_path)
    assert len(violations) == 1
    assert "not pinned by digest" in violations[0].message


def test_r33_helmchartconfig_tag_without_digest_fails(tmp_path: Path) -> None:
    path = tmp_path / "platform" / "traefik" / "helmchartconfig.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |
    image:
      repository: example.io/demo/proxy
      tag: v3.0.0
""",
        encoding="utf-8",
    )
    violations = scan_digest_pin(tmp_path)
    assert len(violations) == 1
    assert "HelmChartConfig kube-system/traefik: image" in violations[0].message
    assert "not pinned by digest" in violations[0].message


def test_r33_pinned_images_and_chart_tags_pass(tmp_path: Path) -> None:
    path = tmp_path / "platform" / "ok.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(_PINNED, encoding="utf-8")
    assert scan_digest_pin(tmp_path) == []

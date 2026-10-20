"""Pinned checksums match upstream release checksum manifests (network)."""

from __future__ import annotations

import re
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_ENV = REPO_ROOT / "tools" / "versions.env"


def _load_versions_env() -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw in VERSIONS_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        versions[key.strip()] = value.strip()
    return versions


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read().decode()


def _checksum_map(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^([0-9a-f]{64})\s+(.+)$", line)
        if match:
            mapping[match.group(2).strip()] = match.group(1).lower()
    return mapping


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture(scope="module")
def versions() -> dict[str, str]:
    return _load_versions_env()


@pytest.mark.parametrize(
    ("arch", "pin_key", "checksum_url", "artifact_name"),
    [
        (
            "amd64",
            "helm_linux_amd64_tarball_sha256",
            "https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz.sha256sum",
            "helm-v{version}-linux-amd64.tar.gz",
        ),
        (
            "arm64",
            "helm_linux_arm64_tarball_sha256",
            "https://get.helm.sh/helm-v{version}-linux-arm64.tar.gz.sha256sum",
            "helm-v{version}-linux-arm64.tar.gz",
        ),
    ],
)
def test_helm_tarball_pin_matches_upstream_sha256sum(
    versions: dict[str, str],
    arch: str,
    pin_key: str,
    checksum_url: str,
    artifact_name: str,
) -> None:
    version = versions["helm_version"]
    pin = versions[pin_key]
    text = _fetch_text(checksum_url.format(version=version))
    upstream = _checksum_map(text)[artifact_name.format(version=version)]
    assert pin == upstream


@pytest.mark.parametrize(
    ("arch", "pin_key", "artifact_suffix"),
    [
        ("amd64", "kustomize_linux_amd64_tarball_sha256", "kustomize_v{version}_linux_amd64.tar.gz"),
        ("arm64", "kustomize_linux_arm64_tarball_sha256", "kustomize_v{version}_linux_arm64.tar.gz"),
    ],
)
def test_kustomize_tarball_pin_matches_upstream_checksums(
    versions: dict[str, str],
    arch: str,
    pin_key: str,
    artifact_suffix: str,
) -> None:
    version = versions["kustomize_version"]
    pin = versions[pin_key]
    url = (
        "https://github.com/kubernetes-sigs/kustomize/releases/download/"
        f"kustomize%2Fv{version}/checksums.txt"
    )
    name = artifact_suffix.format(version=version)
    upstream = _checksum_map(_fetch_text(url))[name]
    assert pin == upstream


@pytest.mark.parametrize(
    ("arch", "pin_key", "artifact_suffix"),
    [
        ("amd64", "kubeconform_linux_amd64_tarball_sha256", "kubeconform-linux-amd64.tar.gz"),
        ("arm64", "kubeconform_linux_arm64_tarball_sha256", "kubeconform-linux-arm64.tar.gz"),
    ],
)
def test_kubeconform_tarball_pin_matches_upstream_checksums(
    versions: dict[str, str],
    arch: str,
    pin_key: str,
    artifact_suffix: str,
) -> None:
    version = versions["kubeconform_version"]
    pin = versions[pin_key]
    url = f"https://github.com/yannh/kubeconform/releases/download/v{version}/CHECKSUMS"
    upstream = _checksum_map(_fetch_text(url))[artifact_suffix]
    assert pin == upstream


@pytest.mark.parametrize(
    ("arch", "tar_arch", "pin_key", "artifact_suffix"),
    [
        (
            "amd64",
            "x64",
            "gitleaks_linux_amd64_tarball_sha256",
            "gitleaks_{version}_linux_x64.tar.gz",
        ),
        (
            "arm64",
            "arm64",
            "gitleaks_linux_arm64_tarball_sha256",
            "gitleaks_{version}_linux_arm64.tar.gz",
        ),
    ],
)
def test_gitleaks_tarball_pin_matches_upstream_checksums(
    versions: dict[str, str],
    arch: str,
    tar_arch: str,
    pin_key: str,
    artifact_suffix: str,
) -> None:
    version = versions["gitleaks_version"]
    pin = versions[pin_key]
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/v{version}/"
        f"gitleaks_{version}_checksums.txt"
    )
    name = artifact_suffix.format(version=version)
    upstream = _checksum_map(_fetch_text(url))[name]
    assert pin == upstream


@pytest.mark.parametrize(
    ("arch", "pin_key", "artifact_suffix"),
    [
        ("amd64", "sops_linux_amd64_binary_sha256", "sops-v{version}.linux.amd64"),
        ("arm64", "sops_linux_arm64_binary_sha256", "sops-v{version}.linux.arm64"),
    ],
)
def test_sops_binary_pin_matches_upstream_checksums(
    versions: dict[str, str],
    arch: str,
    pin_key: str,
    artifact_suffix: str,
) -> None:
    version = versions["sops_version"]
    pin = versions[pin_key]
    url = (
        f"https://github.com/getsops/sops/releases/download/v{version}/"
        f"sops-v{version}.checksums.txt"
    )
    name = artifact_suffix.format(version=version)
    upstream = _checksum_map(_fetch_text(url))[name]
    assert pin == upstream


@pytest.mark.parametrize(
    ("arch", "age_pin", "keygen_pin"),
    [
        (
            "amd64",
            "age_linux_amd64_binary_sha256",
            "age_keygen_linux_amd64_binary_sha256",
        ),
        (
            "arm64",
            "age_linux_arm64_binary_sha256",
            "age_keygen_linux_arm64_binary_sha256",
        ),
    ],
)
def test_age_binary_pins_match_extracted_release_tarball(
    versions: dict[str, str],
    arch: str,
    age_pin: str,
    keygen_pin: str,
) -> None:
    """age publishes no checksum manifest for binaries; hash extracted release tarballs."""
    version = versions["age_version"]
    url = (
        f"https://github.com/FiloSottile/age/releases/download/v{version}/"
        f"age-v{version}-linux-{arch}.tar.gz"
    )
    with tempfile.TemporaryDirectory(prefix="jarvis-age-checksum-") as tmp:
        tarball = Path(tmp) / "age.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as response, tarball.open(
            "wb"
        ) as handle:
            handle.write(response.read())
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extract("age/age", path=tmp)
            archive.extract("age/age-keygen", path=tmp)
        assert versions[age_pin] == _sha256_file(Path(tmp) / "age" / "age")
        assert versions[keygen_pin] == _sha256_file(Path(tmp) / "age" / "age-keygen")

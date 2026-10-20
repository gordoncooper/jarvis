"""Shared constants for envs/<env>/flux entry (p0a-14)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# D11: Renovate stale pin — operator/chart release one behind current upstream.
FLUX_OPERATOR_CHART_PIN = "0.60.0"
FLUX_OPERATOR_CHART_CURRENT = "0.61.0"

FLUX_KUSTOMIZATION_CHAIN: tuple[str, ...] = (
    "platform-controllers",
    "platform-configs",
    "services",
    "apps",
)

FLUX_KUSTOMIZATION_PATHS: dict[str, str] = {
    "platform-controllers": "./platform/flux-operator",
    "platform-configs": "./envs/{env}/flux/platform-configs",
    "services": "./services",
    "apps": "./apps",
}


def kustomize_build_paths(env: str) -> tuple[str, ...]:
    return (
        f"envs/{env}/flux",
        f"envs/{env}/flux/platform-configs",
        "platform/flux-operator",
        "services",
        "apps",
    )

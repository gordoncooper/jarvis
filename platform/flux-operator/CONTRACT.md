# CONTRACT: platform-flux-operator

## Offers

Flux Operator Helm release in `flux-system`, reconciled by Flux after the one-time Ansible `kubectl apply` bootstrap (R1).

## Expects

- `flux-system` namespace and Flux controllers from `FluxInstance`.
- OCI chart `ghcr.io/controlplaneio-fluxcd/charts/flux-operator` at the pinned tag (D11).

## Health

`HelmRelease/flux-operator` reports Ready; deployment `flux-operator` is Available in `flux-system`.

## Config

Chart tag in `oci-repository.yaml` (Renovate-managed after p0a-15). Helm values use upstream defaults unless overridden here.

## Proven by

`tools/tests/test_flux_entry.py` (chart pin, substitute disabled, kustomize render + schema validation).

# CONTRACT: platform-flux-operator

## Offers

Flux Operator Helm release in `flux-system`, reconciled by Flux after the one-time Ansible `helm upgrade --install` bootstrap (R1).

## Expects

- `flux-system` namespace and Flux controllers from `FluxInstance`.
- OCI chart `ghcr.io/controlplaneio-fluxcd/charts/flux-operator` at the pinned tag (D11).

## Health

`HelmRelease/flux-operator` reports Ready; deployment `flux-operator` is Available in `flux-system`.

## Config

Chart tag in `oci-repository.yaml` (`spec.ref.tag`) is the one pin. The flux-operator Ansible role reads it (Renovate-managed after p0a-15). Helm values use upstream defaults unless overridden here.

## Proven by

`ci/tests/test_flux_operator_install.py` (Ansible/Git pin alignment, helm CLI argv), `ci/tests/test_flux_entry.py` (substitute disabled, kustomize render + schema validation).

# CONTRACT: platform-flux-operator

## Offers

Flux Operator Helm release in `flux-system`, reconciled by Flux after the one-time Ansible `helm upgrade --install` bootstrap.

## Expects

- `flux-system` namespace and Flux controllers from `FluxInstance`.
- OCI chart `ghcr.io/controlplaneio-fluxcd/charts/flux-operator` at the pinned tag.
- Each env's FluxInstance sets `cluster.multitenant: true`. kustomize-controller and helm-controller then refuse a reference into another namespace and run an object that names no service account as its namespace's `default` account. The root sync the FluxInstance generates runs as kustomize-controller.
- Every Flux Kustomization and HelmRelease names `spec.serviceAccountName`. The accounts live in `envs/<env>/flux/identities.yaml`, which the root sync applies, so no Kustomization creates the account it runs as. `flux-platform` (bound to `cluster-admin`) runs `platform-controllers`, `platform-configs` and the HelmReleases on their paths, this release included. `flux-services` and `flux-apps` may each write only their own placeholder ConfigMap in `default`, and no ClusterRoleBinding names them. `ci/checks/flux-identity` enforces the lockdown flag, the named accounts, the platform allow-list and in-namespace references.

## Health

`HelmRelease/flux-operator` reports Ready; deployment `flux-operator` is Available in `flux-system`.

## Config

Chart tag in `oci-repository.yaml` (`spec.ref.tag`) is the one pin. The flux-operator Ansible role reads it. Renovate manages that tag (`renovate.json`). Helm values use upstream defaults unless overridden here.

The bootstrap Helm CLI is pinned by version and archive sha256 in the role defaults. The bootstrap install runs only when the operator Deployment is absent, and just before it a binary at `helm_binary_path` that is missing or reports another version is replaced. Once the operator exists, Helm is neither used nor checked. Every run waits for the FluxInstance CRD and the operator rollout before it applies the FluxInstance.

## Proven by

`ci/tests/test_flux_operator_install.py` (Ansible/Git pin alignment, helm CLI argv), `ci/tests/test_flux_operator.py` (Helm version replacement, waits on a re-run, the sops-age identity and refusals), `ci/tests/test_flux_entry.py` (substitute disabled, kustomize render + schema validation), `ci/tests/test_flux_identity.py` (the identity check's failing cases).

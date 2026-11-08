# CONTRACT: platform-flux-operator

## Offers

Flux Operator Helm release in `flux-system`, reconciled by Flux after the one-time Ansible `helm upgrade --install` bootstrap.

## Expects

- `flux-system` namespace and Flux controllers from `FluxInstance`. The FluxInstance has no `sync`; it installs the controllers only.
- OCI chart `ghcr.io/controlplaneio-fluxcd/charts/flux-operator` at the pinned tag.
- Each env's `flux/sync.yaml` declares the root `GitRepository` `flux-system` (the env repository, `flux_git_repo_url` at `flux_git_ref`), the core `GitRepository` `core` (`core_git_repo_url` at `core_git_ref`, public, no secret), and the root Kustomization `flux-system`, which applies `./envs/<env>/flux`. The root source includes `core` at `core/`, so core paths are `./core/...` and one decryptor serves env and core files. An `ssh://` root names `secretRef: flux-deploy-key`. Ansible applies `sync.yaml` once on a new cluster; the root Kustomization then reconciles it.
- Each env's FluxInstance sets `cluster.multitenant: true`. kustomize-controller and helm-controller then refuse a reference into another namespace and run an object that names no service account as its namespace's `default` account. The root Kustomization names `kustomize-controller`, the controller's own identity, as the operator's own sync does under that flag.
- Every other Flux Kustomization and HelmRelease names `spec.serviceAccountName`. The accounts live in core's `flux/identities.yaml`, which each env's root Kustomization applies with core's Flux base, so no Kustomization creates the account it runs as. `flux-platform` (bound to `cluster-admin`) runs `platform-controllers`, `platform-configs` and the HelmReleases on their paths, this release included, and `services`, which applies each enabled add-on's namespaces, tenant account `addon-<name>` and RoleBindings. An add-on's own Kustomizations run as its tenant account, which holds Roles in the add-on's namespaces only ([services/CONTRACT.md](../../services/CONTRACT.md)). Each hosted app's wiring, `app-<name>-wiring` (rendered into the env's `generated/apps.yaml`), runs as `flux-platform` and applies only `./core/platform/optional/app-hosting/app`; the app's own Kustomizations run as its tenant `app-<name>`, which holds a Role in `app-<name>` only ([platform/optional/app-hosting/CONTRACT.md](../optional/app-hosting/CONTRACT.md)). No ClusterRoleBinding names a tenant. Only Kustomizations in `flux-system` decrypt. `ci/checks/flux-identity` enforces the lockdown flag, the named accounts, the root's controller identity, the platform allow-list, decryption in `flux-system` only and in-namespace references; `ci/checks/addons` enforces the add-on tenant's scope.
- `flux-system/flux-deploy-key` for an `ssh://` root: `identity` from the env's sealed `secrets/flux-deploy-key.sops.yaml`, and `known_hosts` from the env's `flux_known_hosts` (cluster-vars), or the role default, GitHub's published SSH host key, when the env leaves it unset. Ansible creates it once, with reconcile disabled. An env repository on another forge sets `flux_known_hosts` to that host's key; env-schema refuses an `ssh://` root whose host has no key there. When GitHub rotates its SSH host key, the `api.github.com/meta` test fails first; update the role default (and any env that sets the old key) to the new published key, then delete and recreate `flux-system/flux-deploy-key` (re-run the flux-operator role), because the existing Secret is not reconciled. A compromised pod with that secret can read the env repository and nothing else.

## Health

`HelmRelease/flux-operator` reports Ready; deployment `flux-operator` is Available in `flux-system`. Both `GitRepository` objects report Ready.

## Config

Chart tag in `oci-repository.yaml` (`spec.ref.tag`) is the one pin. The flux-operator Ansible role reads it. Renovate manages that tag (`renovate.json`). Helm values use upstream defaults unless overridden here.

The bootstrap Helm CLI is pinned by version and archive sha256 in the role defaults. The bootstrap install runs only when the operator Deployment is absent, and just before it a binary at `helm_binary_path` that is missing or reports another version is replaced. Once the operator exists, Helm is neither used nor checked. Every run waits for the FluxInstance CRD and the operator rollout. While the root `GitRepository` is absent, the role sets the rebuild hold, applies the FluxInstance, waits for the source and kustomize CRDs, then applies `sync.yaml`. Once it exists, the role applies neither.

## Proven by

`ci/tests/test_flux_operator_install.py` (Ansible/Git pin alignment, helm CLI argv), `ci/tests/test_flux_operator.py` (Helm version replacement, waits on a re-run, the sops-age identity and refusals, the fresh-cluster hold, FluxInstance and `sync.yaml` order, the deploy-key secret created once, the pinned GitHub host key), `ci/tests/test_flux_entry.py` (substitute disabled, kustomize render + schema validation), `ci/tests/test_flux_identity.py` (the identity check's failing cases), `ci/tests/test_env_schema.py` (the source pair and the deploy-key rule).

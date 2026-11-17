# CONTRACT: platform-flux-operator

The Flux Operator and the Flux controllers it installs, bootstrapped once by Ansible and then reconciled by Flux.

## Offers

| What | Detail |
| --- | --- |
| HelmRelease `flux-system/flux-operator` | OCI chart `ghcr.io/controlplaneio-fluxcd/charts/flux-operator`; its one pin is `spec.ref.tag` in `oci-repository.yaml`, which the Ansible role also reads |
| FluxInstance (`core/flux/flux-instance.yaml`) | The controllers only, no `sync`; `cluster.multitenant: true`, so a cross-namespace reference is refused and an object naming no account runs as its namespace's `default` |
| Accounts (`core/flux/identities.yaml`) | `flux-platform` (`cluster-admin`) runs `platform-controllers`, `platform-configs`, their HelmReleases, `services` and each `app-<name>-wiring`. Add-on tenants `addon-<name>` and app tenants `app-<name>` hold Roles in their own namespaces only. The root Kustomization runs as `kustomize-controller`. No ClusterRoleBinding names a tenant; only `flux-system` decrypts. |
| `flux-system/flux-deploy-key` (an `ssh://` root) | `identity` from `secrets/flux-deploy-key.sops.yaml`; `known_hosts` from `flux_known_hosts`, or the role default, GitHub's published key. Created once by Ansible, not reconciled. Reads the env repository only. |
| Bootstrap (flux-operator role) | Installs the chart with a pinned Helm CLI (version and archive sha256) only while the operator Deployment is absent. While the root GitRepository is absent: sets the rebuild hold, applies the FluxInstance, then the env's `sync.yaml`. Afterwards applies neither. |

## Expects

| What | From |
| --- | --- |
| `flux/sync.yaml`: GitRepository `flux-system` (`flux_git_repo_url` at `flux_git_ref`, including `core` at `core/`), GitRepository `core` (`core_git_repo_url` at `core_git_ref`, public), root Kustomization `flux-system` applying `./envs/<env>/flux` with `postBuild.substitute: {jarvis_env: <env>}` | `jarvis render` |
| Every other Kustomization and HelmRelease names `spec.serviceAccountName` | `flux-identity` (`jarvis validate`) |

## Health

| Check | Pass |
| --- | --- |
| HelmRelease `flux-operator`, Deployment `flux-operator` | Ready, Available |
| GitRepositories `flux-system`, `core` | Ready |

When GitHub rotates its SSH host key, update the role default and any env that sets the old key, then re-run the flux-operator role to recreate `flux-deploy-key`.

# CONTRACT: platform configs

The one list of core components on the `platform-configs` Flux stage, joined once rather than once per env.

## Offers

| What | Detail |
| --- | --- |
| `kustomization.yaml` | `backups`, `cert-manager/config`, `testapp`, `kyverno/rules`, in render order |

## Expects

| What | From |
| --- | --- |
| Each env's `flux/platform-configs/kustomization.yaml` lists `core/platform/configs`, the env's SOPS secrets and its features, nothing else | `jarvis render` |
| Kustomization `platform-configs` decrypts with `sops-age`, substitutes from `cluster-vars`, and depends on `platform-controllers`, which health-checks HelmReleases `cert-manager`, `kyverno`, `volsync` (15m) | core's Flux layers |
| The CRDs these objects use, from `platform-controllers`; nothing here is also on that path | `core/platform/kustomization.yaml` |

## Health

| Check | Pass |
| --- | --- |
| Kustomization `platform-configs` | Ready |

Moving an object between this list and another Flux path takes two commits: add and reconcile, then remove.

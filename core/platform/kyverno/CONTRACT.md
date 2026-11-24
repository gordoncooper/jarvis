# CONTRACT: kyverno

Kyverno generates the backup objects and, with app hosting on, hosted apps' derived objects and priority class, and admits core's workloads by core's rules. CEL policies only (`policies.kyverno.io/v1`); no ClusterPolicy or PolicyException. Hosted apps' admission is native ValidatingAdmissionPolicies.

## Offers

| What | Detail |
| --- | --- |
| HelmRelease `kyverno` in namespace `kyverno` | OCI chart `ghcr.io/kyverno/charts/kyverno`, pinned in `oci-repository.yaml`; admission and background controllers only; reports and cleanup off; images digest-pinned |
| Core's rules, `rules/` on `platform-configs` | ValidatingPolicies `images-pinned`, `resources`, `probes`, `load-balancers` (`rules/rules.yaml`), Deny, failure policy Fail, in namespaces labelled `jarvis.io/layer` `platform` or `feature`. At admission only (`rules/kustomization.yaml`), an object with an owner is skipped, and `probes` skips a pod that does not restart. A feature adds its own exception from its Component. `jarvis validate` applies the rules as the env's build makes them, without the namespace scope, to every object an env deploys but a hosted app's (the app rules hold those), with the Git-only rules (`jarvis/checks/git-rules.yaml`: `backups`, and `flux-prune`, which keeps Flux from pruning what its Kustomizations apply of the protected kinds) |
| Aggregated ClusterRoles | `kyverno:backup-generate` manages Secrets and ReplicationSources and lists ConfigMaps cluster-wide (the hold and overrides); `kyverno:app-derive` (app hosting on) reads Apps and manages NetworkPolicies and ResourceQuotas. The chart's own roles are not edited, and no role updates Namespaces. |

## Expects

| What | From |
| --- | --- |
| Reconciled in `platform-controllers`, before the policies in `platform-configs` | core's Flux layers |
| Each core namespace labelled `jarvis.io/layer` and `pod-security.kubernetes.io/enforce` in its own manifest | the components |

## Health

| Check | Pass |
| --- | --- |
| Deployment `kyverno-admission-controller` | Ready |
| `jarvis verify --smoke` (platform) | a copy of testapp's running pod is admitted, refused by `images-pinned` once unpinned and by Pod Security once privileged |

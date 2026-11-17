# CONTRACT: kyverno

Kyverno generates the backup objects and, with app hosting on, hosted apps' derived objects and priority class. CEL policies only (`policies.kyverno.io/v1`); no ClusterPolicy. Hosted apps' admission is native ValidatingAdmissionPolicies.

## Offers

| What | Detail |
| --- | --- |
| HelmRelease `kyverno` in namespace `kyverno` | Chart 3.9.2 (v1.19.2), admission and background controllers only; reports and cleanup off; images digest-pinned |
| Aggregated ClusterRoles | `kyverno:backup-generate` manages Secrets and ReplicationSources and lists ConfigMaps cluster-wide (the hold and overrides); `kyverno:app-derive` (app hosting on) reads Apps and manages NetworkPolicies and ResourceQuotas. The chart's own roles are not edited, and no role updates Namespaces. |

## Expects

| What | From |
| --- | --- |
| Reconciled in `platform-controllers`, before the policies in `platform-configs` | core's Flux layers |

## Health

| Check | Pass |
| --- | --- |
| Deployment `kyverno-admission-controller` | Ready |

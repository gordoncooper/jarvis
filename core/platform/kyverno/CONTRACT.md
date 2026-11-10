# CONTRACT: kyverno

Kyverno 1.19 generates the backup objects (`ci/checks/pvc-backup`) and, in an env with app hosting on, the hosted apps' derived objects (`app-derive`) and their priority class (`app-priority`). CEL policies only (`policies.kyverno.io/v1`: GeneratingPolicy, MutatingPolicy); no ClusterPolicy, so Kyverno 1.20, which drops it, needs no port. The hosted apps' admission rules are native ValidatingAdmissionPolicies and need no Kyverno.

## Offers

- Namespace `kyverno`.
- Helm release `kyverno` (chart 3.9.2, app v1.19.2). Admission controller and background controller only.
- Aggregated ClusterRoles so those controllers can manage Secrets and ReplicationSources and list ConfigMaps.

## Expects

- Flux `platform-controllers` reconciles this directory before `platform-configs`.
- The backup GeneratingPolicies live under `core/platform/backups/` on the platform-configs path.

## Health

Deployment `kyverno-admission-controller` in `kyverno` is Ready. That is the gate for this install. The admission container sets a memory request of `128Mi` and limit of `384Mi`, and a CPU request of `100m` with no CPU limit. The background controller sets a memory request of `64Mi` and limit of `128Mi`, and a CPU request of `100m` with no CPU limit.

## Config

- Reports controller and cleanup controller are off. Report creation flags are off with them.
- Images for the admission container, its init container, and the background controller are digest-pinned. Renovate updates the chart version, those digests and the CLI pin in `mise.toml` (with its `mise.lock` checksums) in one PR.
- Chart `kubeVersion` is `>=1.25.0-0`. Upstream tests v1.19 on Kubernetes 1.33–1.35. The v1.19.2 module builds against `k8s.io/client-go` v0.36. The chart does not refuse 1.36.
- Permissions are extra aggregated ClusterRoles. The chart's own roles are not edited.
- `kyverno:backup-generate` lists ConfigMaps cluster-wide: the enrollment policies list `kube-system` and `platform-backups` for the rebuild hold and the overrides, and a list cannot be narrowed by name. No Kyverno role updates Namespaces; `volsync.backube/privileged-movers` is set in Namespace manifests.
- In an env that turns app hosting on, `kyverno:app-derive` (from the app-hosting Component) lets both controllers read hosted apps' Apps and manage NetworkPolicies and ResourceQuotas, for GeneratingPolicies `app-derive` and `app-derive-backups` ([core/feature/app-hosting/CONTRACT.md](../../feature/app-hosting/CONTRACT.md)), which writes them only in an app's own namespace.

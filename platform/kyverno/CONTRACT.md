# CONTRACT: kyverno

Kyverno 1.19 admits and generates the backup policy (`ci/checks/pvc-backup`). Classic ClusterPolicy only.

## Offers

- Namespace `kyverno`.
- Helm release `kyverno` (chart 3.9.2, app v1.19.2). Admission controller and background controller only.
- Aggregated ClusterRoles so those controllers can manage Secrets and ReplicationSources, and the background controller can update Namespaces.

## Expects

- Flux `platform-controllers` reconciles this directory before `platform-configs`.
- The backup ClusterPolicy lives under `platform/backups/` on the platform-configs path.

## Health

Deployment `kyverno-admission-controller` in `kyverno` is Ready. That is the gate for this install.

## Config

- Reports controller and cleanup controller are off. Report creation flags are off with them.
- Images for the admission container, its init container, and the background controller are digest-pinned. Renovate updates the chart version and those digests.
- Chart `kubeVersion` is `>=1.25.0-0`. Upstream tests v1.19 on Kubernetes 1.33–1.35. The v1.19.2 module builds against `k8s.io/client-go` v0.36. The chart does not refuse 1.36.
- Permissions are extra aggregated ClusterRoles. The chart's own roles are not edited.
- The background controller's chart role can get Namespaces and cannot update them. `kyverno:backup-namespaces` adds `update` and `patch` so the enrollment policy can set `volsync.backube/privileged-movers`.

## Proven by

- `ci/tests/test_backup_enrollment.py` (controllers off, digest pins, aggregated roles, policy rules).
- `ci/tests/test_flux_entry.py` (kustomize render and schema validation).

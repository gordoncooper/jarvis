# Changelog

One section per release, newest first. A minor release lists what an env must do to upgrade
under **Upgrading**. An env pins core with `core_git_ref: refs/tags/<tag>` in
`values/cluster-vars.yaml` and the core `GitRepository` in `flux/sync.yaml`; moving the pin is
the upgrade.

## Unreleased

### Added

- `core_git_ref` pins a tag. `make site`, `verify`, `drill` and `restore` refuse a checkout that is
  not the env's tag (another commit, uncommitted changes, or a branch ref); `ALLOW_UNPINNED=1`
  warns instead.
- A pushed `v*` tag publishes a GitHub release whose notes are that tag's section here.

### Upgrading

- Set `core_git_ref` (cluster-vars) and the core `GitRepository` `spec.ref.name` (`flux/sync.yaml`)
  to `refs/tags/<tag>`, and run tools from a checkout of that tag.

## v0.2.0

### Changed

- Kyverno policies are CEL types (`policies.kyverno.io/v1`): GeneratingPolicies `pvc-backup`,
  `pvc-backup-app`, `app-derive`, `app-derive-backups` and MutatingPolicy `app-priority`. No
  ClusterPolicy remains, so Kyverno 1.20 needs no port.
- App namespaces set `volsync.backube/privileged-movers` themselves; `kyverno:backup-namespaces` is gone.
- The agent rulebook is `agents/coding-agents.md`; the deny lists are one short list per client.
- pytest runs on every CPU (`pytest-xdist`).

### Removed

- The `apps` Flux layer (`core/apps`, `kustomizations/apps.yaml`, `flux-apps`) and the placeholder
  ConfigMaps.
- `llm_provider_host`, the chat-bot slot contract, and the planned code-delivery rows.
- `ci/checks/core-names`, `plan-codes` and `cli-deny`, and `tools/make-plan`. CI no longer needs `OWNER_NAME`.

### Upgrading

- Delete `flux/kustomizations/apps.yaml`, the `flux-apps` objects in `flux/identities.yaml` and
  `llm_provider_host`, then re-render (`tools/lib/render_switches.py --env <env>`).
- Delete any sealed file nothing reads; env-schema now fails one.

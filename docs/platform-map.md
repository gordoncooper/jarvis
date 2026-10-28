# Platform map

Point-in-time read of this repo on 2026-10-28 (Pacific). Git only: no SSH, no kubectl, no secret decryption.

Two trees:

- `main` at `46a47e5f`. This note is written to merge there.
- The checkout the map was taken from, `3972e7b4` on `cursor/restic-rest-server-ecad`, three commits ahead of `main`. Those commits add the restic rest-server and stop privileged VolSync movers. They are not on `main` yet. Sections below say which tree they describe.

`services/` and `apps/` are placeholders. Most names in the env README (cert-manager, App DNS, Gatus, Grafana, VictoriaMetrics, VictoriaLogs, Hermes, hosted apps) have no manifests.

## Verdict

The deploy graph is a line. It is not a spider web, and it does not need a modularity rewrite.

What is already isolated is the part worth protecting: one folder per platform component, env values only under `envs/`, a four-step Flux `dependsOn` chain, secrets decrypted only on the platform-configs step, and version pins living in the component file that uses them.

The web shows up later, if the next services copy four things into every folder:

1. A VolSync `ReplicationSource` (the backup API leaking into every PVC).
2. `ingressClassName: traefik` (the ingress API leaking into every Ingress).
3. Secret key shapes inside `tools/bootstrap-secrets` (one script that already knows cert-manager, Grafana, both Hermes deploys, and restic).
4. More controllers appended inside `platform/flux-operator/kustomization.yaml` (the Flux operator folder becoming the platform index).

I would not build abstractions for any of those now. There is no second implementation to hide, and there are no Ingresses or PVCs yet. The useful work is to keep the next component PRs inside the seams that already exist.

## Deploy tree

Parent means "this step applies or owns the child." `dependsOn` is the only runtime ordering between Flux kustomizations.

```
envs/<env>/values/cluster-vars.yaml
envs/<env>/inventory/hosts.yml
infra/inventory/roles.yml
        │
        ▼
infra/ansible/site.yml
├── base-os                         packages, chrony, journald, apt policy, zoneinfo
├── node-disk                       only hosts with data_disk; drop-in for the k3s unit
├── k3s                             server on control_plane, agent on every other host
│   ├── bundled Traefik chart       not disabled in config.yaml.j2
│   ├── ServiceLB                   edge role sets svccontroller.k3s.cattle.io/enablelb
│   ├── local-path                  local_path_data
│   ├── CoreDNS
│   └── etcd snapshots              <backup_path>/etcd-snapshots (same-node rollback)
├── node labels                     role.<dns_suffix>/<role> ; applied from the control plane
├── kubeconfig
└── flux-operator role              one-time bootstrap, then Git owns the objects
    ├── chart tag                   read from platform/flux-operator/oci-repository.yaml
    ├── flux-system/sops-age
    └── FluxInstance                rendered from the role template
            │
            │  Git copy: envs/<env>/flux/flux-instance.yaml
            │  sync path: envs/<env>/flux
            ▼
        FluxInstance
        ├── platform-controllers          path ./platform/flux-operator
        │   ├── flux-operator HelmRelease  OCI chart pin in that folder
        │   ├── Traefik HelmChartConfig    the only Git object in kube-system
        │   └── VolSync controller         platform/backups/controller
        ├── platform-configs               dependsOn platform-controllers; SOPS
        │   ├── platform/backups           namespace, and on main nothing else
        │   └── backups-restic secret      envs/<env>/secrets
        ├── services                       dependsOn platform-configs
        │   └── placeholder ConfigMap
        └── apps                           dependsOn services
            └── placeholder ConfigMap

tools/bootstrap-secrets          not in the Flux chain; create-if-missing SOPS files
├── cert-manager-ca              namespace platform-cert-manager (no workload yet)
├── backups-restic               namespace platform-backups
├── grafana-admin                namespace observability (no workload yet)
└── hermes-ops-* and
    hermes-code-factory-*        namespaces hermes-ops, hermes-code-factory (no workload yet)
```

On `3972e7b4` the platform-configs half of backups also includes `platform/backups/rest-server.yaml`: one restic rest-server Deployment and ClusterIP Service, pinned to the backup-role node, data on `<backup_path>/restic`. `main` does not have that file. On `main`, `platform/backups` is the namespace plus the secret, and the namespace sets `volsync.backube/privileged-movers` so movers mount the repo hostPath themselves.

## What is built

| Piece | Where it is configured | State |
| --- | --- | --- |
| base-os | `infra/ansible/roles/base-os` | Built. Play on `all`. |
| node-disk | `infra/ansible/roles/node-disk` | Built. Play matches no hosts when no `data_disk` is set. |
| k3s | `infra/ansible/roles/k3s` | Built. Version, checksum, CIDRs, kubelet reservations, and `local_path_data` come from cluster-vars. etcd snapshot cron and retention are role defaults. |
| kubeconfig | `infra/ansible/roles/kubeconfig` | Built. |
| Flux operator | `platform/flux-operator` and `infra/ansible/roles/flux-operator` | Built. Ansible installs once; Flux reconciles the HelmRelease after that. Distribution `2.8.8` is pinned in the role default and in each env `flux-instance.yaml`. Chart tag `0.60.0` has one pin, the OCIRepository, which Ansible reads. |
| Traefik | `platform/traefik/helmchartconfig.yaml` | Built as config only. k3s ships the chart. Gateway API is off. Ingress class `traefik` is a manifest constant. |
| VolSync | `platform/backups/controller` | Built. Chart `0.17.0`, restic mover, `copyMethod: Direct`. Vendored VolumeSnapshot CRD, no snapshot controller. Applied from the flux-operator kustomization, not from `platform/backups/kustomization.yaml`. |
| restic rest-server | `platform/backups/rest-server.yaml` | On `3972e7b4` only. Not on `main`. |
| restic credentials | `envs/<env>/secrets/backups-restic.sops.yaml` | Present for env-01. Shape is minted by `tools/bootstrap-secrets`. |
| services | `services/placeholder.yaml` | Placeholder. |
| apps | `apps/placeholder.yaml` | Placeholder. `apps_git_repo_url` is validated and unused. |
| cert-manager | secret only | Named. Traefik already points TLS at namespace `platform-cert-manager`. |
| App DNS, Gatus | exposure contract | Named. No manifests. |
| Grafana, VictoriaMetrics, VictoriaLogs | secret, and a CI allowlist for `backup: none` | Named. No manifests. |
| Hermes | `values/hermes.yaml` plus four secrets | Named. Bots and model maps are empty. No `services/hermes/`. |
| Code delivery | `docs/code-delivery/CONTRACT.md` | Contract only. `code_delivery` is validated and no manifest reads it. |

## Where a setting lives

| Kind of value | Home | Who reads it |
| --- | --- | --- |
| Per-env facts (zone, DNS suffix, CIDRs, backup path and retention, git URL and ref, kubelet reservations, NTP, k3s pin) | `envs/<env>/values/cluster-vars.yaml` | Ansible, via `jarvis_cluster_vars`. Flux postBuild, via the generated `cluster-vars` ConfigMap. Unknown keys fail `ci/checks/env-schema`. |
| Hermes tiers and app hosting flags | `values/hermes.yaml`, `values/apps.yaml` | Schema only, until those services exist. |
| Host roles | `inventory/hosts.yml` plus `infra/inventory/roles.yml` | Ansible groups. Kubernetes labels are `role.<dns_suffix>/<role>`. |
| Component version pins | The manifest or role default that uses the pin | Renovate, with a custom manager per file. Pins are not cluster-vars keys. |
| Bootstrap credentials | `envs/<env>/secrets/*.sops.yaml` | Flux decrypts them only on platform-configs. |

That split is the modularity. A value that differs per deployment is an env setting. A pin that is the same for every env stays next to the component. I would keep both rules.

## Cross-links

These are the only edges that are not parent/child. They are the swap surface.

| From | To | What is hardcoded |
| --- | --- | --- |
| `platform/flux-operator/kustomization.yaml` | Traefik and VolSync | The controller index lives inside the Flux operator folder. The Flux path is `./platform/flux-operator`. |
| k3s role | bundled Traefik | `config.yaml.j2` does not disable Traefik. The chart is whatever that k3s pin ships. |
| Traefik HelmChartConfig | `platform-cert-manager` | `--providers.kubernetesCRD.defaultTLSResourcesNamespace=platform-cert-manager`. The namespace has a secret and no workload. |
| k3s labels | Traefik Service | `edge` is the only role that receives the ServiceLB label. |
| node-disk | k3s unit name | Drop-in is `k3s.service.d` or `k3s-agent.service.d`. |
| VolSync HelmRelease on `3972e7b4` | base-os zoneinfo | postRenderer sets `TZ` from `time_zone` and mounts `/usr/share/zoneinfo`. Not on `main`. |
| rest-server on `3972e7b4` | backup role label and `backup_path` | nodeSelector and hostPath use postBuild. The Service DNS name does not. |
| `tools/bootstrap-secrets` | rest-server URL on `3972e7b4` | The repository prefix is a constant in that script. On `main` the secret still points movers at a hostPath. |
| `ci/checks/lib/_pvc_backup.py` | VolSync | Every PVC must have a `ReplicationSource` or an allowed `backup: none`. The allowlist names Victoria folders that do not exist yet. |
| FluxInstance git file | Ansible template | Two copies. `ci/tests/test_flux_entry.py` fails if they differ. |

## Decisions

Each one is a change I considered. None of them should land as a cleanup PR.

### 1. Controller index stays in the Flux operator folder until the next controller

**Recommendation.** Leave `platform/flux-operator/kustomization.yaml` as the list. When cert-manager (or App DNS) is added, move that list to `platform/kustomization.yaml` in the same PR and point `platform-controllers` at `./platform`.

**Why change it at all.** The folder named for the operator also includes Traefik and the VolSync controller. Adding the next controller means editing the operator folder. Swapping the operator install means touching the file that names every other controller. The comment in that file already calls the list "one Flux path," which is the right idea in the wrong directory.

**Why not now.** The list is four lines. Rendered objects would be identical after a move, but the Flux path in `envs/env-01/flux/kustomizations/platform-controllers.yaml` would change, and `ci/tests/test_traefik.py` builds `platform/flux-operator` to find the HelmChartConfig. A standalone move is a reconcile-path edit with no new behavior, which is a poor trade against a typo in the path.

**Pros of moving now.** The operator folder would contain only the operator. The index would be obvious. Later controllers would be one line in a file that is not an operator manifest.

**Cons of moving now.** Review and rollout risk for a cosmetic split. A second kustomization file before a second caller. The list pattern itself is already the right seam; only its address is awkward.

### 2. Keep Traefik as the k3s bundled chart

**Recommendation.** Keep `HelmChartConfig` in `kube-system`. Do not install a second ingress, and do not add `ingress_class` to cluster-vars.

**Why someone would change it.** The object is `helm.cattle.io` HelmChartConfig, which exists because k3s installed the chart. ServiceLB is a k3s label. The class name `traefik` will be copied into every future Ingress. Swapping to another ingress means disabling the bundled chart (no flag in the template today), replacing ServiceLB, and editing every Ingress. Chart comments already assume chart v40 behavior for the TLS-store namespace flag.

**Why not.** There is one ingress and zero Ingress objects. The config surface is one small HelmChartConfig: class name, Gateway off, resource sizes, LoadBalancer, TLS namespace. Replacing that with a Flux HelmRelease of Traefik, or with an "ingress interface," adds a controller the cluster already runs. The exposure contract already says the class is a manifest constant on purpose.

**Pros of replacing the bundled chart.** Ingress install would look like VolSync: a folder, a pin, a HelmRelease, no cattle.io API. A k3s upgrade would stop being an implicit Traefik upgrade. Disabling the bundled chart would be an explicit k3s flag instead of an inherited default.

**Cons.** Two copies of Traefik during a migration, or a flag to disable the bundled one that must land in lockstep. ServiceLB is still k3s-specific even if the chart moves. An abstraction over the class name has one implementation.

**Related, still do not parameterize.** The string `platform-cert-manager` is already in the HelmChartConfig, in `tools/bootstrap-secrets`, and in `ci/tests/test_traefik.py`. That is a platform constant, same as the ingress class. A cluster-var would spread it into every env file and the schema. The cert-manager PR should use that namespace and the existing `cert-manager-ca` secret, and should not rename either.

### 3. Prefer the rest-server boundary; do not generate ReplicationSources yet

**Recommendation.** Keep the rest-server series (`3972e7b4`) as the backup data plane when it merges. Do not return to privileged hostPath movers. Do not add a generator or a kustomize component for `ReplicationSource` until the same snippet has been copied for real PVCs more than twice.

**Why the rest-server is the better isolation.** On `main`, every mover in `platform-backups` is privileged and mounts the repo hostPath (`RESTIC_REPOSITORY` under `/mnt/repo`). A volume's backup manifest has to know disk layout. On `3972e7b4` the disk stays behind one Deployment. Movers talk to `restic.platform-backups.svc.cluster.local:8000`. The namespace comment says no mover there mounts a hostPath. That is the swap seam: replace rest-server, keep the URL contract, leave VolSync's mover API as the only CRD services see.

**Why not wrap it further.** No PVC exists, so a generator has no caller. The CI check is already the policy (`ReplicationSource` or an allowed `backup: none`). Hiding the CRD behind a code generator makes the common case harder to read, and the repository URL still has to vary per PVC (`ns-<namespace>/pvc-<pvc>` on the rest-server tree). A copied example next to the first PVC, with the URL shape written only in `platform/backups/CONTRACT.md`, is the smaller surface.

**Pros of a component or generator now.** VolSync field names would stay in `platform/backups`. Swapping VolSync later would be one folder plus the CI check, not a hunt through services.

**Cons.** Machinery with nothing to generate. Per-PVC repository URLs and secrets fit a short manifest better than a framework. The CI check should stay VolSync-specific until a second backend exists; generalizing it early just renames the coupling.

**URL constant.** On the rest-server tree the repository prefix is also a Python constant in `tools/bootstrap-secrets`. When the first real volume secret is built, that prefix should be read from one place (the contract's URL, or a helper next to backups), not retyped. Not before that volume exists.

### 4. Leave `bootstrap-secrets` as one list

**Recommendation.** Keep one `_specs()` list. When a service's manifests land, move that service's spec next to the service and leave a one-line registration. Do not split the file in a cleanup.

**Why it is a coupling.** The script mints the internal CA, the restic user, the Grafana admin secret, and both Hermes dashboard hashes. It knows namespaces that have no Deployments. Swapping Grafana's auth scheme or Hermes's password hash means editing the CA generator. Those namespaces are now frozen: `platform-cert-manager`, `platform-backups`, `observability`, `hermes-ops`, `hermes-code-factory`.

**Why not now.** The file is create-if-missing, a few hundred lines, and one caller (`make bootstrap-secrets`). The services it pre-creates secrets for are not in Git. A plugin registry with one caller is indirection. The list is also the readable inventory of bootstrap secrets.

**Pros of splitting now.** Deleting or replacing Hermes would not require reading CA code. Each secret shape would sit in the component folder, which matches "pins live with the component."

**Cons.** Shared helpers (sops encrypt, random tokens) still need a home. More files before the matching Deployments exist. Easy to drift from the committed secrets, which the tool will not rewrite once they exist.

### 5. Keep a single `cluster-vars.yaml`

**Recommendation.** One flat file per env, one generated ConfigMap, `substituteFrom` that ConfigMap. Do not split per component. Do not put image tags or chart versions in it.

**Why someone would split it.** Every new key touches `schema/env.py`, `schema/CONTRACT.md`, the example env, the real env, and the rendered ConfigMap. Backup retention sits beside kubelet reservations. Removing a component is a schema change.

**Why not.** Flux postBuild is one ConfigMap, and strict substitution fails closed when a key is missing. The schema's rejection of unknown keys is the feature that keeps env files from becoming a junk drawer. Component pins already live in component files. The keys that remain are the ones that actually differ per env, or that Ansible and Flux both need.

**Pros of per-component value files.** Deleting backups would delete a backups values file instead of a schema migration. Owners would be obvious.

**Cons.** Several ConfigMaps to substitute, or a merge step that recreates today's file. Ansible vars_files would grow. The example-env drift test would get more complicated. No component yet has enough private keys to justify a second file.

### 6. Keep two FluxInstance copies, locked by the test

**Recommendation.** Keep `infra/ansible/roles/flux-operator/templates/flux-instance.yaml.j2` and `envs/<env>/flux/flux-instance.yaml`. Keep `test_git_flux_instance_matches_ansible_template`.

**Why it looks like drift.** The template is what Ansible applies on first boot. The git file is what Flux reconciles after that, and it is plain YAML with no substitution so a broken operator can be restored with `kubectl apply -f` of that file. The git URL and ref therefore exist both in cluster-vars and in the git file. The distribution version exists in the role default and in the git file. Renovate groups the version. The test renders the template with cluster-vars and fails if the git document differs.

**Why not generate the git file.** Generation adds a third mechanism and a step that must run before commit. The rollback file would no longer be something a human can read without rendering. The test already catches the drift that generation would prevent.

**Pros of generating.** One edit when the component list, cluster size, or feature gate changes.

**Cons.** The bootstrap path and the rollback path become a script. The example env has no `flux/` tree; the test already scopes itself to envs that have the file. A generator would need the same scope and would be easy to skip.

### 7. Treat k3s as the base, not a replaceable module

**Recommendation.** Leave the k3s role, the ServiceLB label, the node-disk unit drop-in, and the etcd snapshot defaults where they are.

**Why it is coupled.** node-disk knows k3s unit names so the data disk is mounted before k3s starts. Labels use a k3s ServiceLB key. etcd snapshots write under `backup_path`, next to where the rest-server will put repositories. The k3s config template does not disable bundled components. Replacing k3s means rewriting those roles. There is no distro interface, and there should not be one.

**Why this is acceptable.** k3s is the substrate the rest of the repo assumes. A fake interface (`cluster_runtime: k3s`) with one arm is how a spider web starts. etcd snapshot cron `0 */12 * * *` and retention `5` are product constants in the role default, documented as the k3s pin's built-in defaults. They are not per-env, so they do not belong in cluster-vars.

**Pros of a distro seam.** A future non-k3s env would not fork `site.yml`.

**Cons.** Every play would branch. Nobody is offering a second distro. The seam would be guesswork about which k3s behaviors are "the interface."

**Operational fact, not a refactor.** env-01 puts every node role, including `backup` and `edge`, on one host. etcd snapshots and the rest-server hostPath then share that machine's disk. The backup contract already says etcd snapshots are same-node rollback only. A rest-server on that same disk has the same failure domain. That is the single-node shape of this env. Fixing it means another disk or another node in inventory, not a code split. This note does not propose that.

### 8. Do not add provider switches

**Recommendation.** Leave `code_delivery` and `build_agent` as schema-validated values that no manifest reads. The apps Flux kustomization should keep path `./apps` until app hosting is actually turned on.

**Why it looks unfinished.** `apps_git_repo_url` is required and unused. The apps kustomization reads this repo, while the code-delivery contract says authoritative app git lives in the apps repo. `docs/build-agent` is a contract root that does not exist on disk yet, so `contract-md` does not demand a file.

**Why not wire them.** The contract says manifests do not branch on `code_delivery` until a second provider ships. Pointing `apps` at the other repo now would sync an empty or foreign tree into the cluster for a flag that is off (`app_hosting_enabled: false` on env-01). A switch with one legal value is dead code.

**Pros of wiring the apps repo now.** The Flux object would match the contract's end state. The placeholder ConfigMap could go away.

**Cons.** A second GitRepository, credentials questions, and a dependsOn edge, before any app manifest. The placeholder is the honest "not built" signal.

## Concerns that are not change requests

- **k3s and Traefik move together.** Nothing in Git pins the bundled chart version except the k3s version pin. The HelmChartConfig comments depend on chart v40 (Service type field, missing TLS-namespace value, so `additionalArguments` is used). A k3s bump can break that config without a Traefik file changing. When the k3s pin moves, the Traefik contract's chart assumptions need a look. I would not pin Traefik separately while it remains the bundled chart.

- **Privileged movers are still the `main` backup model.** `platform/backups/namespace.yaml` sets `volsync.backube/privileged-movers: "true"` for every mover in the namespace. The contract says to revisit that at an NFS target. The rest-server commits are that revisit, aimed at a local rest-server instead of NFS. Merging them removes the privilege. Until they merge, any new `ReplicationSource` on `main` inherits root movers and a hostPath.

- **Namespaces are reserved by secrets before the workloads exist.** Rotating or renaming later means a new secret file, a Traefik arg, and the bootstrap script, while Flux will not delete the old Secret if someone only changes the Git name (the old one was applied). Prefer the names already chosen.

- **Zoneinfo is an implicit package dependency on the rest-server tree.** VolSync's image has no tzdata; the postRenderer mounts the host zone database that base-os installs. If a later base-os slim drops that data, backup crons shift to UTC with the controller still Ready. Worth a sentence in the backup contract when that postRenderer is on `main`. Not worth a probe before any schedule exists.

- **The VolumeSnapshot CRD is vendored** under `platform/backups/controller/` because the operator exits if the kind is unmapped, and local-path has no snapshots. `copyMethod: Direct` does not use it. Replacing VolSync means deleting that CRD with the controller. I would not install a snapshot controller to "complete" the CRD.

- **The PVC check names folders that are not in the tree** (`services/observability/victoria-metrics/`, `services/observability/victoria-logs/`). That is a reservation so those volumes can opt out later. Building empty observability folders to match the check would be the wrong kind of completeness.

- **FluxInstance syncs `envs/<env>/flux`, which then points back at `platform/` and `services/`.** Core manifests stay env-agnostic because substitution fills `dns_suffix`, `backup_path`, and `time_zone`. Identity (git URL, host names) stays under `envs/`. That rule is doing the isolation work. Breaking it to "make the platform folder self-contained" would copy env values into `platform/` and make swap-out worse.

## What I would do on the next platform PRs

1. **cert-manager.** New folder `platform/cert-manager`. One line on the controller list. Same PR may move that list to `platform/kustomization.yaml`. Use namespace `platform-cert-manager` and Secret `cert-manager-ca`. Wildcard certificate and Traefik TLSStore as the exposure contract already describes. No new cluster-vars keys unless a value truly differs per env.

2. **First PVC.** One `ReplicationSource` beside that PVC, copied from the backup contract, aimed at the rest-server URL if that series has merged. Retention keys already come from cluster-vars. No generator.

3. **First Ingress.** One Ingress, class `traefik`, host `<svc>.<dns_suffix>`, as the exposure contract says. The class name written in that manifest and in the Traefik contract. No shared library, no cluster-var. Revisit a shared snippet only after a second Ingress copies it wrong.

I would not add a platform umbrella chart, a service interface, or a distro flag in any of those PRs.

## Opinion of the structure

For a repo this early, the structure is stricter than the amount of software in it, and that is a good trade. Contracts, the env schema, and the Flux chain are doing the job that a plugin system would do worse. The gaps are empty layers (`services/`, `apps/`) and a handful of forward references (TLS namespace, bootstrap secrets, the Victoria opt-out paths), which is normal while phases are still landing.

The failure mode to avoid is copying VolSync, Traefik, and secret layout into every new service because those strings are easy to paste. The failure mode to avoid on the other side is a framework that hides three YAML files. The current folders are small enough to replace one component by replacing one folder, as long as the controller index, the backup URL, and the ingress class stay as single contracts instead of being re-declared per service.

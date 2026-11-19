# CONTRACT: app-hosting

Hosted apps are untrusted: one folder each in the apps repository, deployed into `app-<name>` and held there by admission. A feature, off until `app-hosting` is listed under `features` in `values/switches.yaml`; with it off, the env renders none of the objects below. How to write and turn on an app: [docs/add-a-hosted-app.md](../../../docs/add-a-hosted-app.md).

## Offers

| What | Detail |
| --- | --- |
| The app file | `<name>/app.yaml`, an `App` (`hosting.jarvis.io/v1alpha1`). Its fields, defaults and meaning are the CRD's, `crd/app-crd.yaml`: `network.web` (`port`, `healthPath`), `network.ports` (`listen`, `target`, `protocol`), `network.egress` (`internet`, `lan`, `apps`), `network.callers`, `volumes` (`name`, `backup`), `resources`, `risky`, `envs.<env>`. Every network field defaults closed. `python -m jarvis.apps.check <folder>…` (`--repo <dir>`) also refuses unknown keys; the app-check Action (`.github/actions/app-check@<tag>`) runs it in the apps repository's CI. |
| Hostname | `<name>.apps.<dns_suffix>` on 443, through the apps door: listener `apps` of Gateway `platform-gateway/jarvis`, with Certificate `wildcard-apps` (`*.apps.<dns_suffix>`, internal CA); a granted custom hostname uses listener `apps-granted`. Both admit routes only from labelled app namespaces ([traefik](../../platform/traefik/CONTRACT.md)). The app's HTTPRoute names `${app_host}` and `parentRefs` that Gateway. |
| Per listed app, applied as the platform account by `app-<name>-wiring` | Namespace `app-<name>` (label `hosting.jarvis.io/app: <name>`; annotations: host, env, pod/service/LAN ranges, one `hosting.jarvis.io/<grant>` per exception line, `volsync.backube/privileged-movers: "true"`; Pod Security `warn`/`audit` `restricted`); tenant `flux-system/app-<name>` with Role `app`; NetworkPolicies `default-deny` (`prune: disabled`), `same-namespace`, `cluster-dns`; LimitRange `app` (default 100m/128Mi limits, 50m/128Mi requests); Kustomizations `app-<name>-file` then `app-<name>`, reading only `<name>/app.yaml` and `<name>/deploy/` at the pinned tag; RoleBinding `app-grant-revoke`. |
| Derived from the App (GeneratingPolicies `app-derive`, `app-derive-backups`) | ResourceQuota `app` (`resources`, `services.nodeports: 0`, LoadBalancers up to `network.ports`). Only when asked: `app-web` (Traefik to `web.port`), `app-ports` (granted `lan-port` targets), `app-internet` (any port to public addresses; never RFC 1918, `100.64.0.0/10`, link-local or the env's ranges), `app-lan-all` (asked `lan: [all]` and granted `lan-all`), `app-lan` (asked and granted `lan-target`, on its ports), `app-apps` / `app-callers` (one side each), `app-backups` (to `restic-apps:8000` while the app's backup credential exists). An allow the App stops asking for goes on its next update. |
| Backups (`backups/`) | An app's backups are on once the env seals its credential, `secrets/app-<name>/backups-restic-app-<name>.sops.yaml` (`jarvis secrets --app app-<name>`); the render registers it for each listed app. Secret `platform-backups/backups-restic-app-<name>`: the app's own password and login `ns-app-<name>`, repository `rest:http://restic-apps.platform-backups.svc.cluster.local:8000/ns-app-<name>/pvc-<pvc>`. GeneratingPolicy `pvc-backup-app` gives each bound `app-*` PVC Secret `restic-<pvc>` from that credential only and ReplicationSource `<pvc>` as core's `pvc-backup` does ([backups](../../platform/backups/CONTRACT.md)); with no credential, neither. |
| Apps rest-server | Service `platform-backups/restic-apps`, port 8000: core's image and hostPath, `--private-repos`, so user `ns-app-<name>` works only under `ns-app-<name>/`. Its htpasswd is built at start from each registered credential's `htpasswd` key; the user name comes from the folder, not the sealed line. Never mounts `backups-restic`. |
| Admission (`policies/admission.yaml`, ValidatingAdmissionPolicies, `failurePolicy: Fail`) | `app-identity`: `app-<name>` exactly when labelled `<name>`; an App only in its own namespace. `app-tenant`: the tenant writes only in `app-<name>`, only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, HTTPRoutes, PDBs, HPAs and its App. `app-file` and `app-grants` (one binding each on every labelled namespace): Pod Security `restricted`; volumes only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi, ephemeral; PriorityClass `hosted-app`; memory request equal to limit; PVCs only declared volumes (`backup: none` exactly when `backup: false`), no `volumeName`/`dataSource`/`dataSourceRef`; an HTTPRoute names its hostnames, each `<name>.apps.<dns_suffix>`; Services ClusterIP, or LoadBalancer only on granted `network.ports` with `allocateLoadBalancerNodePorts: false`; no NodePort, ExternalName or `externalIPs`. A risky item, custom hostname or LAN port passes only when the App asks **and** the env grants it. A labelled namespace with no App refuses everything. |
| VolSync carve-out | Jobs and claims the VolSync controller creates, and Pods the job controller creates for them, pass. The scheduler's and binder's UPDATE of a VolSync-owned PVC passes; CREATE does not. Removing only `kubernetes.io/pvc-protection` from a terminating VolSync-owned PVC passes. |
| Lowest priority | PriorityClass `hosted-app` (`-1000`, `preemptionPolicy: Never`), set on every workload by MutatingPolicy `app-priority` (`failurePolicy: Ignore`); `app-file` refuses any other class, so an unmutated workload is refused, not promoted. |
| Revoke stops what runs | CronJob `platform-app-hosting/app-grant-revoke` (namespace layer `feature`, Pod Security `restricted`), every five minutes: in each listed app's namespace, dry-run re-admits every Pod, unfinished Job, Service and HTTPRoute, and deletes those refused by a rule marked `grant: ` (pods get 30 seconds). Deletes a derived `app-lan-all`, `app-lan` or `app-ports` holding more than granted. A gone App stops all of its objects. PVCs, data, backups and VolSync movers stay. Its account reads Apps, lists namespaces, and patches or deletes pods, Jobs, Services, HTTPRoutes and NetworkPolicies in app namespaces; no Secret, PVC or core namespace. |
| Grants (exception lines) | `privileged`, `root`, `host-network`, `host-path`, `hostname <name>` (outside `dns_suffix`), `lan-port <port>/<TCP\|UDP>`, `lan-all`, `lan-target <name> <cidr> <port>/<TCP\|UDP>` (inside `lan_cidrs`; name a DNS label other than `all`), `no-uptime-check`. No blanket grant. LAN addresses live only in the env's repository. |

## Expects

| What | From |
| --- | --- |
| The apps repository at `apps_git_repo_url`, https, no credential; each app at its tag `<name>/v<x.y.z>` (`versions:`), never a branch | the env |
| The app's objects meet admission; `${app_host}` is their only variable; image and probes are the app's | the app |
| The app's name in `switches.yaml` and the render `flux/generated/apps.yaml` committed | [schema](../../../schema/CONTRACT.md), `jarvis render` |
| `lan_cidrs` once a line grants `lan-all` or `lan-target` | `values/cluster-vars.yaml` |
| A `backup: true` volume's credential, sealed by the env | [docs/backups.md](../../../docs/backups.md#app-credentials) |
| Kyverno, cert-manager (`platform-controllers`); Traefik in `kube-system` | core |

## Health

| Check | Pass |
| --- | --- |
| `app-<name>-wiring`, `app-<name>-file`, `app-<name>` | Ready (`jarvis up` and `jarvis verify` wait on them; an app with no valid app file fails) |
| `jarvis verify` component `apps` | bindings `app-file` and `app-grants` exist; each app namespace has its label, `default-deny`, `cluster-dns`, `same-namespace`, LimitRange `app`, and annotations equal to the env's lines; HTTPRoute hostnames are its own or granted; a declared `backup: false` volume is reported, any other `backup: none` fails; a dropped `app-` namespace is reported |
| `jarvis verify` component `apps`, backups | each bound app volume not `backup: none` has a ReplicationSource once its credential is registered (core's `backups` checks its freshness); without a credential, a warning names it. `restic-apps` logs `app users: <n>` |
| `app-grant-revoke` | last Job succeeded (not in `jarvis verify` yet); a probe with no policy answer fails the run |
| `app-hosting` (`jarvis validate`) | the env wires exactly the apps it lists, each with its baseline objects and grants |

## Limits

- A granted custom hostname gets Traefik's default certificate.
- Kyverno derives the quota and allows after admission; until then the app has the baseline's deny and the LimitRange defaults only.
- Memory is contained, not guaranteed: a core pod over its requests can still be evicted first, and the kernel's OOM killer goes by QoS, not priority.
- A revoke takes effect within the wiring's interval plus five minutes, stops the whole object, and leaves finished Jobs.
- Kyverno re-derives allows when the App changes, not when the env's grants or ranges change; the reconciler closes an allow that holds too much.
- Egress is by address and port, never hostname. No LAN discovery, no internet-facing app, LAN ranges IPv4 inside RFC 1918.
- `lan-all`, or a `lan-target` range holding a node, reaches node-side endpoints (the API, kubelet, Traefik) as any LAN device can.
- Movers in app namespaces run privileged. The namespaces and CRD keep `prune: disabled`; turning hosting off leaves them for the operator.

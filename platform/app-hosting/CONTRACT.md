# CONTRACT: app-hosting

Hosted apps are untrusted: built by the code factory or third-party, kept in the apps repository, one folder each. Core and add-ons are the trusted zone ([services/CONTRACT.md](../../services/CONTRACT.md)) and are unaffected by anything here except the cluster-wide policy objects. How to write one and turn it on: [docs/add-a-hosted-app.md](../../docs/add-a-hosted-app.md).

App hosting is an optional core component, **off by default**. An env turns it on with one line in `envs/<env>/flux/platform-configs/kustomization.yaml`, the file where it turns on the apps backup server:

```yaml
components:
  - ../../../../core/platform/app-hosting
```

Without that line the env renders none of this: no App CRD, no app admission policies or bindings, no `app-derive` policy or its Kyverno role, no `wildcard-apps` certificate or TLSStore entry, and `pvc-backup`'s `privileged-movers` as core has it. An env that lists apps with the switch off fails `ci/checks/env-schema`. The switch is a kustomize Component, not a directory under `resources:`, because it adds to two objects platform-configs already holds: TLSStore `default` (the apps wildcard) and `pvc-backup` (`privileged-movers` marks an `app-` namespace only once its credential is registered).

## Offers

- **The app file.** Every app folder `<name>/` in the apps repository holds one `app.yaml`, an `App` (`hosting.jarvis.io/v1alpha1`, CRD in `crd/app-crd.yaml`). It is the whole definition of the app, per-env differences included. Nothing of an app deploys without a valid one.

  | Field | Required | Default | Meaning |
  | --- | --- | --- | --- |
  | `metadata.name` | yes | | The app's name: its folder name, a DNS label of at most 59 characters. Its namespace is `app-<name>`. |
  | `spec.network.web` | no | no web address | The web address `https://<name>.apps.<dns suffix>` on 443, TLS from core's wildcard. `port`: the plain-HTTP container port Traefik reaches. `healthPath`: what the uptime check requests, starting with `/` (default `/`); healthy is any answer but a 5xx. No app-chosen external port, no app TLS. |
  | `spec.network.ports` | no | `[]` | LAN ports, each a LoadBalancer port: `listen` (on the LoadBalancer), `target` (the container port), `protocol` (`TCP` or `UDP`). Each needs the env's `lan-port <listen>/<protocol>` line. No cluster-internal entries: the namespace reaches itself, and other apps reach it through `callers`. |
  | `spec.network.egress.internet` | no | `false` | Any port and protocol to the internet; never the LAN or the cluster. |
  | `spec.network.egress.lan` | no | `[]` | LAN targets by name, each needing the env's `lan-target` lines, or exactly `[all]` (`all` is reserved), needing `lan-all`. |
  | `spec.network.egress.apps` | no | `[]` | Apps this app calls, at `<service>.app-<other>.svc`. |
  | `spec.network.callers` | no | `[]` | Apps that may call this app, on any port. |
  | `spec.volumes` | yes (may be `[]`) | | Each PVC the app keeps: `name` and `backup: true` or `false`. |
  | `spec.resources` | no | `cpu: 500m`, `memory: 512Mi` | The namespace's total requests and limits (its ResourceQuota). |
  | `spec.risky` | no | all off | Asks for `privileged`, `root`, `hostNetwork`, `hostPath` (booleans) or `hostnames` (custom hostnames outside `.apps`). Each needs the env's exception line. |
  | `spec.envs.<env>` | no | | Per-env overrides of `network.egress.internet` and `resources`. |

  Every network field defaults closed: an App with no `network` has no web address, no LAN port, no egress beyond cluster DNS and no caller. App-to-app traffic flows only when the caller lists the callee under `egress.apps` and the callee lists the caller under `callers`. The API server refuses an App that breaks the schema. `tools/app-check` also refuses unknown keys, which the API server would drop unread. Kubernetes probes stay in the app's `deploy/`.
- **Hostname.** `<name>.apps.<dns suffix>`, always. The app writes `${app_host}` as its Ingress host; the app's Kustomization fills it. Certificate `wildcard-apps` (`*.apps.<dns suffix>`, internal CA) serves every app, so no app has its own certificate and none can claim a core or add-on name.
- **What core derives, per listed app** (`app/`, applied as the platform account by the env's `app-<name>-wiring`):
  - Namespace `app-<name>`, labelled `hosting.jarvis.io/app: <name>`, annotated with its host, the env, the env's pod, service and LAN ranges (`hosting.jarvis.io/lan-cidrs`) and the env's LAN grants for the app (`hosting.jarvis.io/lan-all`, `hosting.jarvis.io/lan-targets`, equal to the grants ConfigMap's), Pod Security `warn` and `audit` at `restricted`;
  - ConfigMap `flux-system/app-<name>-grants`: the app's host and the env's exception lines for it (`privileged`, `root`, `host-network`, `host-path`, `hostnames`, `lan-ports`, `lan-all`, `lan-targets` as `<name> <cidr> <port>/<protocol>` lines joined by commas, `no-uptime-check`). The app's identity cannot write in `flux-system`;
  - tenant account `flux-system/app-<name>` and Role `app` in `app-<name>` on the kinds an app ships;
  - NetworkPolicies `default-deny`, `same-namespace` and `cluster-dns`, and LimitRange `app` (per container: default limits 100m and 128Mi, default requests 50m and 64Mi); `default-deny` keeps `prune: disabled`, so a namespace the env drops stays isolated;
  - ValidatingAdmissionPolicyBindings `app-file-<name>` (params: the App) and `app-grants-<name>` (params: the grants ConfigMap);
  - two sources reading only `<name>/app.yaml` and `<name>/deploy/`, and two tenant Kustomizations, `app-<name>-file` and `app-<name>` (which waits on the first), targeting `app-<name>`.
- **What core derives from the App** (ClusterPolicy `app-derive`, `policies/derive.yaml`): ResourceQuota `app` from `resources` (with the env's override), `services.nodeports: 0` and `services.loadbalancers` up to the number of `network.ports`; NetworkPolicies, each only when the App asks for it:
  - `app-web`: Traefik to `network.web.port`;
  - `app-ports`: from any address to each `network.ports` `target`;
  - `app-internet`: any port and protocol to `0.0.0.0/0` except RFC 1918, `100.64.0.0/10`, link-local, and the env's pod, service and LAN ranges (DNS comes from `cluster-dns`), with the env's override;
  - `app-lan-all`: any port to each of the env's LAN ranges, only when the App asks `lan: [all]` **and** the env grants `lan-all`;
  - `app-lan`: for each target the App lists under `egress.lan` **and** the env grants as `lan-target`, its range on the granted ports only;
  - `app-apps` (egress to the namespaces of `egress.apps`) and `app-callers` (ingress from the namespaces of `callers`): one side each, so traffic flows only when both list each other;
  - `app-backups` (egress to `restic-apps.platform-backups:8000/TCP`) exists only while the env registers the app's backup credential.
- **Uptime.** The uptime check of `https://<name>.apps.<dns suffix>` is on for every app with `network.web`, wherever observability is on; it reads the App's `network.web.healthPath` and the grants ConfigMap's `no-uptime-check`, which is `true` only with the env's `<app>: no-uptime-check` line. The check itself is not built yet ([platform/traefik/CONTRACT.md](../traefik/CONTRACT.md)).
- **Admission** (`policies/admission.yaml`, native ValidatingAdmissionPolicies, `failurePolicy: Fail`):
  - `app-identity`: a namespace is named `app-<name>` exactly when it carries `hosting.jarvis.io/app: <name>`; an App lives only in its own namespace, named after it.
  - `app-tenant`: the tenant account writes only in `app-<name>`, and only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, Ingresses, PodDisruptionBudgets, HorizontalPodAutoscalers and its App. No Traefik CRD, NetworkPolicy, quota, RBAC, Flux, VolSync, DaemonSet or cluster-scoped object.
  - `app-file` and `app-grants`, bound per app: the **pod baseline** (no privileged mode, added capabilities other than `NET_BIND_SERVICE`, unconfined seccomp, sysctls, host PID or IPC, host network or host ports, hostPath, or root: every container sets `allowPrivilegeEscalation: false` and runs `runAsNonRoot: true` with no `runAsUser: 0`; volumes only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi or ephemeral, since a network volume mounts past every NetworkPolicy), PVCs the StatefulSet controller names `<volume>-<set>-<n>` from a declared claim template, checked on Pods and on every workload template; every PVC and StatefulSet claim template is a declared volume, with `backup: none` exactly when declared `backup: false`; Ingress host `<name>.apps.<dns suffix>`, no default backend, no `spec.tls`; Services ClusterIP, or LoadBalancer only on `network.ports` (each port a `listen` with its `protocol`, and a numeric `targetPort` equal to its `target`) and with `allocateLoadBalancerNodePorts: false` (ServiceLB needs no node port); no NodePort, ExternalName or `externalIPs`. `app-file` holds each object to the App; `app-grants` holds it to the env's grants. A risky item, a custom hostname or a LAN port passes only when the App asks for it **and** the env grants it. A missing App or grants ConfigMap refuses everything they match.
  - **The VolSync carve-out** is keyed on identities a tenant cannot take: Jobs and claims the VolSync controller (`volsync/volsync`) creates, and Pods the job controller creates for a Job. A tenant's own Job is checked when the tenant creates it, so a look-alike Job or Pod is refused.
- **Exceptions.** The only grants: `privileged`, `root`, `host-network`, `host-path`, `hostname <name>`, `lan-port <port>/<TCP|UDP>`, `lan-all`, `lan-target <name> <cidr> <port>/<TCP|UDP>` (one line per port; the range inside the env's `lan_cidrs`, the name a DNS label other than `all`), and `no-uptime-check`. There is no blanket grant. `lan-all` and `lan-target` need the env's `lan_cidrs` ([schema/CONTRACT.md](../../schema/CONTRACT.md)). LAN addresses live only in the env's repository, never in the apps repository.

## Expects

- The apps repository at `apps_git_repo_url` (https, readable without a credential), branch `main`, one folder per app: `<name>/app.yaml` and `<name>/deploy/` (plain manifests, or a kustomization). Other files in the folder never reach the cluster.
- The app's objects meet the rules above. `${app_host}` is the only variable they may use. The image and the probes are the app's own concern.
- The env lists the app in `envs/<env>/values/apps.yaml` (names only) and commits the render, `envs/<env>/flux/generated/apps.yaml`, listed in its `flux/kustomization.yaml`.
- An app with a `backup: true` volume has its own backup credential registered by the env ([platform/backups/CONTRACT.md](../backups/CONTRACT.md), App credentials); until then that PVC gets no backup and `make verify` fails it.
- **Tenant secrets.** No app secrets path exists yet. When one is built, it applies only Secrets sealed for the app, and never `backups-restic-app-<name>.sops.yaml`, which lives in the same `envs/<env>/secrets/app-<name>/` folder but is the backup credential `platform-backups` holds.
- Platform: the switch (`kustomization.yaml`) on the env's `platform-configs` path, which brings the CRD (`crd/`), the policies (`policies/`), Kyverno's aggregated role `kyverno:app-derive` (`kyverno-rbac.yaml`) and `wildcard-apps` (`certificate.yaml`); Kyverno and cert-manager from `platform-controllers`; Traefik in `kube-system` (`app.kubernetes.io/name: traefik`).

## Health

- The env's `app-<name>-wiring`, `app-<name>-file` and `app-<name>` Kustomizations are Ready; `make verify` waits on every Kustomization, so an app listed with no valid app file (`app-<name>-file` not Ready, `app-<name>` waiting) fails it.
- `make verify` component `apps` (only for an env that lists an app): each listed app's namespace carries its label, `default-deny`, `cluster-dns`, `same-namespace`, LimitRange `app` and both bindings; its grants ConfigMap holds exactly the env's exception lines; every Ingress host in it is `<name>.apps.<dns suffix>` or a granted hostname. An app PVC its App declares `backup: false` is reported as declared, not failed; `backup: none` on any other app PVC fails. An `app-` namespace the env no longer lists is reported.

## Config

- **Turn app hosting on:** the `components:` line above. Turn it off only once the env lists no app; the CRD and the app namespaces keep `prune: disabled` and stay until the operator deletes them.
- **Turn an app on:** with app hosting on, add its name to `apps:` in `envs/<env>/values/apps.yaml`, run `python3 tools/lib/render_apps.py --env <env> --output envs/<env>/flux/generated/apps.yaml`, and list `generated/apps.yaml` in `envs/<env>/flux/kustomization.yaml`. **Turn it off:** remove the name and re-render (or, with no app left, delete the file and its line). Changing an app needs no env edit. The render holds only the env's names, lines, `apps_git_repo_url` and CIDRs (pod, service and LAN); a core change to the wiring reaches the env with no re-render.
- **The env's LAN ranges:** `lan_cidrs` in `values/cluster-vars.yaml`, required once an exception line grants `lan-all` or `lan-target`. Internet egress never reaches them.
- **Exception lines** go under `exceptions:` in the same file, one `- <app>: <grant>` each. A grant counts only for an app on the list whose App asks for the same item (`no-uptime-check` alone needs no ask); removing the line and re-rendering removes the grant at the next admission. A custom hostname stays outside the env's `dns_suffix`. Only the env's repository can grant one: its file reaches the cluster as the grants ConfigMap the platform account applies, never through the apps repository, and a file in the app's own folder grants nothing.
- **Per-env differences** live in the App's `spec.envs.<env>`.
- `tools/app-check <folder>...` is the validator the apps repository's CI runs, from a checkout of this repository at a pinned commit (Python 3, PyYAML, and kustomize for a `deploy/kustomization.yaml`).

## Proven by

- `ci/checks/app-hosting` (per env: it wires exactly the apps it lists, and each wiring gives its app the namespace and label, `default-deny` and the other baseline objects, both bindings refusing missing params, grants and the namespace's LAN-grant annotations equal to the env's lines, and a tenant whose Role and Kustomizations stay in its namespace and sources). `ci/tests/test_app_hosting.py` seeds each failure, and env-schema's and flux-identity's app rules (a LAN grant without `lan_cidrs`, a `lan-target` range outside them).
- `ci/tests/test_app_admission.py` runs every admission rule, the derive policy and `privileged-movers` with the pinned `kyverno` CLI against good and bad objects, with the bindings, grants and namespace annotations rendered from the real wiring, and runs `tools/app-check` on the same objects: the mover passes, a look-alike tenant Job or Pod fails, an excepted pod passes only with the env's line, a grants ConfigMap the app ships grants nothing, a LoadBalancer passes only on a granted `network.ports` entry with its numeric target; the derived allows: web to `web.port`, internet any port to a public address and never to a LAN or cluster address, the whole LAN only with `lan-all`, a named target only with its `lan-target` lines and only on their ports, app to app one side per file; `no-uptime-check` reaches the grants ConfigMap.
- `ci/tests/test_app_hosting.py` renders `platform-controllers` and `platform-configs` with and without the switch: off holds no app-hosting object and core's `privileged-movers`; on holds every one, and nothing else differs.
- `ci/checks/env-schema` (apps only with the switch on, the app list, exception lines, `apps_git_repo_url`, the render), `ci/checks/flux-identity` (the wiring as the platform account, the app's Kustomizations as its tenant), `ci/checks/flux-prune`, `ci/checks/addons` (no add-on is named `apps`).
- `make verify` component `apps` (`tools/verify/apps.py`; `ci/tests/test_verify_apps.py`).

## Operations

- **Remove an app for good:** [docs/add-a-hosted-app.md](../../docs/add-a-hosted-app.md#turn-it-off). Until the operator deletes them, the kept bindings refuse every new pod in the left namespace, because its App is gone.

## Limits

- A custom hostname's TLS is not served by the apps wildcard; a granted custom hostname gets Traefik's default certificate.
- Kyverno generates the quota and the derived NetworkPolicies after admission. Until it has, the app has the baseline's deny, and no namespace total (each container still has the LimitRange defaults).
- The live network-policy test runs once an env hosts an app.
- Movers in app namespaces stay privileged.
- Kyverno re-derives an app's allows when its App changes, not when the env's grants or ranges change: removing a `lan-all` or `lan-target` line updates the namespace and the grants at once, and closes the derived `app-lan-all` or `app-lan` at the App's next change. Until then the hole the App asked for and the env had granted stays open.
- Egress is filtered by address and port, not by internet hostname.
- No multicast, mDNS or other LAN discovery: a LAN target is a fixed range the env names.
- No internet-facing app: the web address and LAN ports are for the LAN.
- LAN ranges are IPv4 inside RFC 1918.
- The cluster's own nodes sit on the LAN: `lan-all`, or a `lan-target` range holding a node, lets the app's pods reach the node-side endpoints (the Kubernetes API, the kubelet, Traefik and anything else listening on a node) as any LAN device can. Grant the whole LAN only to an app trusted that far, and keep `lan-target` ranges off the nodes.

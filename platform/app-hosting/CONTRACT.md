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
  | `spec.webPort` | yes | | The container port Traefik reaches, or `none`. |
  | `spec.volumes` | yes (may be `[]`) | | Each PVC the app keeps: `name` and `backup: true` or `false`. |
  | `spec.resources` | no | `cpu: 500m`, `memory: 512Mi` | The namespace's total requests and limits (its ResourceQuota). |
  | `spec.internet` | no | `false` | Egress to TCP 443 on the internet. |
  | `spec.talksTo` | no | `[]` | Apps this app may call. |
  | `spec.acceptsFrom` | no | `[]` | Apps that may call this app, on any port. |
  | `spec.ports` | no | `[]` | Extra container ports: `port`, `protocol` (`TCP` or `UDP`), and `lan: true` to ask for the home network. Cluster-internal otherwise. |
  | `spec.risky` | no | all off | Asks for `privileged`, `root`, `hostNetwork`, `hostPath` (booleans) or `hostnames` (custom hostnames outside `.apps`). Each needs the env's exception line. |
  | `spec.envs.<env>` | no | | Per-env overrides of `internet` and `resources`. |

  The API server refuses an App that breaks the schema. `tools/app-check` also refuses unknown keys, which the API server would drop unread.
- **Hostname.** `<name>.apps.<dns suffix>`, always. The app writes `${app_host}` as its Ingress host; the app's Kustomization fills it. Certificate `wildcard-apps` (`*.apps.<dns suffix>`, internal CA) serves every app, so no app has its own certificate and none can claim a core or add-on name.
- **What core derives, per listed app** (`app/`, applied as the platform account by the env's `app-<name>-wiring`):
  - Namespace `app-<name>`, labelled `hosting.jarvis.io/app: <name>`, annotated with its host, the env and the env's pod and service CIDRs, Pod Security `warn` and `audit` at `restricted`;
  - ConfigMap `flux-system/app-<name>-grants`: the app's host and the env's exception lines for it. The app's identity cannot write in `flux-system`;
  - tenant account `flux-system/app-<name>` and Role `app` in `app-<name>` on the kinds an app ships;
  - NetworkPolicies `default-deny`, `same-namespace` and `cluster-dns`, and LimitRange `app` (per container: default limits 100m and 128Mi, default requests 50m and 64Mi); `default-deny` keeps `prune: disabled`, so a namespace the env drops stays isolated;
  - ValidatingAdmissionPolicyBindings `app-file-<name>` (params: the App) and `app-grants-<name>` (params: the grants ConfigMap);
  - two sources reading only `<name>/app.yaml` and `<name>/deploy/`, and two tenant Kustomizations, `app-<name>-file` and `app-<name>` (which waits on the first), targeting `app-<name>`.
- **What core derives from the App** (ClusterPolicy `app-derive`, `policies/derive.yaml`): ResourceQuota `app` from `resources` (with the env's override), `services.nodeports: 0` and `services.loadbalancers` up to the number of `lan` ports; NetworkPolicy `app-web` (Traefik to `webPort`), `app-internet` (TCP 443 to `0.0.0.0/0` except RFC 1918, `100.64.0.0/10`, link-local and the env's pod and service CIDRs; DNS comes from `cluster-dns`), `app-talks-to`, `app-accepts-from`, `app-lan` (from any address to the `lan` ports). App-to-app traffic flows only when the caller lists the callee under `talksTo` and the callee lists the caller under `acceptsFrom`. `app-backups` (egress to `restic-apps.platform-backups:8000/TCP`) exists only while the env registers the app's backup credential.
- **Admission** (`policies/admission.yaml`, native ValidatingAdmissionPolicies, `failurePolicy: Fail`):
  - `app-identity`: a namespace is named `app-<name>` exactly when it carries `hosting.jarvis.io/app: <name>`; an App lives only in its own namespace, named after it.
  - `app-tenant`: the tenant account writes only in `app-<name>`, and only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, Ingresses, PodDisruptionBudgets, HorizontalPodAutoscalers and its App. No Traefik CRD, NetworkPolicy, quota, RBAC, Flux, VolSync, DaemonSet or cluster-scoped object.
  - `app-file` and `app-grants`, bound per app: the **pod baseline** (no privileged mode, added capabilities other than `NET_BIND_SERVICE`, unconfined seccomp, sysctls, host PID or IPC, host network or host ports, hostPath, or root: every container sets `allowPrivilegeEscalation: false` and runs `runAsNonRoot: true` with no `runAsUser: 0`; volumes only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi or ephemeral, since a network volume mounts past every NetworkPolicy), PVCs the StatefulSet controller names `<volume>-<set>-<n>` from a declared claim template, checked on Pods and on every workload template; every PVC and StatefulSet claim template is a declared volume, with `backup: none` exactly when declared `backup: false`; Ingress host `<name>.apps.<dns suffix>`, no default backend, no `spec.tls`; Services ClusterIP, or LoadBalancer only on `lan` ports and with `allocateLoadBalancerNodePorts: false` (ServiceLB needs no node port); no NodePort, ExternalName or `externalIPs`. `app-file` holds each object to the App; `app-grants` holds it to the env's grants. A risky item, a custom hostname or a LAN port passes only when the App asks for it **and** the env grants it. A missing App or grants ConfigMap refuses everything they match.
  - **The VolSync carve-out** is keyed on identities a tenant cannot take: Jobs and claims the VolSync controller (`volsync/volsync`) creates, and Pods the job controller creates for a Job. A tenant's own Job is checked when the tenant creates it, so a look-alike Job or Pod is refused.
- **Exceptions.** The only grants: `privileged`, `root`, `host-network`, `host-path`, `hostname <name>`, `lan-port <port>/<TCP|UDP>`. There is no blanket grant.

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
- **Turn an app on:** with app hosting on, add its name to `apps:` in `envs/<env>/values/apps.yaml`, run `python3 tools/lib/render_apps.py --env <env> --output envs/<env>/flux/generated/apps.yaml`, and list `generated/apps.yaml` in `envs/<env>/flux/kustomization.yaml`. **Turn it off:** remove the name and re-render (or, with no app left, delete the file and its line). Changing an app needs no env edit. The render holds only the env's names, lines, `apps_git_repo_url` and CIDRs; a core change to the wiring reaches the env with no re-render.
- **Exception lines** go under `exceptions:` in the same file, one `- <app>: <grant>` each. A grant counts only for an app on the list whose App asks for the same item; removing the line and re-rendering removes the grant at the next admission. A custom hostname stays outside the env's `dns_suffix`. Only the env's repository can grant one: its file reaches the cluster as the grants ConfigMap the platform account applies, never through the apps repository, and a file in the app's own folder grants nothing.
- **Per-env differences** live in the App's `spec.envs.<env>`.
- `tools/app-check <folder>...` is the validator the apps repository's CI runs, from a checkout of this repository at a pinned commit (Python 3, PyYAML, and kustomize for a `deploy/kustomization.yaml`).

## Proven by

- `ci/checks/app-hosting` (per env: it wires exactly the apps it lists, and each wiring gives its app the namespace and label, `default-deny` and the other baseline objects, both bindings refusing missing params, grants equal to the env's lines, and a tenant whose Role and Kustomizations stay in its namespace and sources). `ci/tests/test_app_hosting.py` seeds each failure, and env-schema's and flux-identity's app rules.
- `ci/tests/test_app_admission.py` runs every admission rule, the derive policy and `privileged-movers` with the pinned `kyverno` CLI against good and bad objects, with the bindings and grants rendered from the real wiring, and runs `tools/app-check` on the same objects: the mover passes, a look-alike tenant Job or Pod fails, an excepted pod passes only with the env's line, a grants ConfigMap the app ships grants nothing.
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

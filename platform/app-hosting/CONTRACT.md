# CONTRACT: app-hosting

Hosted apps are untrusted: built by the code factory or third-party, kept in the apps repository, one folder each. Core and add-ons are the trusted zone ([services/CONTRACT.md](../../services/CONTRACT.md)) and are unaffected by anything here except the cluster-wide policy objects. How to write one and turn it on: [docs/add-a-hosted-app.md](../../docs/add-a-hosted-app.md).

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
  - Namespace `app-<name>`, labelled `hosting.jarvis.io/app: <name>` with the env's grants as `grants.hosting.jarvis.io/<grant>` labels and annotations, Pod Security `warn` and `audit` at `restricted`;
  - tenant account `flux-system/app-<name>` and Role `app` in `app-<name>` on the kinds an app ships;
  - NetworkPolicies `default-deny`, `same-namespace` and `cluster-dns`, and LimitRange `app` (per container: default limits 500m and 512Mi, default requests 50m and 64Mi);
  - ValidatingAdmissionPolicyBinding `app-file-<name>`;
  - two sources reading only `<name>/app.yaml` and `<name>/deploy/`, and two tenant Kustomizations, `app-<name>-file` and `app-<name>` (which waits on the first), targeting `app-<name>`.
- **What core derives from the App** (ClusterPolicy `app-derive`, `policies/derive.yaml`): ResourceQuota `app` from `resources` (with the env's override), `services.nodeports: 0` and `services.loadbalancers` up to the number of `lan` ports; NetworkPolicy `app-web` (Traefik to `webPort`), `app-internet` (TCP 443 to `0.0.0.0/0` except RFC 1918, `100.64.0.0/10`, link-local and the env's pod and service CIDRs; DNS comes from `cluster-dns`), `app-talks-to`, `app-accepts-from`, `app-lan` (from any address to the `lan` ports). App-to-app traffic flows only when the caller lists the callee under `talksTo` and the callee lists the caller under `acceptsFrom`. `app-backups` (egress to `restic-apps.platform-backups:8000/TCP`) exists only while the env registers the app's backup credential.
- **Admission** (`policies/admission.yaml`, native ValidatingAdmissionPolicies, `failurePolicy: Fail`):
  - `app-identity`: a namespace is named `app-<name>` exactly when it carries `hosting.jarvis.io/app: <name>`; only those carry grant labels; an App lives only in its own namespace, named after it.
  - `app-tenant`: the tenant account writes only in `app-<name>`, and only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, Ingresses, PodDisruptionBudgets, HorizontalPodAutoscalers and its App. No Traefik CRD, NetworkPolicy, quota, RBAC, Flux, VolSync, DaemonSet or cluster-scoped object.
  - `app-file`, bound per app to its App: the **pod baseline** (no privileged mode, privilege escalation, added capabilities other than `NET_BIND_SERVICE`, host PID or IPC, host network or host ports, hostPath, or root: every container `runAsNonRoot: true` and no `runAsUser: 0`), checked on Pods and on every workload template; every PVC and StatefulSet claim template is a declared volume, with `backup: none` exactly when declared `backup: false`; Ingress host `<name>.apps.<dns suffix>`, no default backend, no `spec.tls`; Services ClusterIP, or LoadBalancer only on granted `lan` ports; no NodePort, ExternalName or `externalIPs`. A risky item passes only when the App asks for it **and** the namespace carries the matching grant. A missing App refuses everything the policy matches.
  - **The VolSync carve-out** is keyed on identities a tenant cannot take: Jobs and claims the VolSync controller (`volsync/volsync`) creates, and Pods the job controller creates for a Job. A tenant's own Job is checked when the tenant creates it, so a look-alike Job or Pod is refused.
- **Exceptions.** The only grants: `privileged`, `root`, `host-network`, `host-path`, `hostname <name>`, `lan-port <port>/<TCP|UDP>`. There is no blanket grant.

## Expects

- The apps repository at `apps_git_repo_url` (https, readable without a credential), branch `main`, one folder per app: `<name>/app.yaml` and `<name>/deploy/` (plain manifests, or a kustomization). Other files in the folder never reach the cluster.
- The app's objects meet the rules above. `${app_host}` is the only variable they may use. The image and the probes are the app's own concern.
- The env lists the app in `envs/<env>/values/apps.yaml` (names only) and commits the render, `envs/<env>/flux/generated/apps.yaml`, listed in its `flux/kustomization.yaml`.
- An app with a `backup: true` volume has its own backup credential registered by the env ([platform/backups/CONTRACT.md](../backups/CONTRACT.md), App credentials); until then that PVC gets no backup and `make verify` fails it.
- **Tenant secrets.** No app secrets path exists yet. When one is built, it applies only Secrets sealed for the app, and never `backups-restic-app-<name>.sops.yaml`, which lives in the same `envs/<env>/secrets/app-<name>/` folder but is the backup credential `platform-backups` holds.
- Platform: the CRD on `platform-controllers` (`crd/`), the policies on `platform-configs` (`policies/`), Kyverno's aggregated role `kyverno:app-derive`, and Traefik in `kube-system` (`app.kubernetes.io/name: traefik`).

## Health

- The env's `app-<name>-wiring`, `app-<name>-file` and `app-<name>` Kustomizations are Ready; `make verify` waits on every Kustomization, so an app listed with no valid app file (`app-<name>-file` not Ready, `app-<name>` waiting) fails it.
- `make verify` component `apps`: every `app-` namespace carries its labels and the env's grants exactly, has `default-deny`, `cluster-dns`, `same-namespace`, LimitRange `app` and binding `app-file-<name>`, and every Ingress host in it is `<name>.apps.<dns suffix>` or a granted hostname. A PVC declared `backup: false` is reported, not failed.

## Config

- **Turn an app on:** add its name to `apps:` in `envs/<env>/values/apps.yaml`, run `python3 tools/lib/render_apps.py --env <env> --output envs/<env>/flux/generated/apps.yaml`, and list `generated/apps.yaml` in `envs/<env>/flux/kustomization.yaml`. **Turn it off:** remove the name and re-render (or, with no app left, delete the file and its line). Changing an app needs no env edit.
- **Exception lines** go under `exceptions:` in the same file, one `- <app>: <grant>` each. A grant counts only for an app on the list whose App asks for the same item; removing the line removes the grant at the next admission. A custom hostname stays outside the env's `dns_suffix`. Only the env's repository can grant one: its file reaches the cluster through the wiring the platform account applies, never through the apps repository.
- **Per-env differences** live in the App's `spec.envs.<env>`.
- `tools/app-check <folder>...` is the validator the apps repository's CI runs, from a checkout of this repository at a pinned commit (Python 3, PyYAML, and kustomize for a `deploy/kustomization.yaml`).

## Proven by

- `ci/checks/app-hosting` (per env that lists apps: each app's wiring Kustomization, and the objects the template gives it: namespace labels and grants, default-deny, the binding, the tenant's Role and Kustomizations; per product: `tools/app-check` on the sample apps). `ci/tests/test_app_hosting.py` seeds each failure.
- `ci/tests/test_app_admission.py` runs every admission and derive rule with the pinned `kyverno` CLI against good and bad objects, and runs `tools/app-check` on the same objects: the mover passes, a look-alike tenant Job or Pod fails, an excepted pod passes only with the env's grant, an exception file in the app's folder grants nothing.
- `ci/checks/env-schema` (the app list, exception lines, `apps_git_repo_url`, the render), `ci/checks/flux-identity` (the wiring as the platform account, the app's Kustomizations as its tenant), `ci/checks/flux-prune`, `ci/checks/addons` (no add-on is named `apps`).
- `make verify` component `apps` (`tools/verify/apps.py`).

## Operations

- **Remove an app for good:** drop it from the list and re-render; Flux prunes its Kustomizations, which prune the app's objects. Then, as the cluster admin, delete namespace `app-<name>` (its PVCs go with it), ServiceAccount `flux-system/app-<name>`, GitRepositories `flux-system/app-<name>` and `app-<name>-file`, and ValidatingAdmissionPolicyBinding `app-file-<name>`, which `prune: disabled` kept. Retire its backup credential ([platform/backups/CONTRACT.md](../backups/CONTRACT.md)).

## Limits

- A custom hostname's TLS is not served by the apps wildcard; a granted custom hostname gets Traefik's default certificate.
- Kyverno generates the quota and the derived NetworkPolicies after admission. Until it has, the app has the baseline's deny, and no namespace total (each container still has the LimitRange defaults).
- The live network-policy test runs once an env hosts an app.
- Movers in app namespaces stay privileged.

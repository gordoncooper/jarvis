# Add a hosted app

A hosted app lives in the apps repository, one folder per app, and is untrusted: core decides what it may do, and the cluster holds it to that. The rules are in [platform/optional/app-hosting/CONTRACT.md](../platform/optional/app-hosting/CONTRACT.md).

Start from one of the three examples in the `jarvis-apps` apps repository:

- [hello-web](../ci/tests/fixtures/app-hosting/jarvis-apps/hello-web/): a plain web app on defaults (`network.web.port`, health path `/`).
- [notes](../ci/tests/fixtures/app-hosting/jarvis-apps/notes/): a stateful one, with a backed-up volume and its own `network.web.healthPath`.
- [lan-demo](../ci/tests/fixtures/app-hosting/jarvis-apps/lan-demo/): one that asks for a risky item, a LAN port (`network.ports`), and no web address.

The links are to [ci/tests/fixtures/app-hosting/jarvis-apps/](../ci/tests/fixtures/app-hosting/jarvis-apps/), which holds exact copies of those three folders for this repository's tests; the apps repository's CI fails when a folder there differs from its copy at the jarvis commit it pins.

## Write it

In the apps repository:

1. Make `<name>/`. The folder name is the app's name: a lowercase DNS label of at most 59 characters. Its namespace is `app-<name>` and its address is `https://<name>.apps.<dns suffix>`.
2. Write `<name>/app.yaml`:

   ```yaml
   apiVersion: hosting.jarvis.io/v1alpha1
   kind: App
   metadata:
     name: <name>
   spec:
     network:
       web:                 # absent = no web address
         port: 8080         # plain-HTTP container port; served at https://<name>.apps.<dns suffix>
         healthPath: /      # optional, default /; the uptime check counts any answer but a 5xx
     volumes:               # every PVC the app keeps; [] for none
       - name: data
         backup: true
   ```

   The rest of `network`, all closed by default:

   ```yaml
   network:
     ports:                 # LAN ports; each needs the env's lan-port line
       - listen: 25565      # the LoadBalancer port
         target: 25565      # the container port; the Service's targetPort, as a number
         protocol: TCP      # or UDP
     egress:
       internet: true       # any port to the internet, never the LAN or the cluster; default false
       lan: [nas]           # LAN targets the env names and grants, or [all]; default none
       apps: [notes]        # apps this app calls at <service>.app-<other>.svc
     callers: [web-ui]      # apps that may call this one; both sides list each other
   ```

   Also optional: `resources` (500m CPU, 512Mi memory for the whole app), `risky` (nothing), and `envs.<env>` to change `network.egress.internet` or `resources` in one env. Probes stay in `deploy/`.
3. Put the app's objects in `<name>/deploy/`: plain manifests, or a kustomization. Leave `metadata.namespace` unset. Only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, Ingresses, PodDisruptionBudgets and HorizontalPodAutoscalers. Pods meet Pod Security `restricted`: each container runs as non-root (`runAsNonRoot: true`) with `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]` (adding back only `NET_BIND_SERVICE`), a `seccompProfile` of `RuntimeDefault` (or `Localhost`) is set on the pod or every container, and pods use only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi or ephemeral volumes. Each container's memory request equals its memory limit (or set the limit alone): the app reserves all the memory it may use, and a container past its limit is OOM-killed and restarted. Set no `priorityClassName`: core runs every app pod at `hosted-app`, below everything else, so core preempts it when space runs out. A PVC is named like a declared volume, carries annotation `backup: none` exactly when declared `backup: false`, and sets no `volumeName`, `dataSource` or `dataSourceRef`. The Ingress host is `${app_host}`, `ingressClassName: traefik`, with no `tls` section; of router annotations it carries only `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and `traefik.ingress.kubernetes.io/router.tls: "true"`, and a Service carries no Traefik annotation. A LAN port is a `LoadBalancer` Service with `allocateLoadBalancerNodePorts: false`, each port a `network.ports` entry: `port` its `listen`, `targetPort` its `target` as a number.
4. Run `<jarvis checkout>/.venv/bin/python <jarvis checkout>/tools/app-check <name>`. It reports every rule the folder breaks; the apps repository's CI runs the same tool from a pinned commit of this repository.

## Turn it on in an env

In the env's repository:

1. Once per env, turn app hosting on (it is off by default): add to `envs/<env>/values/switches.yaml`

   ```yaml
   optional:
     - app-hosting
   ```

   Without it, env-schema refuses an app list. The first time, `platform-configs` may fail one apply while the new App CRD becomes known to the API server and Kyverno; its next retry, about a minute later, passes on its own.
2. Add the name to `envs/<env>/values/switches.yaml` (names only, one line), and `apps_git_repo_url` to `values/cluster-vars.yaml` if it is not there yet:

   ```yaml
   apps:
     - <name>
   ```

3. If the app asks for a risky item in `spec.risky`, a LAN port or LAN egress, add the matching exception line under `exceptions:` in `envs/<env>/values/apps.yaml`, one grant each: `- <name>: privileged`, `root`, `host-network`, `host-path`, `hostname <host>`, `lan-port <listen>/<TCP|UDP>`, `lan-all` (for `lan: [all]`) or `lan-target <target> <cidr> <port>/<TCP|UDP>` (one line per port of a named target). Without it the cluster refuses the objects that use that item, or opens no LAN path. A LAN grant needs the env's ranges, `lan_cidrs` in `values/cluster-vars.yaml`, and a `lan-target` range inside them. To keep a web app out of the uptime check, add `- <name>: no-uptime-check`. Removing a line stops what runs on it within minutes (pods, Jobs, Services and Ingresses that need it, and its LAN allows), and keeps the app's volumes; adding it back restores them.
4. If a volume is `backup: true`, select it under `backups:` in `values/apps.yaml`, list `apps-backup-server` under `optional`, and register the app's backup credential: `make bootstrap-secrets ENV=<env> APP=app-<name> INSTANCE=<path>` ([platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md), App credentials). Until then that volume gets no backup, and `make verify` fails it, unless `backups:` leaves the app unselected, which skips it.
5. Render the wiring: `INSTANCE=<path> .venv/bin/python tools/lib/render_switches.py --env <env>`.
6. Run `INSTANCE=<path> bash ci/run-env-checks`, then commit and push. `make verify ENV=<env>` then checks the app's namespace, network baseline, grants and hosts.

Changing the app later needs nothing in the env: its file and `deploy/` reach the cluster from the apps repository.

## Turn it off

1. Remove its name from `values/switches.yaml` (and its exception lines from `values/apps.yaml`) and render again. Commit and push. Flux prunes the app's Kustomizations, which prune its objects.
2. Delete what `prune: disabled` kept, as the cluster admin: namespace `app-<name>` (its volumes go with it), ServiceAccount `flux-system/app-<name>` and GitRepositories `flux-system/app-<name>` and `app-<name>-file`. Retire its backup credential as the backups contract says.
3. With no app left, the env may turn app hosting off: remove `app-hosting` from `values/switches.yaml` and render again. The CRD keeps `prune: disabled`; delete it by hand once no App is left.

# Add a hosted app

A hosted app lives in the apps repository, one folder per app, and is untrusted: core decides what it may do, and the cluster holds it to that. The rules are in [core/feature/app-hosting/CONTRACT.md](../core/feature/app-hosting/CONTRACT.md).

Start from one of the three examples in the `jarvis-apps` apps repository:

- [hello-web](../ci/tests/fixtures/app-hosting/jarvis-apps/hello-web/): a plain web app on defaults, one web entry.
- [notes](../ci/tests/fixtures/app-hosting/jarvis-apps/notes/): a stateful one, with a backed-up volume.
- [lan-demo](../ci/tests/fixtures/app-hosting/jarvis-apps/lan-demo/): one that asks for a risky item, a LAN port (`network.ports`), and no web address, run from an image by a generic upstream chart.

The links are to [ci/tests/fixtures/app-hosting/jarvis-apps/](../ci/tests/fixtures/app-hosting/jarvis-apps/), which holds copies of those three folders for this repository's tests.

## Write it

In the apps repository:

1. Make `<name>/`. The folder name is the app's name: a lowercase DNS label of at most 59 characters. Its namespace is `app-<name>`.
2. Write `<name>/app.yaml`:

   ```yaml
   apiVersion: hosting.jarvis.io/v1alpha1
   kind: App
   metadata:
     name: <name>
   spec:
     network:
       web:                 # absent = no web address; core makes each entry's HTTPRoute
         - service: web     # a Service of the app's, served at https://<name>.apps.<dns suffix>
           port: 80         # its port, plain HTTP
         - name: admin      # optional: https://<name>-admin.apps.<dns suffix>
           service: admin
           port: 8080
           hostnames: [wiki.example.org]   # optional custom hostnames; each needs the env's hostname grant
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

   Also optional: `resources` (500m CPU, 512Mi memory for the whole app) and `risky` (nothing). Probes stay with the app's objects.

   An app whose routing one Service and port cannot express (several paths or backends) marks its entry `ownRoute: true` and ships that entry's HTTPRoute in `deploy/`: `hostnames: [${app_host}]` (or a granted custom hostname) and `parentRefs: [{name: jarvis, namespace: platform-gateway, sectionName: apps}]` (`apps-granted` for a custom hostname). The cluster refuses any other route an app ships, such as a chart's own route left on.
3. Put the app's objects in `<name>/deploy/`: plain manifests, or a kustomization. Or, for an app with an upstream Helm chart, ship no `deploy/` and name the chart in `app.yaml`; core installs it as the app, as is, with no fork:

   ```yaml
   spec:
     chart:
       url: oci://ghcr.io/example/charts/wiki   # the chart's own oci:// address
       version: 1.2.3
       values: {}                               # the chart's values, adapted to the rules below
   ```

   An app that ships only an image uses a generic chart such as [bjw-s app-template](https://bjw-s-labs.github.io/helm-charts/docs/app-template/), as lan-demo does. The chart's output meets the same rules as `deploy/`: turn off its ServiceAccount, Ingress, route and CRDs, and leave its namespace to the release. Either way, leave `metadata.namespace` unset. Only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, HTTPRoutes (an `ownRoute` entry's only), PodDisruptionBudgets and HorizontalPodAutoscalers. Pods meet Pod Security `restricted`: each container runs as non-root (`runAsNonRoot: true`) with `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]` (adding back only `NET_BIND_SERVICE`), a `seccompProfile` of `RuntimeDefault` (or `Localhost`) is set on the pod or every container, and pods use only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi or ephemeral volumes. Each container's memory request equals its memory limit (or set the limit alone): the app reserves all the memory it may use, and a container past its limit is OOM-killed and restarted. Set no `priorityClassName`: core runs every app pod at `hosted-app`, below everything else, so core preempts it when space runs out. A PVC is named like a declared volume, carries annotation `backup: none` exactly when declared `backup: false`, and sets no `volumeName`, `dataSource` or `dataSourceRef`. The Gateway serves each web address's TLS, and a Service carries no Traefik annotation. A LAN port is a `LoadBalancer` Service with `allocateLoadBalancerNodePorts: false`, each port a `network.ports` entry: `port` its `listen`, `targetPort` its `target` as a number.
4. Run `<core checkout>/.venv/bin/python -m jarvis.apps.check <name>`. It reports every rule the folder breaks; the apps repository's CI runs the same tool through the app-check Action at a core tag (`uses: <owner>/jarvis/.github/actions/app-check@<tag>`).

## Turn it on in an env

In the env's repository:

1. Once per env, turn app hosting on (it is off by default): add to `envs/<env>/values/switches.yaml`

   ```yaml
   features:
     - app-hosting
   ```

   Without it, env-schema refuses an app list. The first time, `platform-configs` may fail one apply while the new App CRD becomes known to the API server and Kyverno; its next retry, about a minute later, passes on its own.
2. Add the app's entry to `envs/<env>/values/apps.yaml`, pinned to a released tag, and `apps_git_repo_url` to `values/cluster-vars.yaml` if it is not there yet. The apps repository tags `<name>/v<VERSION>` from the app's `VERSION` file when a change merges, and the env runs that tag until `version` moves; the instance's Renovate preset opens a PR for each new tag:

   ```yaml
   <name>:
     version: v0.1.0
   ```

   For an app with a chart, the entry's `values:` lay the env's values over the app file's, such as a hostname the app must know; `jarvis validate` renders the chart with both against the same rules.

3. If the app asks for a risky item in `spec.risky`, a custom hostname, a LAN port or LAN egress, add the matching grant under its entry's `grants:`, one item each: `privileged`, `root`, `host-network`, `host-path`, `hostname <host>`, `lan-port <listen>/<TCP|UDP>`, `lan-all` (for `lan: [all]`) or `lan-target <target> <cidr> <port>/<TCP|UDP>` (one line per port of a named target). Without it the cluster refuses the objects that use that item, or opens no LAN path. A LAN grant needs the env's ranges, `lan_cidrs` in `values/cluster-vars.yaml`, and a `lan-target` range inside them. A LAN port is refused when core or the node holds it, or another app already does. Removing a grant stops what runs on it within minutes (pods, Jobs, Services and HTTPRoutes that need it, and its LAN allows), and keeps the app's volumes; adding it back restores them.
4. A `backup: true` volume is backed up with the app's own credential while the entry's `backups` is on (the default): `jarvis secrets --env <env>` mints it, and `jarvis validate` fails until it is sealed ([backups.md](backups.md#app-credentials)). To keep the app's volumes out of backups, set `backups: false`.
5. Render the wiring: `jarvis render --env <env>`.
6. Run `jarvis validate`, then commit and push. `jarvis verify --env <env>` then checks the app's namespace, network baseline, grants and hosts.

Changing the app later is a new tag in the apps repository; the env runs it once the app's `version` moves, and `jarvis validate` checks it at that tag against the core the env pins.

## Turn it off

1. Remove its entry from `values/apps.yaml` and render again. Commit and push. Flux prunes the app's Kustomizations, which prune its objects, or its HelmRelease, which uninstalls the chart.
2. Delete what `prune: disabled` kept, as the cluster admin: namespace `app-<name>` (its volumes go with it), ServiceAccount `flux-system/app-<name>` and GitRepositories `flux-system/app-<name>` and `app-<name>-file` (for a chart app, `app-<name>-file` and its chart source `app-<name>`). Retire its backup credential as the backups contract says.
3. With no app left, the env may turn app hosting off: remove `app-hosting` from `values/switches.yaml` and render again. The CRD keeps `prune: disabled`; delete it by hand once no App is left.

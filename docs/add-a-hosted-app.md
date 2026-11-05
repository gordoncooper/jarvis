# Add a hosted app

A hosted app lives in the apps repository, one folder per app, and is untrusted: core decides what it may do, and the cluster holds it to that. The rules are in [platform/app-hosting/CONTRACT.md](../platform/app-hosting/CONTRACT.md). [ci/tests/fixtures/app-hosting/apps/](../ci/tests/fixtures/app-hosting/apps/) holds three complete examples: a plain web app, a stateful one, and one that asks for a risky item.

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
     webPort: 8080          # the container port Traefik reaches, or none
     volumes:               # every PVC the app keeps; [] for none
       - name: data
         backup: true
   ```

   Optional, with their defaults: `resources` (500m CPU, 512Mi memory for the whole app), `internet` (false), `talksTo` and `acceptsFrom` (no other app), `ports` (none), `risky` (nothing), and `envs.<env>` to change `internet` or `resources` in one env.
3. Put the app's objects in `<name>/deploy/`: plain manifests, or a kustomization. Leave `metadata.namespace` unset. Only ConfigMaps, Secrets, Services, PVCs, Deployments, StatefulSets, Jobs, CronJobs, Ingresses, PodDisruptionBudgets and HorizontalPodAutoscalers. Each container runs as non-root (`runAsNonRoot: true`) with `allowPrivilegeEscalation: false`, and pods use only configMap, secret, emptyDir, PVC, projected, downwardAPI, csi or ephemeral volumes. A PVC is named like a declared volume and carries annotation `backup: none` exactly when declared `backup: false`. The Ingress host is `${app_host}`, class `traefik`, entrypoint `websecure`, with no `tls` section.
4. Run `python3 <jarvis checkout>/tools/app-check <name>`. It reports every rule the folder breaks; the apps repository's CI runs the same tool from a pinned commit of this repository.

## Turn it on in an env

In the env's repository:

1. Add the name to `envs/<env>/values/apps.yaml` (names only), and `apps_git_repo_url` to `values/cluster-vars.yaml` if it is not there yet:

   ```yaml
   apps:
     - <name>
   ```

2. If the app asks for a risky item in `spec.risky` or a `lan` port, add the matching exception line, one grant each: `- <name>: privileged`, `root`, `host-network`, `host-path`, `hostname <host>` or `lan-port <port>/<TCP|UDP>`. Without it the cluster refuses the objects that use that item.
3. If a volume is `backup: true`, register the app's backup credential: `make bootstrap-secrets ENV=<env> APP=app-<name> INSTANCE=<path>` ([platform/backups/CONTRACT.md](../platform/backups/CONTRACT.md), App credentials). Until then that volume gets no backup, and `make verify` fails it.
4. Render the wiring: `INSTANCE=<path> python3 tools/lib/render_apps.py --env <env> --output <path>/envs/<env>/flux/generated/apps.yaml`. List `generated/apps.yaml` under `resources` in `envs/<env>/flux/kustomization.yaml` once.
5. Run `INSTANCE=<path> bash ci/run-env-checks`, then commit and push. `make verify ENV=<env>` then checks the app's namespace, network baseline, grants and hosts.

Changing the app later needs nothing in the env: its file and `deploy/` reach the cluster from the apps repository.

## Turn it off

1. Remove its name (and its exception lines) from `values/apps.yaml` and render again. With no app left, delete `generated/apps.yaml` and its line instead. Commit and push. Flux prunes the app's Kustomizations, which prune its objects.
2. Delete what `prune: disabled` kept, as the cluster admin: namespace `app-<name>` (its volumes go with it), ServiceAccount `flux-system/app-<name>`, ConfigMap `flux-system/app-<name>-grants`, GitRepositories `flux-system/app-<name>` and `app-<name>-file`, and ValidatingAdmissionPolicyBindings `app-file-<name>` and `app-grants-<name>`. Retire its backup credential as the backups contract says.

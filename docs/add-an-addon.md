# Add an add-on

An add-on is an optional piece Jarvis ships on top of core. Each env turns it on in its own repository; it is off everywhere else. The rules are in [addons/CONTRACT.md](../addons/CONTRACT.md), and [addons/hello/](../addons/hello) is a complete example.

## Write it

1. Make `addons/<name>/` with a `CONTRACT.md` (template: [CONTRACT.template.md](CONTRACT.template.md)).
2. Add the wiring files and list them in `addons/<name>/kustomization.yaml`, a kustomize `Component` with `resources` only:
   - a Namespace per namespace it uses, labelled `addons.jarvis.io/name: <name>` and annotated `kustomize.toolkit.fluxcd.io/prune: disabled`;
   - ServiceAccount `addon-<name>` in `flux-system`, a Role in each of its namespaces granting only the kinds it ships, and a RoleBinding to that Role for that account, all three annotated `kustomize.toolkit.fluxcd.io/prune: disabled`;
   - a Flux Kustomization `addon-<name>` in `flux-system`, running as `addon-<name>`, with `path: ./addons/<name>/<dir>`, `sourceRef` the root source `flux-system`, and, when its objects use `${...}`, one inline `postBuild.substitute` entry per key (`dns_suffix: ${dns_suffix}`), which the services stage fills. It names no `substituteFrom` source;
   - when it has Secrets, a second Kustomization `addon-<name>-secrets` with `path: ./envs/${jarvis_env}/secrets/<namespace>` and `decryption` with `sops-age`.
3. Put its objects under `addons/<name>/<dir>/`: namespaced, in its own namespaces, with no RBAC or Flux objects. An Ingress follows [core/platform/testapp](../core/platform/testapp/CONTRACT.md): host `<name>.${dns_suffix}`, entrypoint `websecure`, no TLS secret.
4. If it fills a slot, label its Kustomizations `addons.jarvis.io/slot: <slot>` and meet `addons/slots/<slot>/CONTRACT.md`. Depend on slots and platform contracts, never on another add-on by name. Core never names your add-on.
5. Optional standing checks: `addons/<name>/verify.py` registers a component named `<name>` with `verify.registry.register`. `make verify` runs it only on envs that enable the add-on.
6. Run `bash ci/run-checks` and the tests. `ci/checks/addons` reports every rule the add-on breaks.

## Turn it on in an env

In the env's repository:

1. Add the add-on under `addons` in `envs/<env>/values/switches.yaml`, then run `INSTANCE=<path> .venv/bin/python tools/render --env <env>`:

   ```yaml
   addons:
     - <name>
   ```

2. Seal each Secret it reads, one folder per namespace, in the same commit: the add-on's `-secrets` Kustomization is not Ready until its folder holds a file. `make encrypt ENV=<env> NAMESPACE=<namespace> NAME=<name>-<purpose> FILE=<manifest> INSTANCE=<path>`. The manifest carries `kustomize.toolkit.fluxcd.io/prune: disabled`.
3. Run `INSTANCE=<path> bash ci/run-env-checks`, then commit and push.

## Turn it off

1. Remove the add-on's line from `addons`, re-render, commit and push. Flux prunes the add-on's Kustomizations, and each prunes the workloads it applied.
2. Wait until its Kustomizations are gone: `kubectl get kustomizations -n flux-system` lists no `addon-<name>` or `addon-<name>-*`. The tenant account must still exist while they finish, which is why it keeps `prune: disabled`.
3. Delete what `prune: disabled` kept, as the cluster admin:
   - each of its Namespaces (`kubectl delete namespace <namespace>`), which also removes its Roles, RoleBindings, Secrets and any PVCs in them;
   - its tenant account (`kubectl delete serviceaccount -n flux-system addon-<name>`).

   A PVC's restic repository stays under `backup_path/restic/ns-<namespace>/` until you remove it ([core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md)).
4. Remove the add-on's sealed files from `envs/<env>/secrets/<namespace>/` in the env repository once nothing needs them.

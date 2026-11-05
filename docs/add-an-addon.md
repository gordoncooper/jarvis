# Add an add-on

An add-on is an optional piece Jarvis ships on top of core. Each env turns it on in its own repository; it is off everywhere else. The rules are in [services/CONTRACT.md](../services/CONTRACT.md), and [services/hello/](../services/hello/) is a complete example.

## Write it

1. Make `services/<name>/` with a `CONTRACT.md` (template: [CONTRACT.template.md](CONTRACT.template.md)).
2. Add the wiring files and list them in `services/<name>/kustomization.yaml`, a kustomize `Component` with `resources` only:
   - a Namespace per namespace it uses, labelled `addons.jarvis.io/name: <name>` and annotated `kustomize.toolkit.fluxcd.io/prune: disabled`;
   - ServiceAccount `addon-<name>` in `flux-system`, a Role in each of its namespaces granting only the kinds it ships, and a RoleBinding to that Role for that account, all three annotated `kustomize.toolkit.fluxcd.io/prune: disabled`;
   - a Flux Kustomization `addon-<name>` in `flux-system`, running as `addon-<name>`, with `path: ./core/services/<name>/<dir>`, `sourceRef` the root source `flux-system`, and `postBuild.substituteFrom` the `cluster-vars` ConfigMap when it uses `${...}`;
   - when it has Secrets, a second Kustomization `addon-<name>-secrets` with `path: ./envs/${jarvis_env}/secrets/<namespace>` and `decryption` with `sops-age`.
3. Put its objects under `services/<name>/<dir>/`: namespaced, in its own namespaces, with no RBAC or Flux objects. An Ingress follows [platform/testapp](../platform/testapp/CONTRACT.md): host `<name>.${dns_suffix}`, entrypoint `websecure`, no TLS secret.
4. If it fills a slot, label its Kustomizations `addons.jarvis.io/slot: <slot>` and meet `services/slots/<slot>/CONTRACT.md`. Depend on slots and platform contracts, never on another add-on by name. Core never names your add-on.
5. Optional standing checks: `services/<name>/verify.py` registers a component named `<name>` with `verify.registry.register`. `make verify` runs it only on envs that enable the add-on.
6. Run `bash ci/run-checks` and the tests. `ci/checks/addons` reports every rule the add-on breaks.

## Turn it on in an env

In the env's repository:

1. Add the add-on under `spec.components` of `envs/<env>/flux/kustomizations/services.yaml`:

   ```yaml
   spec:
     components:
       - <name>
   ```

2. Seal each Secret it reads, one folder per namespace, in the same commit: the add-on's `-secrets` Kustomization is not Ready until its folder holds a file. `make seal ENV=<env> NAMESPACE=<namespace> NAME=<name>-<purpose> FILE=<manifest> INSTANCE=<path>`. The manifest carries `kustomize.toolkit.fluxcd.io/prune: disabled`.
3. Run `INSTANCE=<path> bash ci/run-env-checks`, then commit and push.

To turn it off, remove the line. Flux prunes the add-on's Kustomizations and the workloads they applied. Its Namespaces, Secrets, tenant account, Roles and RoleBindings stay until you delete them, after its Kustomizations are gone.

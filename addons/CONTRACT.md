# CONTRACT: add-ons

Every add-on meets this contract. An add-on is a limited-tenant swappable service on top of core (the chat bot slot, the code factory and similar), not a feature such as app hosting, the off-host copy or observability ([schema/CONTRACT.md](../schema/CONTRACT.md), `values/switches.yaml`). Add-ons are optional and off by default. Each env turns on the ones it wants, in its own repository. Core and add-ons form one trusted zone. Hosted apps ([core/feature/app-hosting/CONTRACT.md](../core/feature/app-hosting/CONTRACT.md)) are not add-ons.

How to write one: [docs/add-an-addon.md](../docs/add-an-addon.md).

## Offers

- One home per add-on: `addons/<name>/`, holding its own `CONTRACT.md`. Nothing about the add-on lives anywhere else in core.
- The services stage applies the add-on's wiring (below) as the platform account, so the add-on never creates its own namespace or the identity it runs as.
- Slots. A slot is a named role one add-on fills, such as the chat bot. Each slot's contract lives at `addons/slots/<slot>/CONTRACT.md`, not under any add-on. An env enables at most one add-on per slot. Core and other add-ons refer to the slot, never to the add-on that fills it.
- The name `apps` is reserved: hosted apps are `<name>.apps.<dns suffix>`, so an add-on named `apps` would claim every app's host. `ci/checks/addons` refuses it.
- The `${...}` keys the env's `cluster-vars` ConfigMap publishes, filled into the add-on's wiring by the services stage. The add-on's Kustomizations pass the ones they need on as inline `postBuild.substitute` values.

## Expects

An add-on's folder holds:

- `kustomization.yaml`: a kustomize `Component` that lists files only. An env lists the folder name under `addons` in `values/switches.yaml`, and the render writes it under `spec.components` of its services Kustomization. The listed files are its wiring:
  - **Namespaces.** One or more, named `<name>` or `<name>-<part>`, each labelled `addons.jarvis.io/name: <name>` and annotated `kustomize.toolkit.fluxcd.io/prune: disabled`. A name may not start with `app-` (reserved for hosted apps), `platform-`, `kube-` or `flux-`, and may not be `default`.
  - **Tenant identity.** Exactly one ServiceAccount, `addon-<name>` in `flux-system`, plus Roles and RoleBindings in the add-on's own namespaces only, each annotated `kustomize.toolkit.fluxcd.io/prune: disabled`: kustomize-controller skips a Kustomization's cleanup when the account it impersonates is gone, so the account must outlive the add-on's Kustomizations. A RoleBinding binds only that account, to a Role the add-on ships in the same namespace. A Role grants nothing on RBAC, Flux, admission or CRD APIs, nothing on `namespaces`, and no wildcard. No pod mounts the tenant's token; kustomize-controller impersonates it.
  - **Flux Kustomizations.** In `flux-system`, named `addon-<name>` or `addon-<name>-<part>`, each running as the tenant account and reading the root source `flux-system`, with no `patches`, `images`, `components`, name prefix or suffix, or `kubeConfig`. It names no substitution source (`postBuild.substituteFrom`, optional or not): kustomize-controller reads those in `flux-system` with its own rights. A value the add-on needs is an inline `postBuild.substitute` entry such as `dns_suffix: ${dns_suffix}`, which the services stage fills from the env's published `cluster-vars` keys. A Kustomization's path is under `./addons/<name>/`, or for sealed Secrets `./envs/${jarvis_env}/secrets/<namespace>` for one of its namespaces. `dependsOn` names only the add-on's own Kustomizations.
  - Nothing else. In particular, nothing cluster-scoped but its Namespaces.
- The objects its Kustomizations apply are namespaced, land in its own namespaces (set `targetNamespace` when there is one), and include no RBAC or Flux objects.
- **Secrets.** Sealed per env under `envs/<env>/secrets/<namespace>/` in the instance, with `make encrypt ENV=<env> NAMESPACE=<namespace> NAME=<name>-<purpose>`. Each carries `kustomize.toolkit.fluxcd.io/prune: disabled`. The add-on's decrypting Kustomization lives in `flux-system`, so kustomize-controller reads `flux-system/sops-age` there and applies the plain Secret into the add-on's namespace as the tenant. The tenant has no rights in `flux-system` and never sees the key. Only Kustomizations in `flux-system` set `decryption`.
- **Volumes.** Stateless by default. A PVC is enrolled by GeneratingPolicy `pvc-backup` with the core restic credential, because add-ons are in the trusted zone ([core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md)). An add-on writes no VolSync object or restic URL of its own. `backup: none` follows that contract's allow-list.
- **URL and Ingress.** Host `<name>.${dns_suffix}` or `<part>.<name>.${dns_suffix}`, class `traefik`, annotations `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and `traefik.ingress.kubernetes.io/router.tls: "true"`, and no `spec.tls` secret: the wildcard on TLSStore `default` serves it ([core/platform/testapp/CONTRACT.md](../core/platform/testapp/CONTRACT.md) is the pattern). No LoadBalancer Service.
- **Slot.** An add-on that fills a slot labels its Flux Kustomizations `addons.jarvis.io/slot: <slot>`, and meets that slot's contract. `addons/slots/<slot>/` must exist.
- **Depends on** platform contracts and slot contracts only, never on another add-on by name.
- Workloads meet `ci/checks/manifest-lint` (probes, memory request and limit, CPU request, stdout logs) and `ci/checks/digest-pin`.

## Health

The add-on's Flux Kustomizations are Ready; `make verify` already waits on every Kustomization. An add-on may add standing checks in `addons/<name>/verify.py`, which registers one component named after the add-on with `verify.registry.register`. `make verify` loads that file only when the env enables the add-on, so a disabled add-on's checks are skipped and a core-only env runs core's checks alone.

## Config

- Turn on: add `- <name>` under `addons` in `envs/<env>/values/switches.yaml` and run `INSTANCE=<path> .venv/bin/python tools/render --env <env>`. Turn off: remove the line and re-render. An empty list means core-only. `ci/checks/env-schema` rejects an unknown add-on, the same add-on twice, and two add-ons for one slot. Once the env is bootstrapped, it requires each sealed-Secret folder the add-on decrypts.
- Turning an add-on off prunes its Kustomizations, and each of them prunes the workloads it applied. Its Namespaces, Secrets, tenant account, Roles and RoleBindings keep `prune: disabled` and stay until the operator deletes them ([docs/add-an-addon.md](../docs/add-an-addon.md#turn-it-off)); the leftover account holds rights only in the leftover namespaces.
- The add-on's own settings come from `cluster-vars` keys that already exist, or from its sealed Secrets. A new env key is added to `schema/` with its first consumer.

## Proven by

- `ci/checks/addons` (the Component lists files only, its slot exists, no add-on is named `apps`, core never names an add-on, and per enabled add-on: a services stage that lists add-ons runs as the platform account, the wiring kinds, namespace names, `prune: disabled` on the wiring, the tenant's Roles and RoleBindings, each Kustomization's source, path, extra fields and `dependsOn`, and the objects its Kustomizations apply). `ci/tests/test_addons.py` seeds each failure.
- `ci/checks/flux-identity` (the services stage may run as the platform account, the add-on's Kustomizations run as their declared tenant and never as the platform account, no path declares its own account, and only `flux-system` decrypts).
- `ci/checks/env-schema` (unknown add-on, duplicate, slot conflict, missing sealed folder), `ci/checks/contract-md` (each add-on and slot has a `CONTRACT.md`), `ci/checks/flux-prune` and `ci/checks/flux-substitute` on each add-on Kustomization's path.
- `ci/tests/test_addons.py` runs every env-level check on a bootstrapped copy of the example env with no add-on, and on one with the tests' sample add-on and its sealed Secret.

## Limits

- The wiring keeps `prune: disabled`, so a Role or RoleBinding renamed or removed while the add-on stays on is left on the cluster until deleted by hand. A RoleBinding's `roleRef` cannot change in place: give the new one a new name.
- An enabled add-on's sealed-Secret folder must hold a file before its `-secrets` Kustomization can go Ready; Git keeps no empty folder.
- Feature or add-on: ask in order, and the first yes decides. Does every env need it to be a working, recoverable cluster? Then it is platform (`core/platform/`). Does it ship cluster-scoped objects (CRDs, webhooks or policies, ClusterRoles, DaemonSets, host access), or do the platform or features consume it? Then it is a feature (`core/feature/`). Otherwise it is an add-on: a namespaced service people use, depending on platform and feature contracts, never on another add-on by name.
- So an add-on ships nothing cluster-scoped but its own Namespaces. `ci/checks/addons` enforces it on each add-on's wiring and on every object its Kustomizations apply. One that needs more is a feature, or a platform component that owns those objects with a contract the add-on depends on.
- Hosted apps are not add-ons: each has its own wiring and an `app-<name>` namespace ([core/feature/app-hosting/CONTRACT.md](../core/feature/app-hosting/CONTRACT.md)). The app policies leave add-on namespaces alone.

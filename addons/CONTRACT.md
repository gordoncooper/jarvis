# CONTRACT: add-ons

An add-on is a namespaced, swappable service on top of core (a chat bot, the code factory), off until an env lists it under `addons` in `values/switches.yaml`. Core and add-ons are one trusted zone; hosted apps are not add-ons. How to write one: [docs/add-an-addon.md](../docs/add-an-addon.md).

## Offers

| What | Detail |
| --- | --- |
| One home per add-on | `addons/<name>/` with its own `CONTRACT.md`; nothing about it lives elsewhere in core |
| Wiring applied for it | The services stage applies the add-on's wiring as the platform account, so it never creates its own namespace or identity |
| Slots | `addons/slots/<slot>/CONTRACT.md`. One add-on per slot per env. Core and other add-ons refer to the slot, never the add-on |
| Values | The env's published `cluster-vars` keys, as inline `postBuild.substitute` entries on its Kustomizations |
| Backups | Its PVCs are enrolled with core's credential ([backups](../core/platform/backups/CONTRACT.md)) once core's namespace selectors list layer `addon`, with the first add-on; it writes no VolSync object or restic URL |
| Exposure | The core door ([traefik](../core/platform/traefik/CONTRACT.md)) and its wildcard serve `<name>.${dns_suffix}`, once the door's selector lists layer `addon`, with the first add-on |
| Checks | Optional `addons/<name>/verify.py`, registered with `verify.registry.register`, loaded only when the env lists the add-on |

## Expects

| In `addons/<name>/` | Rule |
| --- | --- |
| `kustomization.yaml` | A kustomize `Component` listing files only |
| Namespaces | Named `<name>` or `<name>-<part>`, labelled `addons.jarvis.io/name: <name>`, `prune: disabled`. Not `app-*`, `platform-*`, `kube-*`, `flux-*` or `default` |
| Tenant | One ServiceAccount `flux-system/addon-<name>` and Roles and RoleBindings in its own namespaces only, `prune: disabled`. A Role grants nothing on RBAC, Flux, admission, CRDs or `namespaces`, and no wildcard. No pod mounts the token |
| Flux Kustomizations | In `flux-system`, named `addon-<name>[-<part>]`, as the tenant, from root source `flux-system`, path under `./addons/<name>/` or `./envs/${jarvis_env}/secrets/<namespace>` for its own namespace. No `patches`, `images`, `components`, prefix, suffix, `kubeConfig` or `substituteFrom`; `dependsOn` only its own |
| Applied objects | Namespaced, in its own namespaces, no RBAC or Flux objects. Nothing cluster-scoped but its Namespaces |
| Secrets | Sealed under `envs/<env>/secrets/<namespace>/` with `jarvis encrypt --env <env> --namespace <namespace> --name <name>-<purpose>`, `prune: disabled`; decrypted only by its Kustomization in `flux-system` |
| HTTPRoute | Hostname `<name>.${dns_suffix}` or `<part>.<name>.${dns_suffix}`, on Gateway `platform-gateway/jarvis` section `core`. No LoadBalancer |
| Slot | Label `addons.jarvis.io/slot: <slot>` on its Kustomizations; the slot exists |
| Name | Not `apps` (hosted apps own `*.apps.<dns_suffix>`) |
| Dependencies | Platform, feature and slot contracts only, never another add-on |
| Workloads | `policies` (`jarvis validate`) |

## Health

| Check | Pass |
| --- | --- |
| Its Flux Kustomizations | Ready (`jarvis verify` waits on every Kustomization) |
| Its `verify.py`, if any | its component passes in `jarvis verify` |
| `addons`, `flux-identity`, `flux-prune` (`jarvis validate`) | its wiring and applied objects meet the table above |

## Limits

- Turning it off prunes its Kustomizations and their workloads; Namespaces, Secrets, tenant and RBAC stay (`prune: disabled`) until deleted by hand ([docs/add-an-addon.md](../docs/add-an-addon.md#turn-it-off)). A renamed Role or RoleBinding stays too.
- A sealed-Secret folder needs a file before its Kustomization can go Ready; Git keeps no empty folder.

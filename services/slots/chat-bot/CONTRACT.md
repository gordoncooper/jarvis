# CONTRACT: chat-bot slot

The chat bot is a slot, not a component. One add-on fills it, and an env enables at most one. Core and other add-ons depend on this contract, never on the add-on that fills it, so swapping the chat bot means enabling a different add-on and changing nothing in core.

This contract covers hosting, storage, backups, secrets and the URL. The assistant API and the model provider join it in the task that builds the first chat bot.

## Offers

What any filler gives the rest of the env:

- **Hosting.** Runs in the env's own cluster as an add-on, within [the add-on contract](../../CONTRACT.md). Its Flux Kustomizations carry the label `addons.jarvis.io/slot: chat-bot`, which is how core and other add-ons find the filler without its name.
- **URL.** One HTTPS endpoint, `https://<host>.<dns_suffix>/`, through a Traefik Ingress on `websecure` with the internal CA wildcard, published by App DNS. It is reachable from the LAN only.
- **Data survives a restore.** Every volume it keeps is a PVC that ClusterPolicy `pvc-backup` enrolls, so `make restore APP=<namespace>` and `make restore-all` put it back.

## Expects

What the filler requires, all from core:

- **Hosting.** The services stage of an env that lists it under `spec.components`. Its namespaces, tenant identity and Roles follow the add-on contract.
- **Storage.** PVCs on the cluster's default StorageClass (k3s local-path). No hostPath and no PersistentVolume of its own.
- **Backups.** Enrollment by `pvc-backup` with the core restic credential, on the env's `backup_schedule`. Its state is not rebuildable, so its PVCs never carry `backup: none`.
- **Secrets.** Sealed under `envs/<env>/secrets/<namespace>/`, decrypted by a Kustomization in `flux-system` into its own namespace. It holds no credential for core.
- **Certificates and DNS.** The wildcard on TLSStore `default` (cert-manager contract) and App DNS for the zone.

## Health

The filler's Flux Kustomizations are Ready and its URL answers over HTTPS with a certificate that chains to the internal CA. The filler adds that probe as its own `verify.py`.

## Config

- An env picks the filler by listing exactly one add-on that carries the `chat-bot` slot label. `ci/checks/env-schema` rejects a second one.
- No env key belongs to the slot yet.

## Proven by

- `ci/checks/env-schema`: at most one enabled add-on per slot (`ci/tests/test_addons.py` seeds two for this slot).
- `ci/checks/addons`: a slot an add-on declares has a folder here, and core never names the filler.
- No add-on fills this slot yet.

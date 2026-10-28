# CONTRACT: platform-backups

## Offers

Namespace `platform-backups` and bootstrap restic credentials for VolSync (expanded in p0b per Backups contract).

## Expects

- Flux SOPS decryption via `flux-system/sops-age` (R25).
- `envs/<env>/secrets/backups-restic.sops.yaml` (D10 decrypt proof in P0a).

## Health

Namespace exists; Secret `backups-restic` is present (value not logged).

## Config

Repository path and retention come from `cluster-vars` at reconcile time (p0b).

## Proven by

`ci/tests/test_flux_entry.py` (SOPS-encrypted secret in Git, platform-configs decryption settings).

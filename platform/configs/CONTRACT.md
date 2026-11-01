# CONTRACT: platform configs

The one list of core components on the platform-configs Flux stage. A new platform config joins this list once, not once per env.

## Offers

- `kustomization.yaml` lists `backups`, `cert-manager/config` and `testapp`, in that order. Each component's own contract describes its objects.

## Expects

- Each env's `envs/<env>/flux/platform-configs/kustomization.yaml` lists `platform/configs` and that env's own SOPS secrets, nothing else. The env's Flux `platform-configs` Kustomization names that env folder as its path, decrypts with `sops-age`, depends on `platform-controllers`, and substitutes from `cluster-vars`.
- The CRDs these objects use come from `platform-controllers` (`platform/kustomization.yaml`). Nothing in this list is also on that path.

## Health

Flux `platform-configs` reports Ready.

## Config

- Order is the render order. Moving an object between this list and another Flux path takes two commits (add, reconcile, then remove).

## Proven by

- `ci/tests/test_flux_entry.py` (kustomize build and schema check of every Flux path, no object on two paths, the four standard Kustomizations, each env's platform-configs list is `platform/configs` plus its own SOPS secrets).
- `ci/tests/test_prune_guards.py` (protected kinds on this path keep `prune: disabled`).

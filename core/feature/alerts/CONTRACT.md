# CONTRACT: alerts

Flux's notification-controller posts each failed reconcile to one target the env owner picks. A feature: `alerts` under `features`.

## Offers

| What | Detail |
| --- | --- |
| Alert `flux-system/alerts` | Every Kustomization and HelmRelease in `flux-system`, hosted apps' included; `error` events only |
| Provider `flux-system/alerts` | Type `${alerts_provider}`, address from Secret `alerts-notify` |

## Expects

| What | From |
| --- | --- |
| `alerts_provider` | `values/cluster-vars.yaml` ([reference](../../../docs/reference.md)): a [Flux Provider type](https://fluxcd.io/flux/components/notification/providers/) whose whole target is its `address` |
| Secret `flux-system/alerts-notify`, key `address` | `secrets/alerts-notify.sops.yaml`, sealed by the owner with `jarvis encrypt` |
| notification-controller | The FluxInstance ([flux-operator](../../platform/flux-operator/CONTRACT.md)) |

## Health

| Check | Pass |
| --- | --- |
| Kustomization `platform-configs` | Ready: the Provider, the Alert and Secret `alerts-notify` are applied |

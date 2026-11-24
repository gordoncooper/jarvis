# CONTRACT: hello (sample add-on)

The tests' sample add-on: it proves [the add-on contract](../CONTRACT.md) end to end and fills no slot.

## Offers

| What | Detail |
| --- | --- |
| Namespace `hello` | Labelled `addons.jarvis.io/name: hello`, `prune: disabled` |
| `https://hello.<dns_suffix>/` | Deployment, Service on 8080 and HTTPRoute `hello` on the core door; `GET /` returns `hello` and a newline |
| Secret `hello-greeting` | From `envs/<env>/secrets/hello/` |

## Expects

| What | Rule |
| --- | --- |
| Listing | `hello` under `addons` in `values/switches.yaml` |
| Tenant | `flux-system/addon-hello` with Role `addon-hello` in `hello`: ConfigMaps, Secrets, Services, Deployments, HTTPRoutes |
| Kustomizations | `addon-hello` (`./addons/hello/app`, inline `dns_suffix`) and `addon-hello-secrets` (`./envs/${jarvis_env}/secrets/hello`, `sops-age`), both as the tenant, `targetNamespace: hello` |
| Sealed file | `jarvis encrypt --env <env> --namespace hello --name hello-greeting <manifest>` |

## Health

| Check | Pass |
| --- | --- |
| `addons/hello/verify.py` | Deployment `hello` has a ready replica; Secret `hello-greeting` has data keys (names only) |

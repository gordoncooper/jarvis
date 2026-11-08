# CONTRACT: hello (sample add-on)

The sample add-on. It fills no slot and exists to prove [the add-on contract](../CONTRACT.md) end to end: its own namespace, a tenant identity, one sealed Secret decrypted into its namespace, and an Ingress. The example env enables it so CI renders it. A real env does not need it.

## Offers

- Namespace `hello`, labelled `addons.jarvis.io/name: hello`, with `prune: disabled`.
- Deployment `hello`, ClusterIP Service `hello` on port 8080, Ingress `hello` on `hello.<dns_suffix>`. `GET /` returns `hello` and a trailing newline.
- Secret `hello-greeting` in `hello`, from the env's `envs/<env>/secrets/hello/`.

## Expects

- The env lists `hello` under `spec.components` of its services Kustomization.
- Tenant account `flux-system/addon-hello`, with Role `addon-hello` in `hello` (all with `prune: disabled`): ConfigMaps, Secrets, Services, Deployments and Ingresses there, nothing else. Flux Kustomization `addon-hello` applies `./addons/hello/app` with the inline value `dns_suffix`, filled by the services stage, and `addon-hello-secrets` applies `./envs/${jarvis_env}/secrets/hello` with `sops-age`, both as that account, both with `targetNamespace: hello`.
- Once the env is bootstrapped, at least one sealed file in `envs/<env>/secrets/hello/`. Seal `hello-greeting` with `make encrypt ENV=<env> NAMESPACE=hello NAME=hello-greeting FILE=<manifest>`. The manifest carries `kustomize.toolkit.fluxcd.io/prune: disabled`.
- TLSStore `default`, Traefik on `websecure`, and App DNS, as the platform canary uses them.

## Health

`make verify` loads `addons/hello/verify.py` when the env enables `hello`: Deployment `hello` has a ready replica and Secret `hello-greeting` has data keys (it reads key names only). Container resources: memory request `32Mi`, limit `64Mi`, CPU request `10m`. Readiness and liveness are HTTP `GET /` on 8080.

## Config

- Image `nginxinc/nginx-unprivileged:1.31.7-alpine-slim`, digest-pinned, uid 101, read-only root filesystem, the same build as the platform canary. Renovate updates both.
- No PVC, so the backup policy has nothing to enroll.

## Proven by

- `ci/checks/addons`, `ci/checks/flux-identity`, `ci/checks/manifest-lint` and `ci/checks/digest-pin` on the example env.
- `ci/tests/test_addons.py` builds a bootstrapped copy of the example env with a placeholder Secret sealed to a throwaway key in `secrets/hello/`, and runs every env-level check on it. That proves the sealed file renders on the decrypting Kustomization, which sets `targetNamespace: hello`. The tenant's Role is what limits the plain Secret to `hello` on the cluster; nothing in CI decrypts it. `ci/tests/test_flux_entry.py` renders and schema-checks each of its paths.
- `ci/tests/test_verify.py` runs its standing check with kubectl stubbed.

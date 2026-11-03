# Vendored kubeconform schemas

`test_flux_entry.py` validates rendered manifests with these files only. No
schema is fetched at test time.

Regenerate everything the script owns:

```
python3 ci/tests/fixtures/schemas/refresh
```

Then commit the diff.

## Sources

- **Kubernetes** (`v<minor>.0-standalone-strict/`):
  [yannh/kubernetes-json-schema](https://github.com/yannh/kubernetes-json-schema)
  `master`, the standalone-strict tree for the k3s pin's minor
  (`schema.env.kubernetes_version_from_k3s`). Only the kinds a flux-layer
  `kustomize build` emits. Bump the k3s minor, re-run the script, commit the
  new folder (the script deletes the old one).
- **VolSync** (`volsync.backube/`): CRD YAML from
  [backube/volsync](https://github.com/backube/volsync) at the Helm chart
  tag in `platform/backups/controller/helmrelease.yaml`. Converted with the
  same `additionalProperties: false` pass kubeconform's `openapi2jsonschema`
  uses, so an unknown `spec` field fails.
- **Other CRDs the render uses**:
  [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog) at the
  commit in `refresh` (`CRD_CATALOG_REF`). Flux, cert-manager, Kyverno,
  Traefik.
- **Hand-written** (the script does not touch these):
  `helm.cattle.io/helmchartconfig_v1.json` and
  `apiextensions.k8s.io/customresourcedefinition_v1.json`. Neither upstream
  set ships a usable document for those kinds at the pinned Kubernetes
  minor.

If a new kind appears in the render, kubeconform fails and names the kind.
Add its file name to `K8S_FILES` or `CRD_FILES` in `refresh` and re-run.

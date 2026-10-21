# env-00

Never-deployed example environment (D43): a pretend seven-node cluster used to prove core against multi-node inventory and every env-schema key.

- Values are invented; nothing is copied from env-01.
- No `secrets/`, SOPS files, or `flux/`.
- `inventory/hosts.yml` lists the seven example hosts; `node_roles` and `data_disk` land in catch-up PR 3 (`p0a-11a`).

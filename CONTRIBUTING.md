# Contributing

Changes come as pull requests against `main`. The repository owner reviews and merges every one.

## Before you start

- Read [docs/deploy.md](docs/deploy.md) for what this repository runs, and the `CONTRACT.md` of each component you touch ([docs/README.md](docs/README.md) lists them).
- Install [mise](https://mise.jdx.dev/getting-started.html), then run `make deps` once. It installs the pinned CLIs (`mise.toml`, checksums in `mise.lock`) and every Python package (`uv.lock`) into `.venv`. Outside `make`, run a tool with `mise exec -- <command>`.

## Making a change

- Branch from current `main`. One pull request per change; keep unrelated changes apart.
- Docs move with the code: what a component offers, expects and how its health is checked goes in its `CONTRACT.md` in the same pull request.
- Prefer deleting to adding, and use the upstream tool (Flux, kustomize, Helm, Kyverno, Renovate) as it is rather than wrapping it.
- No LAN IP or numeric user ID in plaintext, and no secret in the clear. Secrets are SOPS-encrypted under `envs/<env>/secrets/`.
- Pin what you add (chart versions, image digests, binary checksums) and let Renovate move it.

## Before you push

```bash
bash ci/run-checks
.venv/bin/pytest ci/tests/<the tests near your change>
```

CI runs every check and the full suite at the head commit. Fill in the pull request template, starting with its net lines.

## Coding agents

Agents start at [AGENTS.md](AGENTS.md) and follow [agents/coding-agents.md](agents/coding-agents.md). A second session reviews each of their pull requests with [agents/review.md](agents/review.md).

## License

Contributions are licensed under the [Apache License 2.0](LICENSE), the license of this repository.

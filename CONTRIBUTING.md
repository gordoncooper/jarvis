# Contributing

Changes come as pull requests against `main`. The repository owner reviews and merges every one.

## Before you start

- Read [docs/deploy.md](docs/deploy.md) for what this repository runs, and the `CONTRACT.md` of each component you touch ([docs/README.md](docs/README.md) lists them).
- Run `make deps` once. It installs the pinned CLIs into `~/.local/bin` and the Python dev dependencies into `.venv`.

## Making a change

- Branch from current `main`. One pull request per change; keep unrelated changes apart.
- Docs move with the code: a new expectation, port, permission or operator step goes in the component's `CONTRACT.md` in the same pull request.
- No LAN IP or numeric user ID in plaintext, and no secret in the clear. Secrets are SOPS-encrypted under `envs/<env>/secrets/`.
- Pin what you add (chart versions, image digests, binary checksums) and let Renovate move it.

## Before you push

```bash
bash ci/run-checks
.venv/bin/pytest ci/tests/<the tests near your change>
```

CI runs every check and the full suite at the head commit. Fill in the pull request template; leave its **Ready to merge** boxes for the reviewer.

## Coding agents

Agents start at [AGENTS.md](AGENTS.md) and follow [agents/coding-agents.md](agents/coding-agents.md).

## License

Contributions are licensed under the [Apache License 2.0](LICENSE), the license of this repository.

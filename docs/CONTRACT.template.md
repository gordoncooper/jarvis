# CONTRACT: [name]

One or two sentences: what the component is, and where its runbooks live if it has any.

## Offers

| What | Detail |
| --- | --- |
| A name a caller may rely on | What it is, in the fewest words that let a caller use it |

## Expects

| What | From |
| --- | --- |
| What it needs from elsewhere | Who provides it |

## Health

| Check | Pass |
| --- | --- |
| A probe, or a `jarvis verify` / `--smoke` line | What passing looks like |

## Limits

Optional: what it deliberately does not do.

A contract changes with its component. Rationale goes in a code comment or the PR, runbooks in `docs/`, and rules in the tests that check them.

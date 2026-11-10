# CONTRACT: [name]

## Offers

What callers may rely on from this component or contract.

## Expects

What this component or contract requires from elsewhere.

## Health

How to tell it is working: its probes, and what `jarvis verify` (and `--smoke`) checks, or "not applicable" with the reason.

## Config

Settings, where they live, and allowed values.

## Operations

Optional. Steps to run, rotate, or restore this component. Omit this heading when there are no steps.

## Limits

Optional. What this component deliberately does not do. Omit this heading when there is nothing to say.

A contract names rules, not tests: each test states the rule it checks (`Rule: …` in its docstring), and a change to this folder runs every test that names it, so a list of tests here would only go stale.

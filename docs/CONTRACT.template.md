# CONTRACT: [name]

## Offers

What callers may rely on from this component or contract.

## Expects

What this component or contract requires from elsewhere.

## Health

How to tell it is working (probe, check, or “not applicable” with reason).

## Config

Settings, where they live, and allowed values.

## Proven by

CI rule checks (`ci/checks/`) and/or standing checks (`make verify ENV=<env>`) that enforce this contract.

## Operations

Optional. Steps to run, rotate, or restore this component. Omit this heading when there are no steps.

## Limits

Optional. What this component deliberately does not do. Omit this heading when there is nothing to say.

# Project context for precheck reviewers

This file is read by **every** precheck reviewer (in its own context, not the
main session). Describe what reviewers can't infer from the diff alone. Keep it
tight — it's prepended to each reviewer's focus.

You can pull in other in-repo files with `@path` or `@{path}` (like CLAUDE.md),
e.g. `See @docs/architecture.md`. The `@ref` stays in the text and the file's
contents are appended below it. Paths resolve from the repo root; references are
de-duped and expand one level deep. (Works the same in `<dimension>.md` files.)

## Stack & architecture
<!-- e.g. Go services + Postgres; React/TypeScript frontend; deployed on AWS ECS. -->
<!-- Note the major modules and how they talk to each other. -->

## Priorities
<!-- What matters most here. e.g. "Latency-sensitive API; correctness over cleverness." -->
<!-- For efficiency reviews, rank resources: e.g. CPU > memory; network rarely matters. -->

## Security / threat model
<!-- Trust boundaries that matter: who can reach what, what's untrusted input. -->
<!-- e.g. "All /admin routes require role check; D1 queries must be parameterized." -->

## Conventions
<!-- Idioms and rules specific to this repo that reviewers should enforce. -->

## Where things live
<!-- Plans: e.g. docs/plans/   Docs: e.g. docs/ + per-package README.md -->
<!-- Documentation granularity: e.g. "module-level only; don't ask for per-function docs." -->

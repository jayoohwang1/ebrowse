# 0004 — argparse over typer/click

Status: accepted (2026-07-03)

## Context

The original design listed typer as the CLI dependency. But the primary consumer of
`--help` output is an LLM agent, and typer/click emit rich-formatted help (boxes, ANSI
color) that is token-noisy and hard to keep dense.

## Decision

Plain stdlib argparse with `RawDescriptionHelpFormatter`. Help text is written for LLM
consumption: short, example-first, ≤ 6 lines per verb, and the root help states the
operating loop.

## Consequences

- Full control over help density; one fewer dependency.
- Verb registration is more verbose than decorator-style CLIs (see the verb-table
  refactor direction if the plumbing gets heavier).

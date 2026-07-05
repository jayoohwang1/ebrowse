# 0005 — Hand-rolled MCP server, no SDK dependency

Status: accepted (2026-07-03)

## Context

`ebrowse mcp` needs to speak Model Context Protocol over stdio so MCP hosts can use
the browser without shell access. The official MCP SDK covers far more of the protocol
(resources, prompts, sampling, transports) than the tool needs.

## Decision

A ~250-line newline-JSON-RPC stdio loop implementing exactly `initialize`,
`tools/list`, and `tools/call`. Zero new dependencies. Six tools
(`browse_open/outline/expand/act/query/screenshot`), with `browse_act` multiplexing
all action verbs behind one schema to keep the host's schema token cost low. Tool text
is the renderer output verbatim; screenshots return MCP image content. Tool failures
return `isError: true` with the CommandError text (recovery action included), never
protocol-level errors. The MCP process is a thin client to the same daemon as the CLI,
so both share browser state.

## Consequences

- No SDK version churn; the protocol subset is small enough to own.
- If the tool surface grows or hosts demand newer protocol revisions, revisit — the
  break-even vs. the SDK shifts with every tool added.
- MCP tool schemas are hand-maintained alongside the CLI argparse definitions (they
  can drift; the e2e MCP test is the guard).

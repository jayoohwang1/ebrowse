# 0016 — Pi session transcript plus observer extension for conversation traces

Status: accepted (2026-07-19)

## Context

Tool-call `step` records cannot reconstruct what an agent saw: they flatten
assistant blocks, omit assistant-only output and non-browser tools, and do not
contain Pi's effective system prompt. Streaming events contain cumulative
partial messages and previously caused quadratic raw-event growth.

## Decision

Keep `step` as the browser-analysis record, but add finalized conversation
messages and join steps by Pi message/tool-call id. Parse the saved Pi session
(with finalized `message_end` events as the timeout fallback), preserving block
order and never persisting `message_update`. Capture the exact starting prompt
at the harness boundary. Load a small eval-owned Pi extension that records
`ctx.getSystemPrompt()` at `agent_start`, after Pi has assembled and modified
the effective prompt.

## Consequences

- The viewer can render the complete conversation and attach browser ground
  truth to the tool result that caused it; old step-only traces still render.
- Pi prompt construction is not duplicated in Python and follows Pi upgrades.
- The normal trace reflects Pi's agent-state prompt, not an opt-in forensic copy
  of every provider-specific serialized request.
- Unusually large transcript blocks use content-addressed blobs; streaming
  deltas remain excluded.

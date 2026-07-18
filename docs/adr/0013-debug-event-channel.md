# 0013 — Contextvar debug-event channel with request-id join

Status: accepted (2026-07-18)

## Context

The eval harness needs to correlate one `ebrowse` CLI call with what happened
inside the daemon (phase timings, ref economics, diff verdicts, anomalies).
Two constraints collide: `core/` must stay pure (no I/O), and default runs must
be byte-identical with zero overhead. Naive options fail one or the other:
loguru/logging calls inside core are I/O and formatting cost even when
filtered; threading an `events: list` parameter through every core function
changes frozen call signatures everywhere.

## Decision

- **Two tiers.** Tier 1 (this channel) is lean facts + anomalies, cheap enough
  to leave on for whole eval runs. Full decision traces (why the splitter chose
  a boundary, per-candidate locate scoring) stay out — they belong to offline
  replay over captured DomSnapshots, which are already pure-function inputs.
- **Contextvar recorder, daemon-flushed.** `debug.emit()` appends a plain
  dataclass to an in-memory recorder installed per request by the daemon; when
  no recorder is installed (the default) it is one contextvar read. Core code
  may call `emit()` freely — no I/O happens at emit time, so core purity holds.
  The daemon writes the collected events as JSONL after the response, keyed by
  config `[debug] log` / `EBROWSE_DEBUG_LOG` (`{session}` substitution for
  per-session files).
- **Request-id join.** The wire protocol already carried a request id; it is
  now overridable via `EBROWSE_REQUEST_ID` at the CLI and stamped as
  `request_id` on every event, matching the harness trace schema's
  `ebrowse_log` record `{request_id, module, event, level, fields, ts, mono}`.
- **Anomalies are events, not a separate channel**: `level=warn` with reserved
  names (`ref_rebound`, `ref_gone`, `snapshot_truncated`, `element_moved`,
  `wait_timeout`, `section_reshaped`). Emission sites are chosen so they are
  rare: e.g. `ref_gone` only fires from the same-page diff (navigation churn is
  normal and never reaches it), `wait_timeout` only when the quiesce cap was
  hit (networkidle misses are routine and logged at info).

## Consequences

- Golden outputs and the frozen model are untouched; the feature is invisible
  unless configured.
- A best-effort sink: a failing write never fails the request, so the log can
  have gaps under disk pressure — acceptable for observability.
- Recorder scope is the daemon request task; background events outside a
  request (adopted tabs, late dialogs) are not captured in tier 1.

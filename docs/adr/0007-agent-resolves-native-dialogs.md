# 0007 — the agent resolves native confirm/prompt dialogs

Status: accepted (2026-07-08). Reverses the auto-accept-everything policy that
shipped in v1.

## Context

`alert`, `confirm`, `prompt`, and `beforeunload` are native JS dialogs that block
the renderer's main thread until answered. v1 wired one `page.on("dialog")` handler
that auto-accepted every type (dismissing `prompt`, since accepting would inject
empty text) and dropped a `note:` in the next diff.

That was safe but lossy: `confirm` ("Delete this item?") and `prompt` ("Enter a
name") are *decisions*, and auto-answering them takes the choice away from the
agent — the very thing an agent driving the browser should get to make. The
vendored agent-browser reference instead leaves these open and exposes a `dialog`
command so the agent decides. We want the same, adapted to Playwright.

The wrinkle: in Playwright a registered dialog handler that doesn't call
`accept()`/`dismiss()` leaves the dialog open, which keeps the renderer main thread
blocked. Everything ebrowse does to read a page routes through `page.evaluate`
(`core/snapshot.py`), so a pending dialog would make `outline`/`expand`/actions hang
until their timeout. Leaving a dialog pending is only viable if page-touching verbs
fail *fast* instead of hanging.

## Decision

- **`alert` / `beforeunload` → auto-accept** (unchanged). No decision to make; a
  `note: native <type> auto-accepted: "…"` still surfaces in the diff.
- **`confirm` / `prompt` → left open and recorded as a pending dialog**, keyed by the
  page it blocked (a dialog on a background tab must not block the active one). The
  opening action returns a `→ dialog opened (blocking)` result instead of a diff.
- **A new `dialog` verb** resolves or inspects it: `dialog accept [text]` (text is a
  prompt's answer), `dialog dismiss`, `dialog status`. Resolving replays the opening
  action's post-action diff, so the agent sees what the decision changed.
- **Page-touching verbs are refused fast while a dialog blocks the current tab**, with
  a recovery-action error (principle 8). Only `dialog`, `tabs`, `tab`, `connect`, and
  `close` run — so `tab <n>` is the escape hatch from a blocked tab. The guard lives
  once in the daemon dispatch, with `observe()` as a backstop.
- **No config flag.** The smart default (auto the no-decision types, hand the rest to
  the agent) is correct without tuning (principle 6); agent-browser's
  `--no-auto-dialog` has no analogue here yet.

## Consequences

- Agents can make destructive-confirm and prompt-input decisions deliberately rather
  than having them silently accepted.
- A pending dialog is sticky: the agent must `dialog accept|dismiss` (or switch tabs)
  before any other page verb works. The error names exactly that, so it's
  self-correcting.
- The blocking-action result (`render_dialog_pending`) is a frozen output shape; it's
  not a `Diff` (the page can't be observed while blocked) and reuses no `DiffKind` —
  note that the existing `dialog` `DiffKind` still means an in-page DOM modal appeared,
  a different thing.
- Playwright detail: the action that opens a modal `confirm`/`prompt` may itself time
  out (the renderer freezes mid-click). `_act` treats a post-action timeout as expected
  when a dialog became pending and reports the dialog instead of a failure.

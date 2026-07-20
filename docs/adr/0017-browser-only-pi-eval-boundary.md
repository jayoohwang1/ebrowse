# 0017 — Browser-only Pi boundary for eval runs

**Status:** Accepted

## Context

Pi's built-in Bash tool gave browser-task agents general host command execution.
Prompt instructions and shell-string filters are not security boundaries, especially
for smaller local models. Page JavaScript (`ebrowse eval`) and file upload also exceed
the standard browser behaviors these experiments are intended to exercise.

## Decision

Pi ebrowse runs load one explicit, sequential custom tool and disable built-in tools
plus automatic extension, skill, template, and context discovery. The tool accepts
one command string and calls a Python policy launcher, which tokenizes it without
expansion, validates an allowlist, and executes a fixed target without a shell. The profile excludes
`eval`, upload, process/daemon control, CDP attachment, session overrides, and
caller-selected output paths.

Navigation defaults to the task URL's hostname plus configured exceptions. The tool
validates explicit URLs; the browser blocks disallowed top-level document requests,
with the existing landed-URL check retained for redirects and same-document edge
cases. Third-party subresources remain unrestricted so ordinary sites still work.

## Consequences

Policy blocks are ordinary, structured tool errors in the trace, so an attempted eval
is experiment signal. The policy, limits, and navigation domains are persisted in run
metadata. This removes general host execution from the model's capability surface,
but it is not OS/container isolation and does not defend against browser-engine flaws
or allowed-site side effects.

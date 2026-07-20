# 0018 — Bounded task-redirect bootstrap

Status: accepted

## Context

Online-Mind2Web supplies a target URL, but common sites redirect that URL to a
regional domain. A task-host allowlist therefore fails on legitimate Canadian
landings, while a permanent unrestricted policy gives the model unnecessary scope.

## Decision

For redirect-aware evals, open the trusted task URL before starting the agent in a
temporary isolated daemon. Allow public HTTP(S) main-frame redirects only in the
initial tab, block popups, bound discovery by time and distinct-host count, record the
observed chain, then stop the daemon. Restart it with the original, observed, and
explicit domains frozen before Pi receives control.

## Consequences

Regional redirects need no machine-specific exceptions and each trace records its
effective scope. Startup performs a second navigation and may fail closed when the
redirect chain exceeds its bounds. Literal private targets are blocked; this is not a
DNS-rebinding defense or a substitute for OS/network isolation.

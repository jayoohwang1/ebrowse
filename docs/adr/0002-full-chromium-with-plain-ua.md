# 0002 — Full chromium build + plain UA for bot-detection fronts

Status: accepted (2026-07-03)

## Context

Playwright's default headless build (`chrome-headless-shell`) is rejected outright by
Akamai-fronted sites (traderjoes.com, drugs.com return "Access Denied") even with a
normal user agent. The default headless UA also advertises `HeadlessChrome`, which
basic bot filters reject.

## Decision

Launch with `channel="chromium"` (the full build, new headless mode), a plain Chrome
UA string, `--disable-blink-features=AutomationControlled`, and normal
locale/timezone context options. This is **not stealth tooling** — it's just not
volunteering to be blocked by UA-string checks. Sites that block full-build headless
too (apartments.com, Cloudflare's "Just a moment…" on cars.com) are the documented use
case for CDP-attach mode (`ebrowse connect 9222` to a real Chrome), not for stealth
patches.

The launch settings live in both the daemon session and the dev harness and must stay
in sync.

## Consequences

- traderjoes.com, drugs.com, bestbuy.com etc. work headless out of the box.
- The UA string hard-codes a Chrome version that will slowly age; bump it occasionally.
- A genuine stealth tier (camoufox/patchright backend) remains future work for the
  strictest sites.

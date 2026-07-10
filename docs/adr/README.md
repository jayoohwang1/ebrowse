# Architecture Decision Records

Short records of non-obvious decisions: the context that forced a choice, the choice,
and its consequences. Write one when a decision would surprise a future reader or was
reversed/refined after real-world testing. Format: Nygard-style
(`Status / Context / Decision / Consequences`), numbered `NNNN-slug.md`.

ADRs 0001–0006 were backfilled from the v1 implementation log (see git history of
`IMPLEMENTATION_LOG.md` for the full play-by-play).

| # | Title |
|---|---|
| [0001](0001-llm-sidecar-never-load-bearing.md) | LLM sidecar is never load-bearing |
| [0002](0002-full-chromium-with-plain-ua.md) | Full chromium build + plain UA for bot-detection fronts |
| [0003](0003-strict-ref-matching.md) | Strict element-descriptor matching (misbinding > churn) |
| [0004](0004-argparse-over-typer.md) | argparse over typer/click |
| [0005](0005-mcp-server-without-sdk.md) | Hand-rolled MCP server, no SDK dependency |
| [0006](0006-image-refs-not-durable.md) | @i image refs are page-scoped, captions expand-time only |
| [0007](0007-agent-resolves-native-dialogs.md) | Agent resolves native confirm/prompt dialogs (`dialog` verb) |
| [0008](0008-explicit-outline-and-synchronous-visual-glance.md) | Explicit outline + synchronous visual glance (`◉`, `describe-screen`) |
| [0009](0009-label-activation-and-trial-click-arbitration.md) | Label activation + Playwright trial-click arbitration in the click pre-check |
| [0010](0010-interaction-plan-and-keyboard-fallback.md) | InteractionPlan routing + keyboard-activation fallback for non-modal covers |

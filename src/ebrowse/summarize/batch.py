"""Batched page summarization: ONE model call per page, strict JSON out.

Input: per-section digests (deterministic label + truncated text + element
names), budgeted to cfg.max_input_tokens. Output: {sid: one-line summary}.
Injection hygiene: model output only ever fills summary *text* — it is
length-clamped, control-stripped, and the renderer adds the ≈ provenance
marker; structure (sids, types, counts) stays deterministic.
"""

from __future__ import annotations

import json
import re

from loguru import logger

from ebrowse.core.pipeline import outline_label
from ebrowse.model import PageMem, Section, estimate_tokens
from ebrowse.summarize.client import SummarizerClient

_SYSTEM = (
    "You label webpage sections for a browsing agent that reads pages as outlines. "
    "For each section, write ONE factual line (max 14 words) saying what the section "
    "contains and what it is for. No advice, no imperatives, no element refs, no "
    "quotes around the whole line. Reply with a JSON array only: "
    '[{"sid": "s1", "summary": "..."}, ...]'
)

_MAX_SUMMARY_CHARS = 140
_PER_SECTION_TEXT_CAP = 1600  # chars of digest text per section before budgeting


def _digest(section: Section, text: str) -> str:
    names = [
        e.desc.name or e.desc.text_head
        for e in section.elements[:12]
        if (e.desc.name or e.desc.text_head)
    ]
    parts = [
        f"{section.sid} type={section.type} {section.counts_desc()}",
        f"label: {outline_label(section)}" if outline_label(section) else "",
        f"text: {text[:_PER_SECTION_TEXT_CAP]}" if text else "",
        f"elements: {', '.join(n[:40] for n in names)}" if names else "",
    ]
    return "\n".join(p for p in parts if p)


def build_messages(page: PageMem, texts: dict[str, str], max_input_tokens: int) -> list[dict]:
    digests = []
    budget = max_input_tokens * 4  # chars
    for s in page.sections:
        if s.cross_origin:
            continue
        d = _digest(s, texts.get(s.sid, ""))
        if len(d) > budget:
            d = d[: max(200, budget)]
        budget -= len(d)
        digests.append(d)
        if budget <= 0:
            logger.warning("summarizer input budget exhausted; later sections get less context")
            break
    user = f"PAGE: {page.title} — {page.url}\n\n" + "\n\n".join(digests)
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def _sanitize(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    text = re.sub(r"\(@e\d+\)", "", text)  # refs are structure, not model output
    return text[:_MAX_SUMMARY_CHARS]


def parse_summaries(raw: str, valid_sids: set[str]) -> dict[str, str]:
    """Parse the model's JSON array of {sid, summary} rows.

    Tolerates code fences, a leading preamble, and — critically — a TRUNCATED
    tail: reasoning-capable models burn a big, near-constant chunk of the token
    budget thinking before the JSON, so the array is often cut off mid-row when
    the budget runs out. We salvage every *complete* object rather than failing
    the whole page (0/N) on one dangling row. Invalid/unknown rows are dropped.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text, flags=re.MULTILINE).strip()
    out: dict[str, str] = {}
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j == -1:
            break
        try:
            row, end = dec.raw_decode(text, j)
        except json.JSONDecodeError:
            i = j + 1  # not a complete object here (e.g. truncated tail); keep scanning
            continue
        i = end
        if not isinstance(row, dict):
            continue
        sid, summary = row.get("sid"), row.get("summary")
        if sid in valid_sids and isinstance(summary, str):
            clean = _sanitize(summary)
            if clean:
                out[sid] = clean
    return out


async def summarize_page(
    client: SummarizerClient,
    page: PageMem,
    texts: dict[str, str],
    max_input_tokens: int,
    timeout_s: float | None = None,
    retry: bool = True,
) -> dict[str, str]:
    """Returns sid -> summary for as many sections as the model labeled.
    Empty dict on any failure (callers fall back to deterministic labels).

    `timeout_s`/`retry` are forwarded to the client: the synchronous outline
    path passes a tight deadline and retry=False so a slow sidecar degrades
    the outline to deterministic labels instead of stalling it."""
    if not client.available:
        return {}
    messages = build_messages(page, texts, max_input_tokens)
    # Budget = reasoning overhead + ~one line of JSON per section. Reasoning
    # models spend a large, near-constant chunk thinking before emitting any
    # JSON (~2k tokens even on a 2-section page); the old flat 60/section budget
    # was consumed entirely by that, cutting the array to nothing (0/N). Keep a
    # generous floor; tolerant parsing + the `|` fallback cover any overrun.
    max_out = min(8000, 2500 + 80 * len(page.sections))
    raw = await client.chat(messages, max_tokens=max_out, retry=retry, timeout_s=timeout_s)
    if raw is None:
        return {}
    valid = {s.sid for s in page.sections}
    parsed = parse_summaries(raw, valid)
    if not parsed:
        # one strict retry: some models chat before the JSON
        raw2 = await client.chat(
            messages + [
                {"role": "assistant", "content": raw[:500]},
                {"role": "user", "content": "Reply with ONLY the JSON array, nothing else."},
            ],
            max_tokens=max_out, retry=retry, timeout_s=timeout_s,
        )  # fmt: skip
        if raw2:
            parsed = parse_summaries(raw2, valid)
    logger.info(
        f"summarized {len(parsed)}/{len(page.sections)} sections "
        f"({estimate_tokens(messages[1]['content'])} tok in)"
    )
    return parsed


_CAPTION_PROMPT = (
    "Describe this image in one short factual phrase (max 12 words). No preamble, no quotes."
)


async def caption_image(client: SummarizerClient, png_b64: str) -> str | None:
    """One VLM caption for one image (base64 png). None on failure/disabled."""
    if not client.available or not client.cfg.vision:
        return None
    raw = await client.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CAPTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                    },
                ],
            }
        ],
        max_tokens=60,
    )
    if not raw:
        return None
    clean = re.sub(r"\s+", " ", raw).strip().strip('"')
    return clean[:100] or None


# The default gist prompt is deliberately anti-speculative: live testing showed
# VLMs drift into "typical" page furniture (cookie banners, popups) that isn't
# actually on screen. The hard "only what is visible" constraint measurably cut
# that drift. Overlay/interstitial flagging is the highest-value signal — it
# catches states the DOM outline can't convey (a login wall, a country picker).
_GLANCE_PROMPT = (
    "You give a browsing agent a quick visual read of the current screen so it can "
    "decide whether to look at the screenshot itself. In 1-2 sentences, describe ONLY "
    "what is actually visible: the main visual content and layout, and any overlay, "
    "modal, popup, cookie banner, or interstitial covering the page. If nothing covers "
    "the main content, say so. Do not guess or mention typical elements that are not "
    "visible. No preamble, no quotes."
)
# Ceiling for the auto ◉ outline line. NOT a target — the concise prompt above
# emits ~1-2 sentences; this is headroom for experimenting with prompt detail.
# Whatever it emits is the one visual cost paid by the MAIN agent (per outline),
# so the shipped prompt stays terse. The manual verb uses its own larger ceiling.
_GLANCE_MAX_TOKENS = 500


async def caption_screen(
    client: SummarizerClient,
    png_b64: str,
    prompt: str | None = None,
    max_tokens: int = _GLANCE_MAX_TOKENS,
    timeout_s: float | None = None,
    retry: bool = True,
) -> str | None:
    """One VLM visual gist of a full screenshot (base64 png). None on
    failure/disabled. `prompt=None` uses the anti-speculative default gist;
    a caller-supplied prompt is a free-form visual query (`describe-screen`)."""
    if not client.available or not client.cfg.vision:
        return None
    raw = await client.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or _GLANCE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                    },
                ],
            }
        ],
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        retry=retry,
    )
    if not raw:
        return None
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", raw)  # strip control chars
    clean = re.sub(r"\s*\n\s*", " ", clean).strip().strip('"').strip()
    return clean or None

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
    """Parse the model's JSON (tolerating code fences); invalid rows dropped."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return {}
    try:
        rows = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
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
) -> dict[str, str]:
    """Returns sid -> summary for as many sections as the model labeled.
    Empty dict on any failure (callers fall back to deterministic labels)."""
    if not client.available:
        return {}
    messages = build_messages(page, texts, max_input_tokens)
    max_out = min(4000, 60 * max(1, len(page.sections)))
    raw = await client.chat(messages, max_tokens=max_out)
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
            max_tokens=max_out,
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

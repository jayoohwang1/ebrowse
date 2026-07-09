"""OpenAI-compatible chat client for the summarizer sidecar.

Never load-bearing: failures degrade outlines to deterministic labels. A
circuit breaker (3 consecutive failures -> disabled for 10 minutes) keeps a
dead server from adding latency to every observation.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger

from ebrowse.config import SummarizerConfig

_BREAKER_FAILURES = 3
_BREAKER_COOLDOWN_S = 600


class SummarizerClient:
    def __init__(self, cfg: SummarizerConfig) -> None:
        self.cfg = cfg
        self._failures = 0
        self._disabled_until = 0.0
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return self.cfg.enabled and time.monotonic() >= self._disabled_until

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.cfg.api_key:
                headers["Authorization"] = f"Bearer {self.cfg.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.cfg.base_url.rstrip("/"),
                headers=headers,
                timeout=self.cfg.timeout_s,
            )
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 2000,
        retry: bool = True,
        timeout_s: float | None = None,
    ) -> str | None:
        """One chat completion; None on failure (after breaker bookkeeping).

        `timeout_s` overrides the client-wide timeout for this request only —
        callers on the synchronous outline hot path pass a tight deadline so a
        slow/hung sidecar can't stall an observation (it degrades to
        deterministic labels). A genuine timeout counts toward the breaker."""
        if not self.available:
            return None
        try:
            body: dict[str, Any] = {
                "model": self.cfg.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
            }
            # Provider-specific knobs (e.g. reasoning-off) live in config, not
            # code; merged last so an operator can override any default above.
            body.update(self.cfg.extra_body)
            timeout = self.cfg.timeout_s if timeout_s is None else timeout_s
            resp = await self._http().post("/chat/completions", json=body, timeout=timeout)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            self._failures = 0
            return content
        except Exception as e:
            logger.warning(f"summarizer call failed: {type(e).__name__}: {str(e)[:150]}")
            if retry:
                return await self.chat(
                    messages, max_tokens=max_tokens, retry=False, timeout_s=timeout_s
                )
            self._failures += 1
            if self._failures >= _BREAKER_FAILURES:
                self._disabled_until = time.monotonic() + _BREAKER_COOLDOWN_S
                logger.warning(
                    f"summarizer disabled for {_BREAKER_COOLDOWN_S}s "
                    f"after {self._failures} consecutive failures"
                )
            return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

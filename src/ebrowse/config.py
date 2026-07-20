"""Configuration loading per docs/configuration.md.

Precedence: built-in defaults < ~/.config/ebrowse/config.toml < EBROWSE_* env vars
< CLI flags (applied by callers). Unknown TOML keys warn, never fail.

Env var mapping is generic: EBROWSE_<SECTION>_<KEY>, e.g.
EBROWSE_SUMMARIZER_BASE_URL, EBROWSE_BROWSER_HEADLESS=false.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DaemonConfig:
    idle_shutdown_minutes: int = 30


@dataclass(slots=True)
class BrowserConfig:
    mode: str = "launch"  # "launch" | "cdp"
    headless: bool = True
    cdp_url: str = ""
    profile_dir: str = ""  # default resolved at use: ~/.cache/ebrowse/profile
    viewport: list[int] = field(default_factory=lambda: [1280, 1280])
    # "cdp": DOMSnapshot.captureSnapshot — no JS runs in the page, refs get
    # node bindings (ADR 0015). "js": the discover.js walk — temporary escape
    # hatch until eval runs establish parity, then removed.
    capture_engine: str = "cdp"  # "cdp" | "js"
    # act on the CDP node binding FIRST for every ref (descriptors as
    # fallback) instead of the default descriptor-first + binding rescue.
    # Exists for eval A/B soak before a possible default flip (ADR 0015).
    act_via_binding: bool = False


@dataclass(slots=True)
class SummarizerConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:5001/v1"
    model: str = "default"
    api_key: str = ""
    vision: bool = True
    # Auto ◉ visual-gist line on the outline (a VLM one-liner of the screenshot,
    # a routing signal between page text and a full screenshot). Effective only
    # when `enabled` + `vision` + the model is reachable; degrades to no line
    # otherwise. Set false to suppress it while keeping `describe-screen`.
    glance: bool = True
    max_input_tokens: int = 100_000
    timeout_s: int = 60
    # Per-*call* deadline for the SYNCHRONOUS outline enrichment (text summaries
    # + auto glance). On timeout the outline renders deterministically (never
    # load-bearing). Note it bounds each sidecar call, not the whole stage: the
    # summaries path may fire one JSON-only reprompt (see summarize/batch.py), so
    # its worst-case wall time is ~2× this. Kept well under the daemon verb
    # ceiling. Keep generous enough for a slow local sidecar.
    sync_timeout_s: int = 30
    # Manual `describe-screen`: patient, agent-initiated visual queries. A high
    # token ceiling lets the agent ask for exhaustive detail; a long timeout
    # covers the resulting slow generation on modest local hardware.
    describe_max_tokens: int = 4096
    describe_timeout_s: int = 180
    # Extra fields merged verbatim into every /chat/completions request body.
    # This is where model/provider-specific knobs live as *config data* rather
    # than provider-branching code — e.g. reasoning-off for a llama.cpp/Qwen
    # sidecar: {"chat_template_kwargs": {"enable_thinking": false}}. Default
    # empty: never assume a backend supports a given field. See
    # docs/configuration.md for per-provider recipes.
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ObserveConfig:
    quiescence_ms: int = 300
    quiescence_max_ms: int = 3000
    preview_chars: int = 120
    list_page_size: int = 20
    max_sections: int = 60
    # Approximate expansion ceiling for ordinary sections. Lists/tables are
    # pageable and therefore use this as a per-page rendering budget instead.
    max_section_tokens: int = 16_384
    # `outline --preview` only: chars of verbatim text appended after each ≈
    # summary. Tune the summary-vs-preview token tradeoff here (shorter = leaner).
    combined_preview_chars: int = 60


@dataclass(slots=True)
class SecurityConfig:
    allowed_domains: list[str] = field(default_factory=list)  # empty = all
    # Eval-harness startup mode: permit public HTTP(S) redirects on the initial
    # tab while recording a bounded set of hosts. The harness restarts the
    # daemon with that set frozen before exposing the browser to the agent.
    bootstrap_navigation: bool = False
    bootstrap_max_hosts: int = 5
    block_private_network: bool = False


@dataclass(slots=True)
class DebugConfig:
    # JSONL debug-event log path (env: EBROWSE_DEBUG_LOG). Empty = off (default;
    # zero overhead, no file created). A literal "{session}" in the path is
    # replaced with the session name for per-session files. See docs/architecture.md
    # ("Debug event channel") and src/ebrowse/debug.py.
    log: str = ""


@dataclass(slots=True)
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    observe: ObserveConfig = field(default_factory=ObserveConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "ebrowse" / "config.toml"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    p = Path(base) / "ebrowse"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.access(base, os.W_OK):
        return Path(base)
    return cache_dir()


def socket_path() -> Path:
    return runtime_dir() / "ebrowse.sock"


def _coerce(raw: str, target_type: type) -> Any:
    if target_type is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(raw)
    if target_type is list:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if target_type is dict:
        value = json.loads(raw)  # e.g. EBROWSE_SUMMARIZER_EXTRA_BODY='{"a":1}'
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    return raw


def _apply_dict(section_obj: Any, data: dict[str, Any], section_name: str) -> None:
    known = {f.name: f for f in fields(section_obj)}
    for key, value in data.items():
        if key not in known:
            print(f"ebrowse: warning: unknown config key [{section_name}].{key}", file=sys.stderr)
            continue
        setattr(section_obj, key, value)


def _apply_env(cfg: Config) -> None:
    for section_field in fields(cfg):
        section_obj = getattr(cfg, section_field.name)
        for f in fields(section_obj):
            env_key = f"EBROWSE_{section_field.name.upper()}_{f.name.upper()}"
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            current = getattr(section_obj, f.name)
            target = type(current) if current is not None else str
            try:
                setattr(section_obj, f.name, _coerce(raw, target))
            except (ValueError, TypeError):
                print(f"ebrowse: warning: bad value for {env_key}: {raw!r}", file=sys.stderr)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or config_path()
    if p.is_file():
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            print(f"ebrowse: warning: could not parse {p}: {e}", file=sys.stderr)
            data = {}
        for section_field in fields(cfg):
            section_data = data.get(section_field.name)
            if isinstance(section_data, dict):
                _apply_dict(getattr(cfg, section_field.name), section_data, section_field.name)
        for key in data:
            if key not in {f.name for f in fields(cfg)}:
                print(f"ebrowse: warning: unknown config section [{key}]", file=sys.stderr)
    _apply_env(cfg)
    return cfg

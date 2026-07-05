"""Configuration loading per docs/configuration.md.

Precedence: built-in defaults < ~/.config/ebrowse/config.toml < EBROWSE_* env vars
< CLI flags (applied by callers). Unknown TOML keys warn, never fail.

Env var mapping is generic: EBROWSE_<SECTION>_<KEY>, e.g.
EBROWSE_SUMMARIZER_BASE_URL, EBROWSE_BROWSER_HEADLESS=false.
"""

from __future__ import annotations

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


@dataclass(slots=True)
class SummarizerConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:5001/v1"
    model: str = "default"
    api_key: str = ""
    vision: bool = True
    max_input_tokens: int = 100_000
    timeout_s: int = 60


@dataclass(slots=True)
class ObserveConfig:
    quiescence_ms: int = 300
    quiescence_max_ms: int = 3000
    preview_chars: int = 120
    list_page_size: int = 20
    resummarize_element_delta: int = 3
    max_sections: int = 60


@dataclass(slots=True)
class SecurityConfig:
    allowed_domains: list[str] = field(default_factory=list)  # empty = all


@dataclass(slots=True)
class Config:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    observe: ObserveConfig = field(default_factory=ObserveConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


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

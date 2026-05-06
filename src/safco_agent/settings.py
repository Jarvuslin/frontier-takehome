"""Centralized settings: YAML config + .env overrides.

Loading precedence: defaults → config/crawler.yaml → environment (SAFCO_* / LLM_*).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "crawler.yaml"
DEFAULT_SELECTORS = REPO_ROOT / "config" / "selectors.yaml"


class SeedConfig(BaseModel):
    id: str
    url: str
    label: str


class RateLimit(BaseModel):
    requests_per_second: float = 1.0
    burst: int = 2


class Timeouts(BaseModel):
    http_seconds: int = 20
    browser_seconds: int = 30
    page_settle_seconds: int = 4


class Limits(BaseModel):
    max_products_per_category: int = 50
    max_concurrent_http: int = 4
    max_concurrent_browser: int = 2


class Retries(BaseModel):
    http_max_attempts: int = 3
    browser_max_attempts: int = 2


class Paths(BaseModel):
    sqlite: str = "data/products.db"
    exports_dir: str = "data/exports"
    reports_dir: str = "data/reports"
    debug_dir: str = "debug"
    logs_dir: str = "logs"


class LLMFallback(BaseModel):
    enabled: bool = False
    trigger_when_missing: list[str] = Field(default_factory=lambda: ["name", "product_url"])
    max_calls_per_run: int = 25
    model: str = "claude-haiku-4-5-20251001"
    api_key: str | None = None


class Site(BaseModel):
    base_url: str = "https://www.safcodental.com"
    user_agent: str = "SafcoCatalogBot/0.1 (+evaluation prototype)"


class Settings(BaseModel):
    site: Site = Field(default_factory=Site)
    seeds: list[SeedConfig] = Field(default_factory=list)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    limits: Limits = Field(default_factory=Limits)
    retries: Retries = Field(default_factory=Retries)
    paths: Paths = Field(default_factory=Paths)
    llm_fallback: LLMFallback = Field(default_factory=LLMFallback)
    log_level: str = "INFO"
    log_format: str = "json"

    @classmethod
    def load(cls, config_path: Path = DEFAULT_CONFIG) -> "Settings":
        raw: dict[str, Any] = {}
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        # Env overrides
        if v := os.getenv("SAFCO_RATE_LIMIT_RPS"):
            raw.setdefault("rate_limit", {})["requests_per_second"] = float(v)
        if v := os.getenv("SAFCO_MAX_PRODUCTS_PER_CATEGORY"):
            raw.setdefault("limits", {})["max_products_per_category"] = int(v)
        if v := os.getenv("SAFCO_USER_AGENT"):
            raw.setdefault("site", {})["user_agent"] = v

        raw.setdefault("llm_fallback", {})
        raw["llm_fallback"]["enabled"] = (
            os.getenv("LLM_FALLBACK_ENABLED", "").lower() in {"1", "true", "yes"}
        )
        raw["llm_fallback"]["api_key"] = os.getenv("ANTHROPIC_API_KEY") or None
        if v := os.getenv("ANTHROPIC_MODEL"):
            raw["llm_fallback"]["model"] = v

        raw["log_level"] = os.getenv("LOG_LEVEL", raw.get("log_level", "INFO"))
        raw["log_format"] = os.getenv("LOG_FORMAT", raw.get("log_format", "json"))

        return cls.model_validate(raw)

    def repo_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else REPO_ROOT / p

    def ensure_dirs(self) -> None:
        for rel in (
            self.paths.exports_dir,
            self.paths.reports_dir,
            self.paths.debug_dir,
            self.paths.logs_dir,
        ):
            self.repo_path(rel).mkdir(parents=True, exist_ok=True)
        self.repo_path(self.paths.sqlite).parent.mkdir(parents=True, exist_ok=True)


def load_selectors(path: Path = DEFAULT_SELECTORS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))

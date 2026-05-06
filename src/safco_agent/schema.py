"""Canonical product schema. The README references this file as the source of truth."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Availability = Literal["in_stock", "out_of_stock", "backorder", "preorder", "unknown"]


class Product(BaseModel):
    """The normalized product record. Persisted in SQLite; flattened into CSV."""

    # Identity
    sku: str | None = None
    product_code: str | None = None
    name: str
    brand: str | None = None
    category_path: list[str] = Field(default_factory=list)
    product_url: str

    # Commercial
    price: Decimal | None = None
    price_text: str | None = None
    currency: str = "USD"
    pack_size: str | None = None
    availability: Availability = "unknown"

    # Descriptive
    description: str | None = None
    specifications: dict[str, str] = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    alternative_product_urls: list[str] = Field(default_factory=list)

    # Provenance
    source_seed: str | None = None
    extraction_method: dict[str, str] = Field(default_factory=dict)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    crawl_run_id: str | None = None

    @field_validator("product_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return v.split("#")[0].rstrip("/")

    @field_validator("image_urls", "alternative_product_urls")
    @classmethod
    def _dedupe_list(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in v:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    @property
    def dedup_key(self) -> str:
        """Stable identity. SKU when present, else hash of canonical URL."""
        if self.sku:
            return f"sku:{self.sku.strip().lower()}"
        return "url:" + hashlib.sha1(self.product_url.encode()).hexdigest()


class CrawlResult(BaseModel):
    """Container around a Product (success) or a failure record."""

    url: str
    success: bool
    product: Product | None = None
    error_class: str | None = None
    error_message: str | None = None
    page_type: str | None = None
    elapsed_ms: int | None = None
    extraction_methods_used: list[str] = Field(default_factory=list)

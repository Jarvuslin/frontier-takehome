"""Canonical product schema. The README references this file as the source of truth."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

Availability = Literal["in_stock", "out_of_stock", "backorder", "preorder", "unknown"]

# The retailer is constant for this scraper. Brand on each Variant is the
# real manufacturer (Halyard, Dash, Aurelia, ...). Distinguishing the two
# is critical: Safco's JSON-LD always reports brand=Safco Dental even when
# the manufacturer is somebody else.
RETAILER = "Safco Dental"


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

    # Variants (one entry per purchasable item-number row from masterData)
    variants: list["Variant"] = Field(default_factory=list)

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


class Variant(BaseModel):
    """One purchasable item-number row from a Safco product page.

    A Safco product page is a Magento configurable product; each variant is
    a child SKU with its own size, pack quantity, price, and availability.
    The data lives in window.masterData on the page.

    `safco_item_number` is the Safco-internal item # (the masterData key and
    `sku` field — they are the same on Safco). `manufacturer_number` is the
    Mfr # (`manufacturer_part_number`). `manufacturer_name` is the real
    brand (Halyard/Dash/Aurelia/...) — NOT the retailer.
    """

    parent_dedup_key: str | None = None
    parent_sku: str | None = None

    safco_item_number: str | None = None
    manufacturer_number: str | None = None
    manufacturer_name: str | None = None  # = brand

    name: str | None = None
    description: str | None = None

    price: Decimal | None = None
    price_text: str | None = None
    currency: str | None = None  # never default; only set when explicitly detected
    availability: Availability = "unknown"
    availability_label: str | None = None

    size: str | None = None
    pack_quantity: int | None = None
    pack_unit: str | None = None

    image: str | None = None
    main_image: str | None = None

    is_synthetic: bool = False
    extraction_method: dict[str, str] = Field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Stable identity within a parent product.

        Falls back through safco_item_number → manufacturer_number → name so
        we always have *some* key. Keys are namespaced `variant:` so they
        cannot collide with Product keys (`sku:` / `url:`).
        """
        identifier = (
            (self.safco_item_number or self.manufacturer_number or self.name or "_unknown")
            .strip()
            .lower()
        )
        suffix = ":synthetic" if self.is_synthetic else ""
        return f"variant:{self.parent_dedup_key or '_orphan'}:{identifier}{suffix}"


# Resolve the forward reference Product.variants: list["Variant"]
Product.model_rebuild()


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

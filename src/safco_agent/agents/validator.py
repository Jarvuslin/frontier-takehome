"""ValidatorAgent — Pydantic check + in-run dedup tracker.

Tracks both Product-level dedup (parent pages) and Variant-level dedup
(per item-number rows). Variant keys are namespaced `variant:` so they
cannot collide with Product keys (`sku:` / `url:`); a single shared
`seen_keys` set safely tracks both.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from safco_agent.observability.logging import get_logger
from safco_agent.schema import Product, Variant

log = get_logger("agent.validator")


@dataclass
class Validator:
    seen_keys: set[str] = field(default_factory=set)
    duplicates: int = 0
    rejected: int = 0
    accepted: int = 0

    # Variant-level counters, kept separate so the run report can show both
    # parent-page totals and variant-row totals without conflation.
    variants_accepted: int = 0
    variants_rejected: int = 0
    variants_duplicates: int = 0

    def validate(self, product: Product) -> tuple[bool, str | None]:
        """Return (accepted, reason_if_rejected)."""
        if not product.name or not product.product_url:
            self.rejected += 1
            return False, "missing_name_or_url"
        if product.dedup_key in self.seen_keys:
            self.duplicates += 1
            return False, "duplicate"
        self.seen_keys.add(product.dedup_key)
        self.accepted += 1
        return True, None

    def validate_variant(self, v: Variant) -> tuple[bool, str | None]:
        """Validate a Variant row; mirror of `validate` but variant-keyed."""
        if not v.safco_item_number and not v.name:
            self.variants_rejected += 1
            return False, "missing_item_number_and_name"
        if v.dedup_key in self.seen_keys:
            self.variants_duplicates += 1
            return False, "duplicate"
        self.seen_keys.add(v.dedup_key)
        self.variants_accepted += 1
        return True, None

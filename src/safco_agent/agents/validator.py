"""ValidatorAgent — Pydantic check + in-run dedup tracker."""
from __future__ import annotations

from dataclasses import dataclass, field

from safco_agent.observability.logging import get_logger
from safco_agent.schema import Product

log = get_logger("agent.validator")


@dataclass
class Validator:
    seen_keys: set[str] = field(default_factory=set)
    duplicates: int = 0
    rejected: int = 0
    accepted: int = 0

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

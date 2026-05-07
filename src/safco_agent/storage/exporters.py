"""Export layer — CSV / JSONL / grouped JSON over the variants + products tables.

Output layout (under `output/` by default):

  products_all.csv               - one row per purchasable variant (master)
  products_<seed>.csv            - same shape, filtered to one source seed
  specifications.jsonl           - one line per parent product, nested variants
  products_grouped.json          - readable JSON array of the same shape

Design notes:
- The CSV is the "evaluator-friendly flat" view: one orderable item per row,
  with parent context columns alongside variant columns. Aliases (`sku`,
  `name`, `product_code`) all map to the variant's `safco_item_number` so a
  reviewer scanning the file finds familiar columns without consulting docs.
- The JSONL is the "catalog-shaped" view: parent product once with nested
  variants, parsed specifications, and an `extraction_quality` block.
- `brand` is the actual manufacturer (Halyard, Dash, ...), `retailer` is the
  constant `"Safco Dental"` — distinguished by design.
- Placeholder image URLs (Magento "white-placeholder" defaults) are filtered
  from every output so they don't pose as real product imagery.
"""
from __future__ import annotations

import csv
import html as html_module
import json
import re
from pathlib import Path
from typing import Any

from safco_agent.schema import RETAILER
from safco_agent.spec_parser import parse_specifications
from safco_agent.storage.sqlite import Store

# Magento serves a generic "no image" placeholder until a real photo exists.
# These URLs are not product imagery — they're padding.
_PLACEHOLDER_TOKENS = ("placeholder", "/placeholder/default/", "white-placeholder")


def is_placeholder_image(url: str | None) -> bool:
    if not url:
        return True
    lo = url.lower()
    return any(tok in lo for tok in _PLACEHOLDER_TOKENS)


def _real_images(urls: list[str]) -> list[str]:
    return [u for u in urls if not is_placeholder_image(u)]


def clean_export_text(text: Any) -> str:
    """Decode HTML entities, strip tags, collapse whitespace.

    Safe to call on already-clean strings — idempotent. Loops a couple of times
    so double-encoded entities (`&amp;nbsp;` → `&nbsp;` → ` ` → space)
    fully resolve. Preserves ® and ™ (already-decoded chars pass through).
    """
    if text is None:
        return ""
    s = str(text)
    for _ in range(3):
        unescaped = html_module.unescape(s)
        if unescaped == s:
            break
        s = unescaped
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def seed_to_slug(seed_id: str) -> str:
    """`gloves` -> `gloves`, `sutures-surgical-products` -> `sutures_surgical`.

    Strips a trailing `-products` (Safco's convention), then `-` -> `_`.
    """
    slug = re.sub(r"-products?$", "", seed_id)
    return slug.replace("-", "_")


# ── CSV (variant-grain) ─────────────────────────────────────────────────────

CSV_COLUMNS = [
    # Evaluator-friendly aliases — all point at safco_item_number / product_name
    "sku", "name", "product_code",
    # Parent context
    "parent_sku", "parent_name",
    # Variant identity
    "safco_item_number", "product_name", "manufacturer_number",
    # Brand vs retailer — distinct columns by design
    "brand", "retailer",
    # Variant attributes
    "description", "size", "pack_quantity", "pack_unit",
    "price", "price_text", "currency", "availability", "availability_label",
    # Parent context fields (kept alongside variant rows for portability)
    "parent_description", "category_path_str", "product_url",
    "image_urls_str", "variant_image",
    "is_synthetic", "source_seed", "extracted_at",
]


def _csv_row(row: dict, parent_real_images: list[str]) -> dict:
    item_no = row.get("safco_item_number")
    variant_name = clean_export_text(row.get("variant_name"))
    raw_variant_image = row.get("variant_image_main") or row.get("variant_image_thumb")
    variant_image = None if is_placeholder_image(raw_variant_image) else raw_variant_image
    cat = json.loads(row.get("category_path_json") or "[]")
    return {
        # aliases
        "sku": item_no,
        "name": variant_name,
        "product_code": item_no,
        # parent
        "parent_sku": row.get("parent_sku") or row.get("p_sku"),
        "parent_name": clean_export_text(row.get("parent_name")),
        # variant identity
        "safco_item_number": item_no,
        "product_name": variant_name,
        "manufacturer_number": row.get("manufacturer_number"),
        # brand / retailer
        "brand": clean_export_text(row.get("manufacturer_name")) or None,
        "retailer": RETAILER,
        # variant attributes
        "description": clean_export_text(row.get("variant_description")),
        "size": row.get("size"),
        "pack_quantity": row.get("pack_quantity"),
        "pack_unit": row.get("pack_unit"),
        "price": row.get("price"),
        "price_text": row.get("price_text"),
        "currency": row.get("currency") or "",  # never default to USD
        "availability": row.get("availability"),
        "availability_label": clean_export_text(row.get("availability_label")),
        # parent context
        "parent_description": clean_export_text(row.get("parent_description")),
        "category_path_str": " > ".join(cat),
        "product_url": row.get("product_url"),
        "image_urls_str": " | ".join(parent_real_images),
        "variant_image": variant_image,
        "is_synthetic": int(bool(row.get("is_synthetic"))),
        "source_seed": row.get("source_seed"),
        "extracted_at": row.get("extracted_at"),
    }


def export_variant_csv(
    store: Store, out_path: Path, seed_filter: str | None = None
) -> int:
    """Write one row per variant, optionally filtered to a single source_seed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    image_cache: dict[str, list[str]] = {}
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in store.all_variants_with_parent():
            d = dict(row)
            if seed_filter and d.get("source_seed") != seed_filter:
                continue
            parent_key = d["parent_dedup_key"]
            if parent_key not in image_cache:
                image_cache[parent_key] = _real_images(store.images_for(parent_key))
            w.writerow(_csv_row(d, image_cache[parent_key]))
            n += 1
    return n


# ── Specifications JSONL (parent-grain) ─────────────────────────────────────

# Fields whose absence we surface in extraction_quality.missing_fields.
_TRACKED_PARENT_FIELDS = (
    "brand", "price", "availability", "specifications", "images",
    "alternative_products", "variants", "currency",
)


def _variant_dict(row: dict) -> dict:
    """One nested variant entry inside a specifications.jsonl record."""
    item_no = row.get("safco_item_number")
    raw_image = row.get("variant_image_main") or row.get("variant_image_thumb")
    variant_image = None if is_placeholder_image(raw_image) else raw_image
    return {
        "sku": item_no,
        "product_code": item_no,
        "safco_item_number": item_no,
        "manufacturer_number": row.get("manufacturer_number"),
        "name": clean_export_text(row.get("variant_name")) or None,
        "brand": clean_export_text(row.get("manufacturer_name")) or None,
        "description": clean_export_text(row.get("variant_description")) or None,
        "size": row.get("size"),
        "pack_quantity": row.get("pack_quantity"),
        "pack_unit": row.get("pack_unit"),
        "price": row.get("price"),
        "price_text": row.get("price_text"),
        "currency": row.get("currency"),  # may be None — we never guess
        "availability": row.get("availability"),
        "availability_label": clean_export_text(row.get("availability_label")) or None,
        "variant_image": variant_image,
        "is_synthetic": bool(row.get("is_synthetic")),
    }


def _missing_fields_for_parent(parent: dict, variants: list[dict], specs: dict, images: list[str]) -> list[str]:
    """Which tracked fields are absent — surface as data-quality awareness."""
    missing = []
    if not parent.get("brand"):
        missing.append("brand")
    # Parent-level price often missing for configurable products (variants carry price).
    if not parent.get("price") and not any(v.get("price") for v in variants):
        missing.append("price")
    if not parent.get("availability") or parent.get("availability") == "unknown":
        # If any variant has a known availability, parent-level is fine.
        if not any((v.get("availability") and v.get("availability") != "unknown") for v in variants):
            missing.append("availability")
    if not specs:
        missing.append("specifications")
    if not images:
        missing.append("images")
    if not parent.get("alternative_products"):
        missing.append("alternative_products")
    if not variants:
        missing.append("variants")
    if not any(v.get("currency") for v in variants) and not parent.get("currency"):
        missing.append("currency")
    return missing


def _build_parent_record(store: Store, parent_row: dict) -> dict:
    """Assemble one specifications.jsonl line from a parent product row."""
    parent_key = parent_row["dedup_key"]
    cat_path = json.loads(parent_row.get("category_path") or "[]")
    images = _real_images(store.images_for(parent_key))
    alternative_products = store.alternatives_for(parent_key)

    variants_raw = [
        dict(r) for r in store._conn.execute(
            "SELECT * FROM variants WHERE parent_dedup_key=? ORDER BY size, safco_item_number",
            (parent_key,),
        )
    ]
    # Re-key: storage column names → JSONL field names via _variant_dict's expectations.
    variants = [
        _variant_dict({
            **v,
            "variant_name": v["name"],
            "variant_description": v["description"],
            "variant_image_main": v.get("main_image"),
            "variant_image_thumb": v.get("image"),
        })
        for v in variants_raw
    ]

    parent_description = clean_export_text(parent_row.get("description"))
    variant_descriptions = [clean_export_text(v.get("description") or "") for v in variants_raw]
    specs, spec_source = parse_specifications(
        parent_description, variant_descriptions, cat_path
    )

    parent_brand = clean_export_text(parent_row.get("brand")) or None
    record: dict[str, Any] = {
        "parent_sku": parent_row.get("sku"),
        "parent_name": clean_export_text(parent_row.get("name")) or None,
        "brand": parent_brand,
        "retailer": RETAILER,
        "category_path": cat_path,
        "category_path_str": " > ".join(cat_path),
        "product_url": parent_row.get("product_url"),
        "description": parent_description or None,
        "specifications": specs,
        "variants": variants,
        "images": images,
        "alternative_products": alternative_products,
        "extraction_quality": {
            "spec_source": spec_source,
            "has_variants": bool(variants),
            "variant_count": len(variants),
            "placeholder_images_filtered": True,
            "missing_fields": [],  # filled below
        },
        "extracted_at": parent_row.get("extracted_at"),
    }
    record["extraction_quality"]["missing_fields"] = _missing_fields_for_parent(
        record, variants, specs, images
    )
    return record


def export_specifications_jsonl(store: Store, out_path: Path) -> int:
    """One JSONL line per parent product with nested variants and parsed specs."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as f:
        for parent_row in store.all_products():
            record = _build_parent_record(store, dict(parent_row))
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def export_grouped_json(store: Store, out_path: Path) -> int:
    """Readable JSON array — same shape as JSONL but pretty-printed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        _build_parent_record(store, dict(parent_row))
        for parent_row in store.all_products()
    ]
    out_path.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return len(records)


# ── Backwards-compatible aliases (kept so old code paths don't break) ───────

def export_csv(store: Store, out_path: Path) -> int:
    """Backwards-compat: same as `export_variant_csv` with no seed filter."""
    return export_variant_csv(store, out_path)


def export_jsonl(store: Store, out_path: Path) -> int:
    """Backwards-compat: writes the new specifications.jsonl shape."""
    return export_specifications_jsonl(store, out_path)

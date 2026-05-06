"""CSV / JSONL exporters. Run after crawl completion."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from safco_agent.storage.sqlite import Store


def export_jsonl(store: Store, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as f:
        for row in store.all_products():
            d = dict(row)
            d["category_path"] = json.loads(d.get("category_path") or "[]")
            d["extraction_method"] = json.loads(d.get("extraction_method") or "{}")
            d["specifications"] = {s["name"]: s["value"] for s in store.specs_for(d["dedup_key"])}
            d["image_urls"] = store.images_for(d["dedup_key"])
            d["alternative_product_urls"] = store.alternatives_for(d["dedup_key"])
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            n += 1
    return n


CSV_COLUMNS = [
    "dedup_key", "sku", "product_code", "name", "brand", "category_path_str",
    "product_url", "price", "price_text", "currency", "pack_size", "availability",
    "description", "specifications_json", "image_urls_str",
    "alternative_product_urls_str", "source_seed", "extracted_at",
]


def export_csv(store: Store, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in store.all_products():
            d = dict(row)
            cat = json.loads(d.get("category_path") or "[]")
            specs = {s["name"]: s["value"] for s in store.specs_for(d["dedup_key"])}
            imgs = store.images_for(d["dedup_key"])
            alts = store.alternatives_for(d["dedup_key"])
            w.writerow(
                {
                    "dedup_key": d["dedup_key"],
                    "sku": d.get("sku"),
                    "product_code": d.get("product_code"),
                    "name": d.get("name"),
                    "brand": d.get("brand"),
                    "category_path_str": " > ".join(cat),
                    "product_url": d.get("product_url"),
                    "price": d.get("price"),
                    "price_text": d.get("price_text"),
                    "currency": d.get("currency"),
                    "pack_size": d.get("pack_size"),
                    "availability": d.get("availability"),
                    "description": (d.get("description") or "").replace("\n", " ").strip(),
                    "specifications_json": json.dumps(specs, ensure_ascii=False),
                    "image_urls_str": " | ".join(imgs),
                    "alternative_product_urls_str": " | ".join(alts),
                    "source_seed": d.get("source_seed"),
                    "extracted_at": d.get("extracted_at"),
                }
            )
            n += 1
    return n


def export_specs_csv(store: Store, out_path: Path) -> int:
    """Long-form spec table: one row per (product, spec)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dedup_key", "sku", "name", "spec_name", "spec_value"])
        for prod in store.all_products():
            for s in store.specs_for(prod["dedup_key"]):
                w.writerow([prod["dedup_key"], prod["sku"], prod["name"], s["name"], s["value"]])
                n += 1
    return n

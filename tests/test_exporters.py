"""Exporter tests — variant-grain CSV layout, per-seed splitting,
specifications.jsonl shape, placeholder image filtering, and spec parsing.
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from safco_agent.schema import Product, Variant
from safco_agent.storage.exporters import (
    CSV_COLUMNS,
    clean_export_text,
    export_grouped_json,
    export_specifications_jsonl,
    export_variant_csv,
    is_placeholder_image,
    seed_to_slug,
)
from safco_agent.storage.sqlite import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _glove_parent() -> Product:
    return Product(
        sku="DRCDK",
        name="Alasta Pro",
        brand="Safco Dental",  # JSON-LD lies; the manufacturer is on the variant
        category_path=["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"],
        product_url="https://www.safcodental.com/product/alasta-pro",
        description=(
            "Powder-free nitrile exam gloves. Beaded cuff. Chlorinated. "
            "Ambidextrous. Blue color. Thickness: at palm 3.1 mils; at "
            "fingertip 3.9 mils. 200 gloves per box. "
            "Order 10 boxes to purchase a case."
        ),
        image_urls=[
            "https://www.safcodental.com/media/catalog/product/d/r/drcdk.jpg",
            "https://www.safcodental.com/media/catalog/product/placeholder/default/white-placeholder.jpg",
        ],
        source_seed="gloves",
    )


def _glove_variants(parent: Product) -> list[Variant]:
    return [
        Variant(
            parent_dedup_key=parent.dedup_key, parent_sku="DRCDK",
            safco_item_number=item, manufacturer_number=mfr,
            manufacturer_name="Dash",
            name=f"Dash Alasta® PRO gloves, {size.lower()}, 200/box",
            description=f"{size}, 200/box",
            price=Decimal("23.49"), price_text="23.490000",
            availability=avail, availability_label=label,
            size=size, pack_quantity=200, pack_unit="box",
            main_image=img,
        )
        for item, mfr, size, avail, label, img in [
            ("4681214", "ALGA200XS", "X-small", "backorder", "Backorder",
             "https://x.test/media/catalog/product/placeholder/default/white-placeholder.jpg"),
            ("4681216", "ALGA200S",  "Small",   "in_stock",  "In stock",
             "https://x.test/media/catalog/product/d/r/dash-s.jpg"),
            ("4681218", "ALGA200M",  "Medium",  "in_stock",  "In stock",
             "https://x.test/media/catalog/product/d/r/dash-m.jpg"),
            ("4681220", "ALGA200L",  "Large",   "in_stock",  "In stock",
             "https://x.test/media/catalog/product/d/r/dash-l.jpg"),
        ]
    ]


def _suture_parent() -> Product:
    return Product(
        sku="SUTX",
        name="Test Sutures",
        brand="Safco Dental",
        category_path=["Dental Supplies", "Sutures & surgical products"],
        product_url="https://www.safcodental.com/product/test-sutures",
        description="Sterile absorbable 4-0 silk sutures. Dimensions 15 x 20mm.",
        image_urls=["https://x.test/media/catalog/product/s/u/sutures.jpg"],
        source_seed="sutures-surgical-products",
    )


def _suture_variant(parent: Product) -> Variant:
    return Variant(
        parent_dedup_key=parent.dedup_key, parent_sku="SUTX",
        safco_item_number="9001", manufacturer_number="SUT4-0",
        manufacturer_name="Acme",
        name="Acme silk sutures 4-0",
        description="4-0, 12/box",
        price=Decimal("18.99"), availability="in_stock",
        size="4-0", pack_quantity=12, pack_unit="box",
    )


def _seed_db(store: Store) -> tuple[Product, Product]:
    gp, sp = _glove_parent(), _suture_parent()
    store.upsert_product(gp)
    store.upsert_product(sp)
    store.upsert_variants(gp.dedup_key, _glove_variants(gp))
    store.upsert_variants(sp.dedup_key, [_suture_variant(sp)])
    return gp, sp


# ── Helpers ──────────────────────────────────────────────────────────────────


def test_seed_to_slug_known_cases() -> None:
    assert seed_to_slug("gloves") == "gloves"
    assert seed_to_slug("sutures-surgical-products") == "sutures_surgical"
    assert seed_to_slug("anesthetics") == "anesthetics"
    assert seed_to_slug("hand-instruments") == "hand_instruments"


def test_is_placeholder_image_detects_magento_defaults() -> None:
    assert is_placeholder_image("https://x.test/media/.../placeholder/default/white-placeholder.jpg")
    assert is_placeholder_image("https://x.test/.../white-placeholder_4.jpg")
    assert is_placeholder_image(None)
    assert not is_placeholder_image("https://x.test/media/catalog/product/d/r/real.jpg")


def test_clean_export_text_decodes_double_encoded() -> None:
    # Double-encoded entity → real space after the loop runs twice.
    assert clean_export_text("Foo&amp;nbsp;bar") == "Foo bar"
    # Already-decoded ® passes through unchanged.
    assert clean_export_text("Barricaid® dressing") == "Barricaid® dressing"
    # Tags stripped, whitespace collapsed.
    assert clean_export_text("<p>line 1</p>\n<p>line 2</p>") == "line 1 line 2"
    assert clean_export_text(None) == ""


# ── CSV ─────────────────────────────────────────────────────────────────────


def test_products_all_csv_has_one_row_per_variant(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "products_all.csv"
    n = export_variant_csv(store, out)
    assert n == 5  # 4 glove variants + 1 suture variant


def test_per_seed_split_row_counts_sum_to_master(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    n_all = export_variant_csv(store, tmp_path / "all.csv")
    n_g = export_variant_csv(store, tmp_path / "g.csv", seed_filter="gloves")
    n_s = export_variant_csv(store, tmp_path / "s.csv", seed_filter="sutures-surgical-products")
    assert n_g + n_s == n_all
    assert n_g == 4 and n_s == 1


def test_per_seed_csv_only_contains_its_seed(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "g.csv"
    export_variant_csv(store, out, seed_filter="gloves")
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert all(r["source_seed"] == "gloves" for r in rows)
    assert all("alasta-pro" in r["product_url"] for r in rows)


def test_csv_aliases_mirror_safco_item_number(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    for row in csv.DictReader(out.open(encoding="utf-8")):
        assert row["sku"] == row["safco_item_number"]
        assert row["product_code"] == row["safco_item_number"]
        assert row["name"] == row["product_name"]


def test_csv_brand_is_manufacturer_not_retailer(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    brands = {r["brand"] for r in rows}
    retailers = {r["retailer"] for r in rows}
    assert "Safco Dental" not in brands  # never as brand
    assert retailers == {"Safco Dental"}


def test_csv_currency_blank_when_undetected(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    for row in csv.DictReader(out.open(encoding="utf-8")):
        assert row["currency"] == ""  # never default to USD for variants


def test_csv_column_order_stable(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == CSV_COLUMNS


def test_csv_filters_placeholder_variant_image(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    text = out.read_text(encoding="utf-8")
    assert "white-placeholder" not in text
    assert "/placeholder/default/" not in text


def test_csv_parent_image_urls_drop_placeholders(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "all.csv"
    export_variant_csv(store, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    glove_rows = [r for r in rows if r["source_seed"] == "gloves"]
    # Real image stays; placeholder is filtered.
    for r in glove_rows:
        assert "drcdk.jpg" in r["image_urls_str"]
        assert "placeholder" not in r["image_urls_str"].lower()


# ── specifications.jsonl ────────────────────────────────────────────────────


def test_jsonl_one_line_per_parent(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    n = export_specifications_jsonl(store, out)
    assert n == 2  # two parent products
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # Each line is valid JSON.
    for line in lines:
        json.loads(line)


def test_jsonl_record_has_required_keys(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    for key in (
        "parent_sku", "parent_name", "brand", "retailer",
        "category_path", "category_path_str", "product_url",
        "description", "specifications", "variants",
        "images", "alternative_products", "extraction_quality", "extracted_at",
    ):
        assert key in record, f"missing key: {key}"


def test_jsonl_glove_record_nests_four_variants(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    glove = next(r for r in records if r["parent_sku"] == "DRCDK")
    assert len(glove["variants"]) == 4
    assert glove["extraction_quality"]["variant_count"] == 4
    assert glove["extraction_quality"]["has_variants"] is True


def test_jsonl_glove_specs_parsed_from_description(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    glove = next(r for r in records if r["parent_sku"] == "DRCDK")
    specs = glove["specifications"]
    assert specs["material"] == "nitrile"
    assert specs["powder_free"] is True
    assert specs["ambidextrous"] is True
    assert specs["color"] == "blue"
    assert specs["cuff"] == "beaded cuff"
    assert specs["thickness_palm_mils"] == 3.1
    assert specs["thickness_fingertip_mils"] == 3.9
    assert specs["case_quantity_boxes"] == 10


def test_jsonl_surgical_specs_parsed(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    suture = next(r for r in records if r["parent_sku"] == "SUTX")
    specs = suture["specifications"]
    assert specs["material"] == "silk"
    assert specs["sterile"] is True
    assert specs["absorbable"] is True
    assert specs["suture_size"] == "4-0"
    assert specs["dimensions"] == "15 x 20mm"


def test_jsonl_missing_fields_includes_alternative_products(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    for record in records:
        # We don't extract alternatives for either fixture, so they should appear here.
        assert "alternative_products" in record["extraction_quality"]["missing_fields"]
        # currency too — variants don't carry one for this site
        assert "currency" in record["extraction_quality"]["missing_fields"]


def test_jsonl_variant_image_placeholder_nulled(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    glove = next(r for r in records if r["parent_sku"] == "DRCDK")
    xs = next(v for v in glove["variants"] if v["safco_item_number"] == "4681214")
    assert xs["variant_image"] is None  # placeholder was nulled
    s = next(v for v in glove["variants"] if v["safco_item_number"] == "4681216")
    assert s["variant_image"] and "placeholder" not in s["variant_image"].lower()


def test_jsonl_no_html_entity_leakage(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    text = out.read_text(encoding="utf-8")
    for entity in ("&reg;", "&trade;", "&nbsp;", "&amp;", "&lt;", "&gt;"):
        assert entity not in text


def test_jsonl_images_array_drops_placeholders(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "specs.jsonl"
    export_specifications_jsonl(store, out)
    records = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    for r in records:
        for url in r["images"]:
            assert "placeholder" not in url.lower()


# ── Optional grouped JSON ───────────────────────────────────────────────────


def test_grouped_json_is_array_of_parents(store: Store, tmp_path: Path) -> None:
    _seed_db(store)
    out = tmp_path / "grouped.json"
    n = export_grouped_json(store, out)
    assert n == 2
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 2
    # Same shape as JSONL records.
    assert {"parent_sku", "variants", "specifications"} <= set(data[0].keys())

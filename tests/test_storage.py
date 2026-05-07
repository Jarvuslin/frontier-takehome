from decimal import Decimal
from pathlib import Path

import pytest

from safco_agent.schema import Product, Variant
from safco_agent.storage.exporters import export_csv, export_jsonl
from safco_agent.storage.sqlite import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _make(sku: str = "ABC1") -> Product:
    return Product(
        sku=sku,
        name=f"Test {sku}",
        brand="TestBrand",
        category_path=["Dental Supplies", "Gloves"],
        product_url=f"https://x.test/product/{sku.lower()}",
        price=Decimal("9.99"),
        price_text="$9.99",
        availability="in_stock",
        description="hello",
        specifications={"size": "M", "material": "nitrile"},
        image_urls=["https://x.test/img/1.jpg"],
        alternative_product_urls=["https://x.test/product/other"],
    )


def _make_variant(parent: Product, item_no: str, mfr: str = "Acme") -> Variant:
    return Variant(
        parent_dedup_key=parent.dedup_key,
        parent_sku=parent.sku,
        safco_item_number=item_no,
        manufacturer_number=f"MFR-{item_no}",
        manufacturer_name=mfr,
        name=f"{parent.name} variant {item_no}",
        description="Medium, 100/box",
        price=Decimal("9.99"),
        price_text="9.990000",
        availability="in_stock",
        size="Medium",
        pack_quantity=100,
        pack_unit="box",
    )


def test_upsert_then_idempotent(store: Store) -> None:
    p = _make("S1")
    store.upsert_product(p)
    store.upsert_product(p)  # second time should not duplicate
    assert store.product_count() == 1
    assert {s["name"]: s["value"] for s in store.specs_for(p.dedup_key)} == {
        "size": "M", "material": "nitrile"
    }
    assert store.images_for(p.dedup_key) == ["https://x.test/img/1.jpg"]


def test_variant_upsert_replaces_not_duplicates(store: Store) -> None:
    p = _make("S1")
    store.upsert_product(p)
    v1 = [_make_variant(p, "1001"), _make_variant(p, "1002")]
    v2 = [_make_variant(p, "1001"), _make_variant(p, "1002"), _make_variant(p, "1003")]
    store.upsert_variants(p.dedup_key, v1)
    assert store.variant_count() == 2
    store.upsert_variants(p.dedup_key, v2)
    assert store.variant_count() == 3  # third variant added, others replaced in place
    store.upsert_variants(p.dedup_key, v1)
    assert store.variant_count() == 2  # third variant cleanly removed


def test_export_roundtrip(store: Store, tmp_path: Path) -> None:
    p1, p2 = _make("S1"), _make("S2")
    store.upsert_product(p1)
    store.upsert_product(p2)
    store.upsert_variants(p1.dedup_key, [_make_variant(p1, "1001"), _make_variant(p1, "1002")])
    store.upsert_variants(p2.dedup_key, [_make_variant(p2, "2001")])
    n_csv = export_csv(store, tmp_path / "p.csv")
    n_jsonl = export_jsonl(store, tmp_path / "p.jsonl")
    # CSV is now variant-grain (3 variants), JSONL is still parent-grain (2 products)
    assert n_csv == 3
    assert n_jsonl == 2
    csv_text = (tmp_path / "p.csv").read_text(encoding="utf-8")
    assert "1001" in csv_text and "2001" in csv_text
    assert "Safco Dental" in csv_text  # retailer column


def test_variant_fk_cascade_removes_on_parent_delete(store: Store) -> None:
    p = _make("S1")
    store.upsert_product(p)
    store.upsert_variants(p.dedup_key, [_make_variant(p, "1001")])
    assert store.variant_count() == 1
    store._conn.execute("DELETE FROM products WHERE dedup_key=?", (p.dedup_key,))
    store._conn.commit()
    assert store.variant_count() == 0  # FK cascade removed the orphan variant


def test_crawl_state_lifecycle(store: Store) -> None:
    store.mark_pending("https://x.test/product/a", "gloves")
    store.mark_done("https://x.test/product/a")
    rows = store.pending_urls("gloves")
    assert all(r["url"] != "https://x.test/product/a" for r in rows)


def test_crawl_state_failure_then_retry_visible(store: Store) -> None:
    store.mark_pending("https://x.test/product/b", "gloves")
    store.mark_failed("https://x.test/product/b", "Timeout", "boom")
    rows = store.pending_urls("gloves")
    assert any(r["url"] == "https://x.test/product/b" for r in rows)

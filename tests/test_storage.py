from decimal import Decimal
from pathlib import Path

import pytest

from safco_agent.schema import Product
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


def test_upsert_then_idempotent(store: Store) -> None:
    p = _make("S1")
    store.upsert_product(p)
    store.upsert_product(p)  # second time should not duplicate
    assert store.product_count() == 1
    assert {s["name"]: s["value"] for s in store.specs_for(p.dedup_key)} == {
        "size": "M", "material": "nitrile"
    }
    assert store.images_for(p.dedup_key) == ["https://x.test/img/1.jpg"]


def test_export_roundtrip(store: Store, tmp_path: Path) -> None:
    store.upsert_product(_make("S1"))
    store.upsert_product(_make("S2"))
    n_csv = export_csv(store, tmp_path / "p.csv")
    n_jsonl = export_jsonl(store, tmp_path / "p.jsonl")
    assert n_csv == 2
    assert n_jsonl == 2
    csv_text = (tmp_path / "p.csv").read_text(encoding="utf-8")
    assert "S1" in csv_text and "S2" in csv_text


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

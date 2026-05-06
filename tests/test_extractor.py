"""Offline extractor tests against committed HTML fixtures.

Fixtures were captured with `httpx` against live product pages on 2026-05-06.
Re-capture by running the small script in tests/fixtures/REGEN.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from safco_agent.agents.extractor import Extractor
from safco_agent.settings import load_selectors

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def extractor() -> Extractor:
    return Extractor(load_selectors(), "https://www.safcodental.com")


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_aquasoft_full_extraction(extractor: Extractor) -> None:
    html = _read("product_aquasoft.html")
    p, methods = extractor.extract("https://www.safcodental.com/product/aquasoft", html)
    assert p is not None
    assert p.name == "Aquasoft"
    assert p.sku == "DRCBT"
    assert p.price is not None and float(p.price) > 0
    assert p.currency == "USD"
    assert p.availability == "in_stock"
    assert p.image_urls, "expected at least one image url"
    assert p.description and "nitrile" in p.description.lower()
    # extraction-method telemetry recorded
    assert methods["name"] == "json-ld"
    assert methods["sku"] == "json-ld"


def test_lavender_nitrile_breadcrumbs(extractor: Extractor) -> None:
    html = _read("product_lavender-nitrile.html")
    p, _ = extractor.extract(
        "https://www.safcodental.com/product/lavender-nitrile", html
    )
    assert p is not None
    # category path should contain at least 2 levels
    assert len(p.category_path) >= 2
    assert any("Glove" in c for c in p.category_path)


def test_clearance_item_still_extracts(extractor: Extractor) -> None:
    """Edge case: 'clearance' is a real product page even though the slug is generic."""
    html = _read("product_clearance-item.html")
    p, _ = extractor.extract(
        "https://www.safcodental.com/product/clearance-item", html
    )
    assert p is not None
    assert p.name


def test_product_url_is_canonicalized(extractor: Extractor) -> None:
    html = _read("product_aquasoft.html")
    p, _ = extractor.extract(
        "https://www.safcodental.com/product/aquasoft#tab=description", html
    )
    assert p is not None
    assert "#" not in p.product_url


def test_dedup_key_prefers_sku(extractor: Extractor) -> None:
    html = _read("product_aquasoft.html")
    p, _ = extractor.extract("https://www.safcodental.com/product/aquasoft", html)
    assert p is not None
    assert p.dedup_key.startswith("sku:")

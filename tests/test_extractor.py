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
    p, variants, methods = extractor.extract(
        "https://www.safcodental.com/product/aquasoft", html
    )
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
    # masterData should produce variants too
    assert len(variants) >= 1


def test_lavender_nitrile_breadcrumbs(extractor: Extractor) -> None:
    html = _read("product_lavender-nitrile.html")
    p, _variants, _methods = extractor.extract(
        "https://www.safcodental.com/product/lavender-nitrile", html
    )
    assert p is not None
    # category path should contain at least 2 levels
    assert len(p.category_path) >= 2
    assert any("Glove" in c for c in p.category_path)


def test_clearance_item_still_extracts(extractor: Extractor) -> None:
    """Edge case: 'clearance' is a real product page even though the slug is generic."""
    html = _read("product_clearance-item.html")
    p, _variants, _methods = extractor.extract(
        "https://www.safcodental.com/product/clearance-item", html
    )
    assert p is not None
    assert p.name


def test_product_url_is_canonicalized(extractor: Extractor) -> None:
    html = _read("product_aquasoft.html")
    p, _variants, _methods = extractor.extract(
        "https://www.safcodental.com/product/aquasoft#tab=description", html
    )
    assert p is not None
    assert "#" not in p.product_url


def test_dedup_key_prefers_sku(extractor: Extractor) -> None:
    html = _read("product_aquasoft.html")
    p, _variants, _methods = extractor.extract(
        "https://www.safcodental.com/product/aquasoft", html
    )
    assert p is not None
    assert p.dedup_key.startswith("sku:")


# ─── Variant extraction tests ────────────────────────────────────────────────


def test_master_data_parses_aquasoft_variants(extractor: Extractor) -> None:
    """Aquasoft has a masterData block; brand should be the actual mfr, not Safco."""
    html = _read("product_aquasoft.html")
    p, variants, _methods = extractor.extract(
        "https://www.safcodental.com/product/aquasoft", html
    )
    assert p is not None
    assert len(variants) >= 1
    brands = {v.manufacturer_name for v in variants if v.manufacturer_name}
    assert brands and brands != {"Safco Dental"}, (
        f"variant brand should be the real manufacturer, not Safco; got {brands}"
    )


def test_size_pack_parses_xs_300_box() -> None:
    from safco_agent.agents.extractor import _parse_size_pack
    assert _parse_size_pack("X-small, 300/box") == ("X-small", 300, "box")
    assert _parse_size_pack("Medium, 100/case") == ("Medium", 100, "case")
    assert _parse_size_pack("not a pack size") == (None, None, None)
    assert _parse_size_pack(None) == (None, None, None)


def test_html_entities_decoded_in_variant_name(extractor: Extractor) -> None:
    """`&reg;` / `&trade;` / `&nbsp;` should not leak through to variant strings."""
    html = _read("product_alasta-pro.html")
    _, variants, _ = extractor.extract(
        "https://www.safcodental.com/product/alasta-pro", html
    )
    joined = " ".join((v.name or "") + " " + (v.description or "") for v in variants)
    assert "&reg;" not in joined and "&trade;" not in joined
    assert "&nbsp;" not in joined and "&amp;" not in joined


def test_synthetic_variant_when_no_master_data(extractor: Extractor) -> None:
    """Pages without masterData (clearance) still produce one synthetic Variant."""
    html = _read("product_clearance-item.html")
    p, variants, _ = extractor.extract(
        "https://www.safcodental.com/product/clearance-item", html
    )
    assert p is not None
    # Either no masterData (single synthetic) or masterData present.
    # If synthetic, we expect exactly one with is_synthetic=True.
    if all(v.is_synthetic for v in variants):
        assert len(variants) == 1
        assert variants[0].name == p.name


def test_alasta_pro_regression(extractor: Extractor) -> None:
    """Pinned regression for the original failure case the user reported.

    The Alasta Pro page exposes 5 variants (XS/S/M/L/XL × 200/box) with item
    numbers 4681214..4681222 and Mfr# ALGA200XS..ALGA200XL. Brand is `Dash`,
    not `Safco Dental`. Item 4681214 is on backorder while the rest are
    in stock — verifies per-variant availability.
    """
    html = _read("product_alasta-pro.html")
    p, variants, _ = extractor.extract(
        "https://www.safcodental.com/product/alasta-pro", html
    )
    assert p is not None
    by_item = {v.safco_item_number: v for v in variants}

    xs = by_item["4681214"]
    s = by_item["4681216"]
    assert xs.manufacturer_number == "ALGA200XS"
    assert s.manufacturer_number == "ALGA200S"
    assert xs.size == "X-small" and s.size == "Small"
    assert xs.pack_quantity == 200 and xs.pack_unit == "box"
    assert s.pack_quantity == 200 and s.pack_unit == "box"
    assert xs.manufacturer_name == "Dash" and s.manufacturer_name == "Dash"
    assert xs.availability == "backorder"
    assert s.availability == "in_stock"
    assert xs.currency is None  # we never default currency for variants

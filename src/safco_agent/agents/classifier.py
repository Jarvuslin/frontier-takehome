"""PageClassifier — cheap URL+DOM heuristic with optional LLM tiebreaker.

Classes: 'category' | 'subcategory' | 'product' | 'unknown'.

For Safco the URL pattern alone gives near-perfect classification:
  /catalog                       → category root
  /catalog/<slug>                → category
  /catalog/<slug>/<sub-slug>     → subcategory (also a listing page)
  /product/<slug>                → product detail page

DOM markers are used as a secondary check (defense against URL routing changes).
"""
from __future__ import annotations

import re
from typing import Literal

from selectolax.parser import HTMLParser

PageType = Literal["category", "subcategory", "product", "unknown"]

CATEGORY_RE = re.compile(r"/catalog(?:/[^/?#]+)?/?$", re.I)
SUBCATEGORY_RE = re.compile(r"/catalog/[^/?#]+/[^/?#]+/?$", re.I)
PRODUCT_RE = re.compile(r"/product/[^/?#]+/?$", re.I)


def classify_url(url: str) -> PageType:
    if PRODUCT_RE.search(url):
        return "product"
    if SUBCATEGORY_RE.search(url):
        return "subcategory"
    if CATEGORY_RE.search(url):
        return "category"
    return "unknown"


def classify_dom(html: str) -> PageType:
    """Secondary check using DOM markers."""
    tree = HTMLParser(html)
    if tree.css_first("[itemtype*='Product']"):
        return "product"
    if tree.css_first("script[type='application/ld+json']"):
        # may be Product, ItemList, Organization; relies on URL classifier first
        pass
    if tree.css_first(".product-detail, .product-page, h1.product-title, [itemprop='sku']"):
        return "product"
    if tree.css_first(".product-card, .product-list, .category-products"):
        return "subcategory"
    return "unknown"


def classify(url: str, html: str | None = None) -> PageType:
    """Combine URL and DOM signals; URL wins when confident."""
    by_url = classify_url(url)
    if by_url != "unknown":
        return by_url
    if html is None:
        return "unknown"
    return classify_dom(html)

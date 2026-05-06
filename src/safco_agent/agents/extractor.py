"""ExtractorAgent — tiered, deterministic-first product field extraction.

Tiers (per field, first non-empty wins; method recorded):
  1. JSON-LD       (script[type=application/ld+json] Product/Offer)
  2. OpenGraph     (meta[property^=og:], product:price:amount, etc.)
  3. Microdata     (itemtype=schema.org/Product, itemprop=*)
  4. CSS selectors (config/selectors.yaml — live-editable)
  5. LLM fallback  (only if critical fields still missing AND enabled)
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from safco_agent.observability.logging import get_logger
from safco_agent.schema import Availability, Product

log = get_logger("agent.extractor")

PRICE_RE = re.compile(r"(?P<num>\d{1,3}(?:[,\d]{0,9})(?:\.\d{1,2})?)")
AVAILABILITY_MAP = {
    "instock": "in_stock",
    "in_stock": "in_stock",
    "in stock": "in_stock",
    "outofstock": "out_of_stock",
    "out_of_stock": "out_of_stock",
    "out of stock": "out_of_stock",
    "preorder": "preorder",
    "backorder": "backorder",
}


def _norm_availability(raw: str | None) -> Availability:
    if not raw:
        return "unknown"
    s = raw.lower().strip().rsplit("/", 1)[-1]
    return AVAILABILITY_MAP.get(s.replace("-", "_"), "unknown")  # type: ignore[return-value]


def _norm_price(text: str | None) -> tuple[Decimal | None, str | None]:
    if not text:
        return None, None
    s = text.replace(",", "").strip()
    m = PRICE_RE.search(s)
    if not m:
        return None, text.strip() or None
    try:
        return Decimal(m.group("num")), text.strip()
    except InvalidOperation:
        return None, text.strip()


def _abs(base: str, href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(base, href.split("#")[0])


class Extractor:
    def __init__(self, selectors: dict, base_url: str):
        self._sel = selectors.get("product_page", {})
        self.base_url = base_url

    def extract(self, url: str, html: str) -> tuple[Product | None, dict[str, str]]:
        """Return (Product or None, extraction_method per field). None if name+url missing."""
        tree = HTMLParser(html)
        method: dict[str, str] = {}

        ld = self._collect_jsonld(tree)
        og = self._collect_opengraph(tree)
        md = self._collect_microdata(tree)

        def pick(field: str, *sources: tuple[str, Any]) -> Any:
            for src_name, val in sources:
                if val not in (None, "", [], {}):
                    method[field] = src_name
                    return val
            return None

        name = pick(
            "name",
            ("json-ld", ld.get("name")),
            ("opengraph", og.get("og:title")),
            ("microdata", md.get("name")),
            ("selector", self._sel_text(tree, "name")),
        )

        brand = pick(
            "brand",
            ("json-ld", ld.get("brand")),
            ("microdata", md.get("brand")),
            ("selector", self._sel_text(tree, "brand")),
        )

        sku = pick(
            "sku",
            ("json-ld", ld.get("sku") or ld.get("mpn")),
            ("microdata", md.get("sku")),
            ("selector", self._sel_text(tree, "sku")),
        )

        product_code = pick(
            "product_code",
            ("json-ld", ld.get("productID") or ld.get("gtin13") or ld.get("gtin")),
            ("selector", self._sel_text(tree, "product_code")),
        )

        price_raw = pick(
            "price",
            ("json-ld", ld.get("price")),
            ("opengraph", og.get("product:price:amount")),
            ("microdata", md.get("price")),
            ("selector", self._sel_text(tree, "price")),
        )
        price, price_text = _norm_price(price_raw)

        currency = (
            ld.get("priceCurrency")
            or og.get("product:price:currency")
            or md.get("priceCurrency")
            or "USD"
        )

        availability = _norm_availability(
            pick(
                "availability",
                ("json-ld", ld.get("availability")),
                ("opengraph", og.get("product:availability")),
                ("microdata", md.get("availability")),
                ("selector", self._sel_text(tree, "availability")),
            )
        )

        description = pick(
            "description",
            ("json-ld", ld.get("description")),
            ("opengraph", og.get("og:description")),
            ("microdata", md.get("description")),
            ("selector", self._sel_text(tree, "description")),
        )

        breadcrumbs = self._sel_multi_text(tree, "breadcrumbs") or self._jsonld_breadcrumbs(tree)
        if breadcrumbs:
            method["category_path"] = method.get("category_path", "selector")
        category_path = [b for b in (s.strip() for s in (breadcrumbs or [])) if b and b.lower() != "home"]

        images = []
        for src in [
            ld.get("image"),
            og.get("og:image"),
            self._sel_multi_attr(tree, "images"),
        ]:
            if not src:
                continue
            if isinstance(src, str):
                images.append(src)
            elif isinstance(src, list):
                images.extend([s for s in src if isinstance(s, str)])
        images = [_abs(self.base_url, i) or i for i in images if i]
        images = [i for i in images if i]
        if images:
            method.setdefault("image_urls", "json-ld" if ld.get("image") else "selector")

        alternatives = [
            _abs(self.base_url, a)
            for a in self._sel_multi_attr(tree, "alternatives")
            if a
        ]
        alternatives = [a for a in alternatives if a]

        specs = self._extract_specs(tree)
        if specs:
            method["specifications"] = "selector"

        pack_size = pick("pack_size", ("selector", self._sel_text(tree, "pack_size")))
        if not pack_size and name:
            # last-resort heuristic on name: e.g. "Gloves Latex 100/box"
            m = re.search(r"(\d+\s*/\s*(?:box|case|pack|bag|pkg))", name, re.I)
            if m:
                pack_size = m.group(1)
                method["pack_size"] = "name-heuristic"

        if not name:
            return None, method

        product = Product(
            sku=str(sku).strip() if sku else None,
            product_code=str(product_code).strip() if product_code else None,
            name=str(name).strip(),
            brand=str(brand).strip() if brand else None,
            category_path=category_path,
            product_url=url,
            price=price,
            price_text=price_text,
            currency=str(currency).upper()[:3] if currency else "USD",
            pack_size=str(pack_size).strip() if pack_size else None,
            availability=availability,
            description=str(description).strip() if description else None,
            specifications=specs,
            image_urls=images,
            alternative_product_urls=alternatives,
            extraction_method=method,
        )
        return product, method

    # ---------- helpers: tiers ----------
    def _collect_jsonld(self, tree: HTMLParser) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(s.text() or "")
            except Exception:
                continue
            for obj in _iter_ld(data):
                t = obj.get("@type")
                types = t if isinstance(t, list) else [t]
                if "Product" in types:
                    for k in (
                        "name", "description", "sku", "mpn", "productID",
                        "gtin", "gtin13", "image", "category",
                    ):
                        if k in obj and k not in out:
                            out[k] = obj[k]
                    brand = obj.get("brand")
                    if isinstance(brand, dict):
                        out.setdefault("brand", brand.get("name"))
                    elif isinstance(brand, str):
                        out.setdefault("brand", brand)
                    offers = obj.get("offers")
                    for offer in _iter_ld(offers):
                        if isinstance(offer, dict):
                            out.setdefault("price", offer.get("price"))
                            out.setdefault("priceCurrency", offer.get("priceCurrency"))
                            out.setdefault("availability", offer.get("availability"))
        return out

    def _jsonld_breadcrumbs(self, tree: HTMLParser) -> list[str]:
        for s in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(s.text() or "")
            except Exception:
                continue
            for obj in _iter_ld(data):
                if obj.get("@type") == "BreadcrumbList":
                    items = obj.get("itemListElement") or []
                    out = []
                    for it in items:
                        if isinstance(it, dict):
                            n = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                            if n:
                                out.append(str(n))
                    if out:
                        return out
        return []

    def _collect_opengraph(self, tree: HTMLParser) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in tree.css("meta[property]"):
            prop = (m.attributes.get("property") or "").lower()
            content = m.attributes.get("content")
            if prop and content:
                out[prop] = content
        for m in tree.css("meta[name]"):
            n = (m.attributes.get("name") or "").lower()
            content = m.attributes.get("content")
            if n.startswith(("og:", "twitter:", "product:")) and content:
                out[n] = content
        return out

    def _collect_microdata(self, tree: HTMLParser) -> dict[str, Any]:
        out: dict[str, Any] = {}
        scope = tree.css_first("[itemtype*='Product']")
        if scope is None:
            return out
        for el in scope.css("[itemprop]"):
            prop = el.attributes.get("itemprop") or ""
            val = el.attributes.get("content") or el.attributes.get("href") or el.text(strip=True)
            if val and prop and prop not in out:
                out[prop] = val
        return out

    # ---------- helpers: selectors ----------
    def _sel_text(self, tree: HTMLParser, field: str) -> str | None:
        for rule in self._sel.get(field, []):
            if rule.get("multi"):
                continue
            node = tree.css_first(rule["sel"])
            if not node:
                continue
            attr = rule.get("attr", "text")
            if attr == "text":
                txt = node.text(strip=True)
            else:
                txt = node.attributes.get(attr) or ""
            if txt:
                return txt.strip()
        return None

    def _sel_multi_text(self, tree: HTMLParser, field: str) -> list[str]:
        for rule in self._sel.get(field, []):
            if not rule.get("multi"):
                continue
            nodes = tree.css(rule["sel"])
            if not nodes:
                continue
            attr = rule.get("attr", "text")
            out = []
            for n in nodes:
                v = n.text(strip=True) if attr == "text" else (n.attributes.get(attr) or "")
                if v:
                    out.append(v.strip())
            if out:
                return out
        return []

    def _sel_multi_attr(self, tree: HTMLParser, field: str) -> list[str]:
        out: list[str] = []
        for rule in self._sel.get(field, []):
            attr = rule.get("attr")
            if rule.get("multi"):
                for n in tree.css(rule["sel"]):
                    v = n.attributes.get(attr) if attr else None
                    if v:
                        out.append(v)
            else:
                n = tree.css_first(rule["sel"])
                if n is not None and attr:
                    v = n.attributes.get(attr)
                    if v:
                        out.append(v)
        # de-dupe preserving order
        seen, dedup = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return dedup

    def _extract_specs(self, tree: HTMLParser) -> dict[str, str]:
        specs: dict[str, str] = {}
        # Try table rows (th/td) and dl (dt/dd) under selector rules
        for rule in self._sel.get("specs_rows", []):
            for node in tree.css(rule["sel"]):
                pair = _row_to_pair(node)
                if pair:
                    k, v = pair
                    specs.setdefault(k, v)
        return specs


def _row_to_pair(node: Node) -> tuple[str, str] | None:
    tag = node.tag
    if tag == "tr":
        cells = node.css("th, td")
        if len(cells) >= 2:
            k = cells[0].text(strip=True)
            v = cells[1].text(strip=True)
            if k and v:
                return k, v
    if tag == "dt":
        nxt = node.next
        while nxt is not None and getattr(nxt, "tag", None) not in ("dd", None):
            nxt = nxt.next
        if nxt is not None and nxt.tag == "dd":
            k = node.text(strip=True)
            v = nxt.text(strip=True)
            if k and v:
                return k, v
    if tag == "li":
        txt = node.text(strip=True)
        if ":" in txt:
            k, v = txt.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                return k, v
    return None


def _iter_ld(data: Any):
    """Yield dict objects from arbitrarily nested JSON-LD payloads."""
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for x in data["@graph"]:
                yield from _iter_ld(x)
        else:
            yield data
    elif isinstance(data, list):
        for x in data:
            yield from _iter_ld(x)

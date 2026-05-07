"""ExtractorAgent — tiered, deterministic-first product field extraction.

Tiers (per field, first non-empty wins; method recorded):
  1. JSON-LD       (script[type=application/ld+json] Product/Offer)
  2. OpenGraph     (meta[property^=og:], product:price:amount, etc.)
  3. Microdata     (itemtype=schema.org/Product, itemprop=*)
  4. CSS selectors (config/selectors.yaml — live-editable)
  5. LLM fallback  (only if critical fields still missing AND enabled)
"""
from __future__ import annotations

import html as html_module
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from safco_agent.observability.logging import get_logger
from safco_agent.schema import Availability, Product, Variant

log = get_logger("agent.extractor")

# Variant data is embedded in product pages as JS: `window.masterData = "..."`.
# The captured string is a JS string literal containing \uXXXX escapes whose
# decoded form is itself a JSON object keyed by Safco item number.
_MASTERDATA_RE = re.compile(r'window\.masterData\s*=\s*"([^"]+)"')

# Size/pack pattern for variant descriptions like "X-small, 200/box".
_SIZE_PACK_RE = re.compile(r"^\s*([^,]+?)\s*,\s*(\d+)\s*/\s*([A-Za-z]+)\s*$")


def _parse_master_data(html: str) -> dict[str, dict] | None:
    """Extract and decode the masterData blob from a product page.

    Returns a {item_number: variant_dict} mapping, or None if not present
    or if any decoding step fails. The double json.loads is intentional:
    the captured string is a JS string literal — `json.loads` of the wrapped
    form unescapes the \\uXXXX sequences; the resulting string is itself JSON.
    """
    m = _MASTERDATA_RE.search(html)
    if not m:
        return None
    try:
        decoded = json.loads('"' + m.group(1) + '"')
        data = json.loads(decoded)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("masterdata.parse_failed", error=str(e))
        return None
    return data if isinstance(data, dict) else None


def _parse_size_pack(description: str | None) -> tuple[str | None, int | None, str | None]:
    """Parse a variant description like "X-small, 200/box" into (size, qty, unit).

    Returns (None, None, None) if the description doesn't fit the pattern.
    """
    if not description:
        return None, None, None
    m = _SIZE_PACK_RE.match(description)
    if not m:
        return None, None, None
    return m.group(1).strip(), int(m.group(2)), m.group(3).lower()

PRICE_RE = re.compile(r"(?P<num>\d{1,3}(?:[,\d]{0,9})(?:\.\d{1,2})?)")

# Pack-size patterns, tried in priority order. Listing longer unit names first
# (`package` before `pack`) prevents a prefix-match cutting "package" → "pack".
# Intermediate words use [a-zA-Z]+ (not \w+) so digit-containing tokens like
# "3.1" in "3.1 mils 300 gloves per box" can't bridge two separate numbers.
_UNITS = r"(?:carton|package|case|box|bag|pkg|pack|kit|set)"
_PACK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+\s*/\s*" + _UNITS, re.I),                                  # 12/box
    re.compile(r"\d+(?:\s+[a-zA-Z]+){0,3}\s+per\s+" + _UNITS, re.I),           # 300 gloves per box
    re.compile(_UNITS + r"\s+of\s+\d+", re.I),                                 # box of 25
    re.compile(r"(?:package|pack|pkg|box|set|kit)\s*/\s*\d+", re.I),           # pkg/50
    re.compile(r"(?:each\s+)?" + _UNITS + r"\s+(?:contains?|holds?|includes?)" # each box contains 200
               r"\s+\d+(?:\s+[a-zA-Z]+){0,3}", re.I),
    re.compile(r"\d+(?:\s+[a-zA-Z]+){0,3}\s+(?:in|inside)\s+"                  # 200 gloves in each box
               r"(?:each\s+|every\s+)?" + _UNITS, re.I),
)


def _find_pack_in_text(text: str | None) -> str | None:
    """Return the first pack-size phrase found in text, or None.

    Whitespace is normalized to a single space first so that newlines or HTML
    entity remnants can't bridge two unrelated numbers in the source.
    """
    if not text:
        return None
    flat = re.sub(r"\s+", " ", str(text))
    for pattern in _PACK_PATTERNS:
        m = pattern.search(flat)
        if m:
            return m.group(0).strip()
    return None

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


def _clean_text(text: str | None) -> str | None:
    """Decode HTML entities and strip any inline tags (common in Magento JSON-LD).

    Some Safco fields are double-encoded — `&amp;nbsp;` in the raw HTML decodes
    to `&nbsp;` after one pass, which still looks like an entity to a CSV
    reader. Loop until idempotent (capped) so we land on the real characters.
    """
    if not text:
        return None
    for _ in range(3):
        unescaped = html_module.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


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

    def extract(
        self, url: str, html: str
    ) -> tuple[Product | None, list[Variant], dict[str, str]]:
        """Return (Product, variants, methods).

        - Product is None when name+url are missing.
        - variants is the list of purchasable item-number rows from masterData,
          or a single synthetic Variant derived from the parent if no masterData
          is present (so every page still produces at least one output row).
        """
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
        if not pack_size:
            pack_size, ps_source = self._find_pack_size(name, description, specs, tree)
            if pack_size:
                method["pack_size"] = ps_source

        if not name:
            return None, [], method

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
            description=_clean_text(str(description) if description else None),
            specifications=specs,
            image_urls=images,
            alternative_product_urls=alternatives,
            extraction_method=method,
        )

        variants = self._build_variants(html, product, description)
        product.variants = variants
        return product, variants, method

    # ---------- variants ----------

    def _build_variants(
        self, html: str, product: Product, description: Any
    ) -> list[Variant]:
        """Build the per-item-number variants for a product page.

        masterData (when present) is authoritative — it holds Safco's actual
        purchasable rows including per-variant brand and availability. When
        masterData is absent (e.g. clearance pages, products that aren't
        configurable), emit one synthetic Variant from the parent fields so
        every page still produces an output row.
        """
        master = _parse_master_data(html)
        if master:
            return [self._variant_from_master(item, product) for item in master.values()]
        return [self._synthetic_variant(product, description)]

    def _variant_from_master(self, raw: dict, product: Product) -> Variant:
        """Build a Variant from one masterData entry.

        Field map (per plan):
          sku                       -> safco_item_number
          manufacturer_part_number  -> manufacturer_number
          parent_product_sku        -> parent_sku
          manufacturer_name         -> manufacturer_name (brand)
          name, description         -> as-is, after _clean_text
          product_price             -> price (Decimal) + price_text (raw)
          stock_availability        -> availability via AVAILABILITY_MAP
        """
        size, qty, unit = _parse_size_pack(raw.get("description"))
        price, price_text = _norm_price(raw.get("product_price"))
        return Variant(
            parent_dedup_key=product.dedup_key,
            parent_sku=raw.get("parent_product_sku") or product.sku,
            safco_item_number=str(raw.get("sku")).strip() if raw.get("sku") else None,
            manufacturer_number=raw.get("manufacturer_part_number") or None,
            manufacturer_name=_clean_text(raw.get("manufacturer_name")),
            name=_clean_text(raw.get("name")),
            description=_clean_text(raw.get("description")),
            price=price,
            price_text=price_text,
            currency=None,  # masterData doesn't expose currency; do not guess
            availability=_norm_availability(raw.get("stock_availability")),
            availability_label=_clean_text(raw.get("stock_availability_label")),
            size=size,
            pack_quantity=qty,
            pack_unit=unit,
            image=raw.get("image") or None,
            main_image=raw.get("main_image") or None,
            is_synthetic=False,
            extraction_method={"_": "masterdata"},
        )

    def _synthetic_variant(self, product: Product, description: Any) -> Variant:
        """One synthetic Variant for pages without masterData (e.g. clearance)."""
        size = qty = unit = None
        pack_phrase = _find_pack_in_text(product.pack_size) or _find_pack_in_text(description)
        if pack_phrase:
            # pack_phrase is a fragment like "200/box" or "300 gloves per box";
            # try the strict size+pack pattern, else just keep qty/unit if present.
            size, qty, unit = _parse_size_pack(pack_phrase)
        return Variant(
            parent_dedup_key=product.dedup_key,
            parent_sku=product.sku,
            safco_item_number=product.sku or product.product_code,
            manufacturer_number=None,
            manufacturer_name=product.brand,
            name=product.name,
            description=product.description,
            price=product.price,
            price_text=product.price_text,
            currency=None,  # do not guess
            availability=product.availability,
            availability_label=None,
            size=size,
            pack_quantity=qty,
            pack_unit=unit,
            image=product.image_urls[0] if product.image_urls else None,
            main_image=None,
            is_synthetic=True,
            extraction_method={"_": "synthetic"},
        )

    # ---------- pack size ----------

    def _find_pack_size(
        self,
        name: str | None,
        description: Any,
        specs: dict[str, str],
        tree: HTMLParser,
    ) -> tuple[str | None, str]:
        """Search four sources in priority order; return (value, source_label).

        Sources, in order:
          1. name           — most reliable when present (e.g. "Gloves 100/box")
          2. description    — JSON-LD / OG; embeds bullet list for gloves
          3. specs keys     — already-parsed div.prose ul li bullets (sutures)
          4. raw prose text — last resort for non-bullet phrasings
        """
        if (m := _find_pack_in_text(name)):
            return m, "name-heuristic"
        if (m := _find_pack_in_text(description)):
            return m, "description-heuristic"
        for key in specs:
            if (m := _find_pack_in_text(key)):
                return m, "specs-heuristic"
        prose = tree.css_first("div.prose")
        if prose and (m := _find_pack_in_text(prose.text(strip=True))):
            return m, "prose-heuristic"
        return None, ""

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
        if not txt or re.match(r"order\s+\d+", txt, re.I):
            return None
        if ":" in txt:
            k, v = txt.split(":", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                return k, v
        else:
            return txt, "yes"
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

"""DiscoveryAgent — turns seeds into a frontier of product URLs.

Strategy (in order of efficiency):
  1. Parse `sitemap.xml` → `catalog.xml` to enumerate sub-categories under each seed.
  2. Render each category/sub-category page with Playwright to harvest product
     detail links (the listing grid is JS-rendered).
  3. Cross-reference against `products.xml` for any /product/ slugs we missed.

Sitemap is authoritative for *what exists*. Listing render is authoritative for
*what belongs to a category* (since the flat /product/ URLs carry no category).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from selectolax.parser import HTMLParser

from safco_agent.http.browser import BrowserPool
from safco_agent.http.client import HTTPClient
from safco_agent.observability.logging import get_logger
from safco_agent.settings import SeedConfig, Settings

log = get_logger("agent.discovery")

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
PRODUCT_HREF_RE = re.compile(r"/product/[a-z0-9\-]+", re.I)


class DiscoveryAgent:
    def __init__(self, settings: Settings, http: HTTPClient, browser: BrowserPool):
        self.settings = settings
        self.http = http
        self.browser = browser
        self.base = settings.site.base_url.rstrip("/")

    async def fetch_sitemap_index(self) -> list[str]:
        url = f"{self.base}/sitemap.xml"
        r = await self.http.fetch(url)
        root = ET.fromstring(r.text)
        return [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", NS) if loc.text]

    async def fetch_sitemap_urls(self, sitemap_url: str) -> list[str]:
        r = await self.http.fetch(sitemap_url)
        root = ET.fromstring(r.text)
        return [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", NS) if loc.text]

    async def discover_subcategories(self, seed: SeedConfig) -> list[str]:
        """Find all sub-category URLs for a seed via catalog.xml prefix match."""
        index = await self.fetch_sitemap_index()
        catalog_sm = next((u for u in index if u.endswith("/catalog.xml")), None)
        if not catalog_sm:
            log.warning("discovery.no_catalog_sitemap", index=index)
            return [seed.url]
        cat_urls = await self.fetch_sitemap_urls(catalog_sm)
        prefix = seed.url.rstrip("/")
        sub = [u for u in cat_urls if u == prefix or u.startswith(prefix + "/")]
        log.info("discovery.subcategories", seed=seed.id, count=len(sub))
        return sub

    async def harvest_listing(self, listing_url: str) -> tuple[str, list[str]]:
        """Render a listing page and return (rendered_html, product_urls)."""
        result = await self.browser.render(listing_url)
        urls = self._extract_product_links(result.html)
        log.info("discovery.harvest", url=listing_url, products=len(urls))
        return result.html, urls

    def _extract_product_links(self, html: str) -> list[str]:
        tree = HTMLParser(html)
        seen: set[str] = set()
        out: list[str] = []
        for a in tree.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not PRODUCT_HREF_RE.search(href):
                continue
            full = urljoin(self.base, href.split("#")[0])
            # canonical: keep only /product/<slug>
            m = PRODUCT_HREF_RE.search(full)
            if not m:
                continue
            canon = self.base + m.group(0)
            if canon not in seen:
                seen.add(canon)
                out.append(canon)
        return out

    async def discover(self, seeds: list[SeedConfig]) -> dict[str, list[str]]:
        """Return {seed_id: [product_urls]} after traversing sub-categories."""
        out: dict[str, list[str]] = {}
        cap = self.settings.limits.max_products_per_category or 10**9
        for seed in seeds:
            urls: list[str] = []
            sub_categories = await self.discover_subcategories(seed)
            seen: set[str] = set()
            for sub in sub_categories:
                if len(urls) >= cap:
                    break
                try:
                    _, harvested = await self.harvest_listing(sub)
                except Exception as e:
                    log.warning("discovery.listing_failed", url=sub, error=str(e))
                    continue
                for u in harvested:
                    if u in seen:
                        continue
                    seen.add(u)
                    urls.append(u)
                    if len(urls) >= cap:
                        break
            out[seed.id] = urls
            log.info("discovery.seed_done", seed=seed.id, products=len(urls))
        return out

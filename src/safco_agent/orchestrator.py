"""Orchestrator — wires every agent into a runnable crawl.

Phases:
  1. Discovery (sitemap + browser-rendered listings) → frontier of product URLs.
  2. Concurrent extraction over the frontier (HTTP only; product pages are
     server-rendered so we don't pay browser cost per product).
  3. Validate, persist, finalize report + exports.

Resumability: every URL passes through `Store.mark_pending` before fetch and
`mark_done`/`mark_failed` after. A subsequent run with `--resume` skips
URLs in 'done' status.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from safco_agent.agents.classifier import classify
from safco_agent.agents.discovery import DiscoveryAgent
from safco_agent.agents.extractor import Extractor
from safco_agent.agents.llm_fallback import LLMFallbackAgent
from safco_agent.agents.navigator import NavigatorAgent
from safco_agent.agents.validator import Validator
from safco_agent.http.browser import BrowserPool
from safco_agent.http.client import FatalHTTPError, HTTPClient, RetryableError
from safco_agent.observability import debug_bundle
from safco_agent.observability.logging import configure_logging, get_logger
from safco_agent.observability.report import RunStats, write_report
from safco_agent.schema import Product, Variant
from safco_agent.settings import SeedConfig, Settings, load_selectors
from safco_agent.storage.exporters import (
    export_grouped_json,
    export_specifications_jsonl,
    export_variant_csv,
    seed_to_slug,
)
from safco_agent.storage.sqlite import Store

log = get_logger("orchestrator")


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_dirs()
        configure_logging(
            level=settings.log_level,
            fmt=settings.log_format,
            logs_dir=settings.repo_path(settings.paths.logs_dir),
        )
        self.http = HTTPClient(
            user_agent=settings.site.user_agent,
            timeout_seconds=settings.timeouts.http_seconds,
            rps=settings.rate_limit.requests_per_second,
            burst=settings.rate_limit.burst,
            max_attempts=settings.retries.http_max_attempts,
            max_concurrent=settings.limits.max_concurrent_http,
        )
        self.browser = BrowserPool(
            user_agent=settings.site.user_agent,
            max_concurrent=settings.limits.max_concurrent_browser,
            timeout_seconds=settings.timeouts.browser_seconds,
            page_settle_seconds=settings.timeouts.page_settle_seconds,
        )
        self.navigator = NavigatorAgent(
            base_url=settings.site.base_url,
            user_agent=settings.site.user_agent,
            http=self.http,
        )
        self.discovery = DiscoveryAgent(settings, self.http, self.browser)
        self.extractor = Extractor(load_selectors(), settings.site.base_url)
        self.llm = LLMFallbackAgent(settings.llm_fallback)
        self.store = Store(settings.repo_path(settings.paths.sqlite))
        self.validator = Validator()

    # ---------- public ----------
    async def crawl_seeds(self, seed_ids: list[str] | None = None) -> RunStats:
        seeds = self.settings.seeds
        if seed_ids:
            seeds = [s for s in seeds if s.id in seed_ids]
        if not seeds:
            raise ValueError(f"No seeds match: {seed_ids}")

        run_id = uuid4().hex[:12]
        stats = RunStats(run_id=run_id, seeds=[s.id for s in seeds])
        self.store.start_run(run_id, stats.seeds)

        try:
            await self.navigator.load_robots()
            await self.browser.start()
            log.info("crawl.start", run_id=run_id, seeds=stats.seeds)

            frontier = await self.discovery.discover(seeds)
            stats.pages_visited = sum(len(v) for v in frontier.values())

            for seed in seeds:
                seed_urls = [u for u in frontier.get(seed.id, []) if self.navigator.allowed(u)]
                for u in seed_urls:
                    self.store.mark_pending(u, seed.id, "product")

                await self._process_seed(seed, seed_urls, stats, run_id)

            stats.finished_at = datetime.now(timezone.utc).isoformat()
            stats.llm_calls = self.llm.calls_made
            stats.duplicates = self.validator.duplicates
            stats.products_rejected = self.validator.rejected
            stats.products_extracted = self.validator.accepted
            stats.variants_extracted = self.validator.variants_accepted
            stats.variants_rejected = self.validator.variants_rejected
            stats.variants_duplicates = self.validator.variants_duplicates

            self.store.finish_run(
                run_id=run_id,
                pages=stats.pages_visited,
                products=stats.products_extracted,
                failures=stats.failures,
                notes=f"seeds={','.join(stats.seeds)}",
            )

            md_path, _ = write_report(stats, self.settings.repo_path(self.settings.paths.reports_dir))
            log.info("crawl.report_written", path=str(md_path))

            self._export(run_id)
            log.info("crawl.done", run_id=run_id, products=stats.products_extracted)
            return stats

        finally:
            await self.browser.close()
            await self.http.aclose()
            self.store.close()

    # ---------- internals ----------
    async def _process_seed(
        self, seed: SeedConfig, urls: list[str], stats: RunStats, run_id: str
    ) -> None:
        sem = asyncio.Semaphore(self.settings.limits.max_concurrent_http)

        async def worker(u: str) -> None:
            async with sem:
                await self._process_product(u, seed, stats, run_id)

        await asyncio.gather(*(worker(u) for u in urls), return_exceptions=False)

    async def _process_product(
        self, url: str, seed: SeedConfig, stats: RunStats, run_id: str
    ) -> None:
        page_type = classify(url)
        if page_type != "product":
            log.debug("orch.skip_non_product", url=url, page_type=page_type)
            self.store.mark_done(url)
            return
        t0 = time.perf_counter()
        try:
            r = await self.http.fetch(url)
        except FatalHTTPError as e:
            self.store.mark_failed(url, "FatalHTTP", str(e))
            stats.record_failure("FatalHTTP")
            debug_bundle.save(
                self.settings.repo_path(self.settings.paths.debug_dir), url, e,
                page_type=page_type, attempts=self.settings.retries.http_max_attempts,
            )
            return
        except RetryableError as e:
            self.store.mark_failed(url, "RetryableExhausted", str(e))
            stats.record_failure("RetryableExhausted")
            debug_bundle.save(
                self.settings.repo_path(self.settings.paths.debug_dir), url, e,
                page_type=page_type, attempts=self.settings.retries.http_max_attempts,
            )
            return
        except Exception as e:  # last-resort safety net
            self.store.mark_failed(url, type(e).__name__, str(e))
            stats.record_failure(type(e).__name__)
            debug_bundle.save(
                self.settings.repo_path(self.settings.paths.debug_dir), url, e,
                page_type=page_type, attempts=1,
            )
            return

        stats.latencies_ms.append(r.elapsed_ms)
        product, variants, methods = self.extractor.extract(url, r.text)

        if product is None and self.llm.available:
            log.info("orch.llm_fallback", url=url)
            llm_data = self.llm.extract(url, r.text)
            if llm_data and llm_data.get("name"):
                product, variants = self._product_from_llm(url, llm_data, seed.id, run_id)
                methods = {k: "llm" for k in llm_data.keys() if llm_data.get(k)}

        if product is None:
            err = ValueError("missing_critical_fields")
            self.store.mark_failed(url, "ExtractionMissingFields", str(err))
            stats.record_failure("ExtractionMissingFields")
            debug_bundle.save(
                self.settings.repo_path(self.settings.paths.debug_dir), url, err,
                html=r.text, page_type=page_type, attempts=1,
                extra={"selector_methods_attempted": methods},
            )
            return

        product.source_seed = seed.id
        product.crawl_run_id = run_id
        if not product.category_path:
            product.category_path = [seed.label]

        ok, reason = self.validator.validate(product)
        if not ok and reason == "duplicate":
            self.store.mark_done(url)
            return
        if not ok:
            self.store.mark_failed(url, "ValidatorRejected", reason or "unknown")
            stats.record_failure("ValidatorRejected")
            return

        self.store.upsert_product(product)

        # Variants — set parent_dedup_key (extractor cannot know it yet because
        # source_seed and crawl_run_id are stamped here), then validate and persist.
        accepted_variants = []
        for v in variants:
            v.parent_dedup_key = product.dedup_key
            ok, _reason = self.validator.validate_variant(v)
            if ok:
                accepted_variants.append(v)
        self.store.upsert_variants(product.dedup_key, accepted_variants)

        self.store.mark_done(url)
        missing = [
            f for f in ("sku", "brand", "price", "description")
            if getattr(product, f, None) in (None, "", [], {})
        ]
        stats.record_extraction(methods, missing)
        log.info(
            "orch.product",
            url=url, sku=product.sku, name=product.name[:60],
            variants=len(accepted_variants),
            price=str(product.price) if product.price else None,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    def _product_from_llm(
        self, url: str, d: dict, seed_id: str, run_id: str
    ) -> tuple[Product, list[Variant]]:
        """LLM-fallback path: build the parent Product and one synthetic Variant.

        The LLM only sees a stripped page snippet and returns flat fields; we
        don't ask it to enumerate variants. One synthetic Variant per parent
        keeps the variant-grain contract intact for downstream code.
        """
        price = None
        price_text = d.get("price")
        if price_text:
            m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", price_text)
            if m:
                try:
                    price = Decimal(m.group(0).replace(",", ""))
                except InvalidOperation:
                    price = None
        product = Product(
            sku=d.get("sku"),
            name=d["name"],
            brand=d.get("brand"),
            category_path=d.get("category_path") or [],
            product_url=url,
            price=price,
            price_text=price_text,
            availability=d.get("availability") or "unknown",  # type: ignore[arg-type]
            description=d.get("description"),
            specifications=d.get("specifications") or {},
            pack_size=d.get("pack_size"),
            source_seed=seed_id,
            crawl_run_id=run_id,
            extraction_method={"_": "llm"},
        )
        synthetic = Variant(
            parent_dedup_key=product.dedup_key,
            parent_sku=product.sku,
            safco_item_number=product.sku,
            manufacturer_name=product.brand,
            name=product.name,
            description=product.description,
            price=product.price,
            price_text=product.price_text,
            currency=None,
            availability=product.availability,
            is_synthetic=True,
            extraction_method={"_": "llm"},
        )
        return product, [synthetic]

    def _export(self, run_id: str) -> None:
        """Write the four required output files plus optional grouped JSON.

        - `products_all.csv`         flat, one row per variant (master export)
        - `products_<seed>.csv`      same shape, filtered to one seed
        - `specifications.jsonl`     parent-grouped, one product per line,
                                      nested variants + parsed specs
        - `products_grouped.json`    optional readable JSON array (best-effort)
        """
        out = self.settings.repo_path(self.settings.paths.exports_dir)

        n_all = export_variant_csv(self.store, out / "products_all.csv")
        per_seed: dict[str, int] = {}
        for seed in self.settings.seeds:
            slug = seed_to_slug(seed.id)
            per_seed[slug] = export_variant_csv(
                self.store, out / f"products_{slug}.csv", seed_filter=seed.id
            )
        n_jsonl = export_specifications_jsonl(self.store, out / "specifications.jsonl")
        n_grouped = export_grouped_json(self.store, out / "products_grouped.json")

        log.info(
            "export.done",
            products_all=n_all, per_seed=per_seed,
            specifications_jsonl=n_jsonl, grouped_json=n_grouped,
        )

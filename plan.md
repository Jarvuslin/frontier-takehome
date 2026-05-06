# Safco Catalog Agent — Implementation Plan

## Context

Frontier Dental take-home: build a runnable, agent-based prototype that crawls Safco Dental Supply (`safcodental.com`), traverses two seed categories (`/catalog/sutures-surgical-products`, `/catalog/gloves`), extracts structured product data, and persists it for query/export. The bar is **production-minded prototype**, not a complete crawl, not a slide deck. 24-hour budget. Working directory `c:\Users\7474g\OneDrive\Desktop\frontier-takehome` is currently empty — greenfield.

---

## Reconnaissance Findings

1. **`robots.txt` is permissive** for `/catalog/<slug>` and `/product/<slug>` paths. Disallows `?page=`, `?sortBy=`, `?price=`, `/checkout/`, `/customer/`, Magento internals. No `Crawl-delay` — we self-impose 1 req/s.
2. **Sitemaps exist and are gold:**
   - `sitemap.xml` (index) → `catalog.xml` (~530 category URLs) + `products.xml` (~1200 product URLs)
   - Both seed categories have sub-categories
3. **Rendering split:**
   - Category listing pages render **client-side** (JS) → Playwright required
   - Product detail pages are **server-rendered** → plain async httpx (~10× faster)
4. Detail pages expose: name, price, brand, breadcrumbs, in-stock — all required fields. JSON-LD treated as first-choice extractor.
5. B2B checkout gate is irrelevant — doesn't block catalog scraping.

---

## Architecture Decision

**Hybrid discovery + tiered extraction.** Playwright only where the site forces our hand (category listing render); HTTP everywhere else. LLM only as **last-resort fallback** — not in the hot path.

```
                ┌──────────────────────────────────────────────────┐
                │             Orchestrator (asyncio)               │
                │   config-driven · checkpoint · rate-limit · log  │
                └──┬─────────────┬─────────────┬─────────┬─────────┘
                   ▼             ▼             ▼         ▼
           ┌──────────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐
           │ DiscoveryAgt │ │NavigatorA│ │PageClass.│ │ExtractorAg │
           │ sitemap +    │ │frontier  │ │URL+DOM   │ │JSON-LD →   │
           │ Playwright   │ │+ robots  │ │heuristic;│ │OG → micro- │
           │ category     │ │+ token   │ │LLM only  │ │data → CSS  │
           │ render       │ │bucket    │ │if unsure │ │→ LLM       │
           └──────────────┘ └──────────┘ └──────────┘ └─────┬──────┘
                                                            │
                            ┌────────────┐  ┌───────────┐  ┌▼──────────┐
                            │ Persister  │◄─│ Validator │◄─│ Normalize │
                            │ SQLite +   │  │ Pydantic, │  │ price str │
                            │ JSONL/CSV  │  │ dedup SKU │  │ → cents   │
                            └────────────┘  └───────────┘  └───────────┘
                                  │
              failure path ──────►│ DebugBundle: html, screenshot, error.json
              run summary ───────►│ DataQualityReport: counts, missing-field %
```

---

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | preferred per brief |
| HTTP | `httpx[http2]` async | fast, modern, retry-friendly |
| JS render | `playwright` (Chromium headless) | only for listing pages |
| HTML parse | `selectolax` (primary) + `beautifulsoup4` (fallback) | fast + ergonomic |
| Schema | `pydantic` v2 | validation + JSON Schema export |
| Storage | SQLite + CSV + JSONL | queryable + portable + lossless |
| Logs | `structlog` → JSONL | structured, grep-able |
| Retries | `tenacity` | classified backoff |
| Rate limit | `aiolimiter` | token bucket |
| LLM (optional) | `anthropic` SDK, Claude Haiku 4.5 | cheap, off by default |
| CLI | `typer` | clean subcommands |
| Tests | `pytest` + saved HTML fixtures | offline, deterministic |
| Container | `Dockerfile` + `docker-compose.yml` | deployment path |

---

## Agent Responsibilities

- **DiscoveryAgent** — fetches sitemap XML, renders category listing pages with Playwright, harvests product URLs
- **NavigatorAgent** — owns frontier queue, applies robots.txt, enforces token-bucket rate limit, deduplicates, persists state for resume
- **PageClassifier** — classifies URLs as `category | sub-category | listing | product | unknown` via URL regex + DOM markers; LLM only when heuristic returns `unknown`
- **ExtractorAgent** — tiered: JSON-LD → OpenGraph → Microdata → CSS selectors → LLM fallback; records `extraction_method` per field
- **Validator/Deduplicator** — Pydantic schema, dedup key = `sku` if present else SHA-1 of canonical URL
- **Persister** — atomic upsert into SQLite; dumps CSV/JSONL on run-end
- **DebugBundleSaver** — on failure writes `debug/{url_hash}/{html.gz, screenshot.png, error.json}`
- **DataQualityReporter** — emits `data/reports/run-{ts}.md/.json` with counts, missing-field rate, extraction-method distribution, latency p50/p95

---

## Schema (Pydantic)

```python
class Product:
    sku: str | None
    product_code: str | None
    name: str
    brand: str | None
    category_path: list[str]
    product_url: str
    price: Decimal | None
    price_text: str | None
    currency: str = "USD"
    pack_size: str | None
    availability: str | None     # "in_stock" | "out_of_stock" | "backorder" | None
    description: str | None
    specifications: dict[str, str]
    image_urls: list[str]
    alternative_product_urls: list[str]
    source_seed: str
    extraction_method: dict[str, str]
    extracted_at: datetime
    crawl_run_id: str
```

---

## Repository Layout

```
frontier-takehome/
├── README.md
├── plan.md
├── pyproject.toml
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── config/
│   ├── crawler.yaml
│   └── selectors.yaml
├── src/safco_agent/
│   ├── cli.py
│   ├── settings.py
│   ├── schema.py
│   ├── orchestrator.py
│   ├── agents/
│   │   ├── discovery.py
│   │   ├── navigator.py
│   │   ├── classifier.py
│   │   ├── extractor.py
│   │   ├── validator.py
│   │   └── llm_fallback.py
│   ├── storage/
│   │   ├── sqlite.py
│   │   └── exporters.py
│   ├── observability/
│   │   ├── logging.py
│   │   ├── debug_bundle.py
│   │   └── report.py
│   └── http/
│       ├── client.py
│       └── browser.py
├── tests/
│   ├── fixtures/
│   ├── test_extractor.py
│   ├── test_classifier.py
│   ├── test_validator.py
│   └── test_storage.py
└── data/
    ├── exports/
    ├── reports/
    └── samples/    ← committed sample dataset
```

---

## Implementation Order (24h budget)

| Hours | Task |
|---|---|
| 0–1 | Scaffold (pyproject.toml, Makefile, .gitignore, config YAML, package layout) |
| 1–3 | http/client.py, http/browser.py, observability/logging.py, settings |
| 3–5 | agents/discovery.py (sitemap → category + product URL frontier; Playwright listing render) |
| 5–7 | schema.py, agents/extractor.py (JSON-LD → OG → microdata → selector). Save HTML fixtures. |
| 7–9 | agents/classifier.py, agents/validator.py, storage/sqlite.py + exporters.py. First end-to-end run. |
| 9–11 | orchestrator.py, cli.py; checkpoint/resume tested by killing mid-run |
| 11–13 | llm_fallback.py, debug_bundle.py, report.py |
| 13–15 | pytest suite on saved fixtures (no network) |
| 15–17 | Full run on both seeds, cap 50 products/category; commit sample outputs |
| 17–20 | README (architecture, why-this-approach, setup, schema, sample queries, limitations, scaling, monitoring), Dockerfile validation |
| 20–24 | Buffer for site quirks + final self-review against PDF rubric |

---

## Known Limitations (to document)

- Demo run caps products per category (configurable); full crawl works but takes ~25–30 min
- Variant SKUs without per-variant detail URL: extracted as `specifications.variants`; per-variant pricing may be incomplete
- LLM fallback requires API key + opt-in; default deterministic path
- `?page=` pagination not crawled (robots.txt disallow); bypassed via sitemap + listing render

---

## Scaling Notes

- Discovery via sitemap is O(categories) not O(pages crawled)
- Frontier + crawl_state in SQLite scales to ~10⁵ URLs; swap to Postgres + Redis beyond that
- Horizontal scale: shard categories across workers via same frontier
- Selector drift handled by `selectors.yaml` (no code change) + LLM repair suggestions
- Monitoring: structlog JSONL → Loki/Datadog; per-run metrics → Prometheus; data-quality report → Slack webhook on threshold breach

---

## Verification (end-to-end)

```bash
# from a clean clone
make install                  # creates venv, installs deps + Playwright browsers
cp .env.example .env          # no API key needed by default
make crawl                    # runs both seed categories, ~5–10 min capped
make report                   # opens data/reports/run-{ts}.md
make test                     # pytest fixtures; no network
sqlite3 data/products.db "SELECT brand, COUNT(*) FROM products GROUP BY brand ORDER BY 2 DESC LIMIT 10;"
```

## Acceptance Checklist (PDF rubric)

- [ ] Discovers categories (sitemap + listing render)
- [ ] Traverses category → listing → product
- [ ] Extracts all required fields where publicly available
- [ ] Normalized output (Pydantic + SQLite + CSV + JSONL)
- [ ] Agent-based design with separated responsibilities
- [ ] Production hardening (rate limit, retries, checkpoints, logging, dedup, idempotency, config-driven, secrets, Docker)
- [ ] Sample dataset committed
- [ ] README with architecture / scaling / monitoring sections
- [ ] Practical AI usage (LLM only as fallback, optional, gated)

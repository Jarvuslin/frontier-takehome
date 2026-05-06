# Safco Catalog Agent

An agent-based prototype that crawls [Safco Dental Supply](https://safcodental.com), traverses two seed categories (`sutures-surgical-products`, `gloves`), extracts structured product data with a five-tier extraction pipeline, and persists it to SQLite, CSV, and JSONL.

Built as a production-minded prototype with rate-limiting, checkpointing, deduplication, structured logging, and an optional LLM fallback — not as a one-shot script.

---

## Why This Approach

**Hybrid Playwright + httpx** — reconnaissance showed category listing pages are client-side rendered (JavaScript) while product detail pages are server-rendered HTML. Using Playwright everywhere would be 10× slower and more fragile; using plain HTTP everywhere would miss the product URLs entirely. The hybrid approach uses the right tool for each page type.

**Sitemap-first discovery** — `products.xml` contains ~1200 product URLs and `catalog.xml` contains ~530 category URLs. Starting from the sitemap is O(categories), not O(pages-crawled), and sidesteps `?page=` pagination which `robots.txt` explicitly disallows.

**LLM as last resort, not first reach** — the site exposes JSON-LD structured data on every product page, making deterministic extraction reliable and fast. LLM fallback is wired in and gated behind a config flag (`llm.enabled: true`) so it activates only for layout-irregular pages. In the sample run, zero LLM calls were needed. This keeps cost and latency predictable and keeps the pipeline auditable.

**SQLite for storage** — portable, zero-infra, queryable with standard SQL, and the schema mirrors a Postgres schema exactly. Swap the connection string to scale up; no application code changes needed.

**Config-driven selectors** — CSS selectors live in `config/selectors.yaml`, not in source code. When the site drifts, operators update YAML; no redeploy required.

---

## Architecture

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

**Design decision — hybrid Playwright + HTTP:** Category listing pages render client-side (JavaScript); product detail pages are server-rendered. Playwright is used only where forced; plain `httpx` async handles everything else (~10× faster).

**Design decision — LLM as last resort:** The extraction pipeline tries JSON-LD → OpenGraph → Microdata → CSS selectors before ever touching the LLM. In the sample run, 100% of fields were extracted by JSON-LD, so zero LLM calls were made.

---

## Agents

| Agent | File | Responsibility |
|---|---|---|
| `DiscoveryAgent` | `agents/discovery.py` | Fetches sitemap XML, renders category listing pages with Playwright, harvests product URLs |
| `NavigatorAgent` | `agents/navigator.py` | Owns the frontier queue, applies `robots.txt`, enforces token-bucket rate limit, deduplicates, persists state for resume |
| `PageClassifier` | `agents/classifier.py` | Classifies URLs as `category \| sub-category \| listing \| product \| unknown` via URL regex + DOM markers; LLM only when heuristic returns `unknown` |
| `ExtractorAgent` | `agents/extractor.py` | Five-tier extraction: JSON-LD → OpenGraph → Microdata → CSS selectors → LLM fallback; records `extraction_method` per field |
| `ValidatorAgent` | `agents/validator.py` | Pydantic schema validation; dedup key = `sku` when present, else SHA-1 of canonical URL |
| `LLMFallback` | `agents/llm_fallback.py` | Claude Haiku 4.5 via Anthropic SDK; gated behind `ANTHROPIC_API_KEY` and `llm.enabled: true` |
| `Persister` | `storage/sqlite.py` | Atomic upsert into SQLite with idempotency; 6 normalized tables |
| `DebugBundleSaver` | `observability/debug_bundle.py` | On failure writes `debug/{url_hash}/{html.gz, screenshot.png, error.json}` |
| `DataQualityReporter` | `observability/report.py` | Emits `data/reports/run-{ts}.md/.json` with counts, missing-field rates, extraction-method distribution, latency p50/p95 |

---

## Schema

Defined in `src/safco_agent/schema.py` (Pydantic v2). Export via `safco schema-dump`.

```python
class Product(BaseModel):
    # Identity
    sku: str | None
    product_code: str | None
    name: str                           # required
    brand: str | None
    category_path: list[str]
    product_url: str                    # required; canonicalized (fragment + trailing slash stripped)

    # Commercial
    price: Decimal | None
    price_text: str | None
    currency: str = "USD"
    pack_size: str | None
    availability: Literal["in_stock", "out_of_stock", "backorder", "preorder", "unknown"]

    # Descriptive
    description: str | None
    specifications: dict[str, str]
    image_urls: list[str]
    alternative_product_urls: list[str]

    # Provenance
    source_seed: str | None
    extraction_method: dict[str, str]   # {"name": "json-ld", "price": "selector", ...}
    extracted_at: datetime
    crawl_run_id: str | None
```

**Dedup key:** `sku:<sku>` when present, `url:<sha1(canonical_url)>` otherwise.

---

## Quick Start

### Option A — Local (uv)

```bash
# 1. Install dependencies + Playwright Chromium (~2 min)
make install
# or: uv sync --python 3.12 && uv run python -m playwright install chromium

# 2. Configure (no API key needed by default)
cp .env.example .env

# 3. Run — both seed categories, capped at 50 products/seed
make crawl

# 4. Explore results
make report
sqlite3 data/products.db "SELECT brand, COUNT(*) FROM products GROUP BY brand ORDER BY 2 DESC LIMIT 10;"
```

### Option B — Docker

```bash
docker-compose up crawl
```

---

## CLI Commands

```
safco crawl          Run full agent pipeline on all configured seeds
safco crawl -s gloves                     Single seed
safco discover       Print URL frontier without crawling
safco report         Tail the latest data-quality report
safco stats          Quick brand/count table from the live DB
safco schema-dump    Write JSON Schema to data/exports/schema.json
```

Use `--config path/to/crawler.yaml` on any command to override the default config.

---

## Configuration

`config/crawler.yaml` is the primary knob. Key sections:

```yaml
site:
  base_url: https://www.safcodental.com
  user_agent: "SafcoAgent/0.1 (+research-prototype)"

rate_limit:
  requests_per_second: 1.0
  burst: 3

limits:
  max_concurrent_browser: 2
  max_concurrent_http: 8
  max_products_per_seed: 50          # cap for demo; set to null for full crawl

llm:
  enabled: false                     # true = enable LLM fallback
  model: claude-haiku-4-5-20251001
  max_tokens: 512
```

CSS selectors live in `config/selectors.yaml` — update without touching Python source when the site drifts.

Environment variables (`.env` / shell) override any YAML value:

```
ANTHROPIC_API_KEY=sk-...
SAFCO_LLM__ENABLED=true
SAFCO_LIMITS__MAX_PRODUCTS_PER_SEED=200
```

---

## Tests

The test suite runs entirely offline against three saved HTML fixtures (real product pages captured from the live site).

```bash
make test
# or: uv run pytest -v
```

```
17 passed in 3.57s
tests/test_classifier.py   — URL + DOM classification heuristics
tests/test_extractor.py    — JSON-LD extraction, breadcrumbs, canonicalization, dedup key
tests/test_storage.py      — upsert idempotency, CSV/JSONL roundtrip, crawl-state lifecycle
tests/test_validator.py    — Pydantic acceptance, SKU dedup, URL-hash dedup
```

---

## Sample Run (committed)

Captured 2026-05-06 — both seeds, 50-product cap.

| Metric | Value |
|---|---|
| Pages visited | 50 |
| Products extracted | 49 |
| Duplicates skipped | 1 |
| Failed pages | 0 |
| LLM fallback calls | 0 |
| Latency p50 | 1133 ms |
| Latency p95 | 1822 ms |
| Extraction method | JSON-LD (100% of fields) |

```
data/samples/
├── products.csv         — flat export (49 rows)
├── products.jsonl       — one JSON object per line
├── specifications.csv   — key/value specs (normalized)
├── run-report.md        — human-readable quality report
└── run-report.json      — machine-readable version
```

### Sample SQL queries

```sql
-- Products by brand
SELECT COALESCE(brand,'<unknown>') AS brand, COUNT(*) AS n
FROM products
GROUP BY brand ORDER BY n DESC LIMIT 10;

-- Average price by category
SELECT c.label, ROUND(AVG(p.price_cents) / 100.0, 2) AS avg_price_usd
FROM products p
JOIN product_categories pc ON pc.product_id = p.id
JOIN categories c ON c.id = pc.category_id
GROUP BY c.label ORDER BY avg_price_usd DESC;

-- In-stock vs out-of-stock
SELECT availability, COUNT(*) FROM products GROUP BY availability;

-- Extraction method distribution
SELECT field, method, COUNT(*) AS n
FROM extraction_methods GROUP BY field, method ORDER BY n DESC;

-- Recent run history
SELECT run_id, started_at, finished_at, products_accepted, products_failed
FROM crawl_runs ORDER BY started_at DESC LIMIT 5;
```

---

## Observability

- **Structured logs:** `structlog` JSONL → `logs/crawl.jsonl`. Every page fetch, extraction result, and error has `run_id`, `url`, `elapsed_ms`, and `level`.
- **Debug bundles:** On any extraction failure, `debug/{url_hash}/` contains `html.gz`, `screenshot.png` (if browser), and `error.json`.
- **Per-run quality report:** `data/reports/run-{ts}.md` — counts, missing-field rates, extraction-method distribution, latency percentiles.

---

## Failure Handling

Every failure path is handled at three levels:

**HTTP / network errors** (`http/client.py`)
- `RetryableError` (5xx, connection reset, timeout) → `tenacity` retries with exponential backoff (3 attempts, 1s–8s window)
- `FatalHTTPError` (4xx) → logged and skipped; URL marked `failed` in `crawl_state` table so it is not retried on resume

**Extraction failures** (`agents/extractor.py`, `observability/debug_bundle.py`)
- If all five extraction tiers fail to produce a required field, the page is treated as a failure
- A debug bundle is written to `debug/{url_hash}/`: `html.gz` (full page source), `screenshot.png` (if rendered via browser), `error.json` (exception class, message, traceback, URL, timestamp)
- The failure is counted in the run report under "Failed pages" and "Failures by error class"

**Validation failures** (`agents/validator.py`)
- Products that fail Pydantic validation (missing required `name` or `product_url`) are rejected and logged; the URL is not retried unless the extractor is fixed
- Soft failures (missing optional fields) are accepted; missing-field rates appear in the quality report

**Resume / checkpointing** (`storage/sqlite.py`, `orchestrator.py`)
- `crawl_state` table records `pending → in_progress → done / failed` per URL
- Killing the process mid-run and restarting picks up from `pending` URLs; no duplicate work
- `crawl_runs` table tracks start/finish time and counts per run for audit

**Rate limit / politeness**
- `aiolimiter` token bucket enforces ≤1 req/s by default; burst=3 for brief bursts
- All requests carry a descriptive `User-Agent` header (configurable)
- `robots.txt` is checked before any URL is added to the frontier

---

## Known Limitations

- **Product cap** — demo defaults to 50 products/seed; set `limits.max_products_per_seed: null` for a full crawl (~25–30 min)
- **`?page=` pagination** — disallowed by `robots.txt`; we bypass it via sitemap (`products.xml`, ~1200 URLs) + listing-page render
- **Variant pricing** — SKU variants without individual detail URLs are recorded as `specifications.variants`; per-variant pricing may be incomplete
- **LLM fallback** — requires `ANTHROPIC_API_KEY` and `llm.enabled: true` in config; the default deterministic pipeline handled 100% of the sample run without it

---

## Scaling Path

| Concern | Now (prototype) | At scale |
|---|---|---|
| Discovery | Sitemap XML — O(categories) | Same; sitemap is the canonical frontier |
| Frontier + state | SQLite | Postgres + Redis; same SQLite schema |
| Workers | Single process, async | Shard categories across N workers via shared frontier |
| Selector drift | `selectors.yaml` (no code change) | LLM suggests repair when selector yields empty |
| Logs | Local JSONL | Ship to Loki / Datadog via log tail |
| Metrics | Per-run Markdown report | Prometheus counters + Grafana; alert on data-quality threshold |
| Notifications | None | Slack webhook on `missing_field_rate > 5%` or `failed_pages > 10%` |

---

## Stack

| Concern | Library |
|---|---|
| HTTP | `httpx[http2]` async |
| JS render | `playwright` Chromium headless |
| HTML parse | `selectolax` (primary) + `beautifulsoup4` |
| Schema + validation | `pydantic` v2 |
| Storage | SQLite + CSV + JSONL |
| Structured logs | `structlog` |
| Retries | `tenacity` with classified backoff |
| Rate limit | `aiolimiter` token bucket |
| LLM (optional) | `anthropic` SDK, Claude Haiku 4.5 |
| CLI | `typer` + `rich` |
| Tests | `pytest` + saved HTML fixtures (offline) |
| Container | `Dockerfile` + `docker-compose.yml` |

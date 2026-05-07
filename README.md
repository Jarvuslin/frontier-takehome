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

## Data Model — Parent Products vs Variants

Each Safco product page is a **parent product** (the catalog/master record). Inside that page is a table of **purchasable variants** — one row per orderable item, each with its own Safco item number, manufacturer number, size, pack quantity, price, and availability. One parent typically yields 4–10 variants (sizes XS/S/M/L/XL × pack options).

We model both:

```python
class Product(BaseModel):
    """Parent product — the page-level master record."""
    sku: str | None              # parent SKU (Magento configurable SKU)
    name: str
    brand: str | None            # JSON-LD field — often inaccurate (= retailer)
    category_path: list[str]
    product_url: str
    description: str | None
    specifications: dict[str, str]
    image_urls: list[str]
    alternative_product_urls: list[str]
    variants: list[Variant]      # nested
    # ... + price/availability/etc inherited from JSON-LD


class Variant(BaseModel):
    """One purchasable item-number row from window.masterData."""
    parent_dedup_key: str
    parent_sku: str | None
    safco_item_number: str       # Item # (Safco internal)
    manufacturer_number: str     # Mfr # (e.g. ALGA200XS)
    manufacturer_name: str       # actual brand (e.g. Dash, Halyard)
    name: str
    description: str             # e.g. "X-small, 200/box"
    price: Decimal | None
    currency: str | None         # never defaulted — null when undetected
    availability: Literal[...]   # per-variant
    size: str | None             # parsed from description
    pack_quantity: int | None    # parsed
    pack_unit: str | None        # parsed
    image: str | None
    main_image: str | None
    is_synthetic: bool           # True if this row was synthesized from parent
                                 # (page had no masterData)
```

`brand` (manufacturer) and `retailer` ("Safco Dental") are kept distinct so that the JSON-LD's tendency to label every product as `Safco Dental`-branded doesn't pollute the catalog.

**Dedup keys**
- `Product.dedup_key`: `sku:<lower>` if SKU present, else `url:<sha1>`.
- `Variant.dedup_key`: `variant:<parent_dedup_key>:<safco_item_number>` (with fallbacks through `manufacturer_number` and `name` so missing parent SKUs never collide).

---

## Output Files

A crawl writes to `output/` (configurable via `paths.exports_dir`). All files are produced from the same SQLite source of truth so they stay in sync.

| File | Grain | Purpose |
|---|---|---|
| `output/products_all.csv` | one row per **variant** | Master flat export — easiest for spreadsheet review |
| `output/products_gloves.csv` | one row per **variant** | Same shape, filtered to the gloves seed |
| `output/products_sutures_surgical.csv` | one row per **variant** | Same shape, filtered to sutures/surgical seed |
| `output/specifications.jsonl` | one line per **parent** | Catalog-shaped: nested variants, parsed specs, `extraction_quality` block |
| `output/products_grouped.json` | array of **parents** | Pretty-printed JSON twin of the JSONL — human-readable |
| `data/products.db` | SQLite | Source-of-truth for queries |

Row-count invariant: `products_all.csv == products_gloves.csv + products_sutures_surgical.csv` (excluding headers). Per-seed CSVs are added automatically for any new seed in `crawler.yaml` — naming follows `products_{slug}.csv` where the slug strips a trailing `-products` and replaces hyphens with underscores.

### CSV columns
Variant-grain rows include both **parent context** (`parent_sku`, `parent_name`, `parent_description`, `category_path_str`, `product_url`, `source_seed`, `retailer`) and **variant fields** (`sku`, `product_code`, `safco_item_number`, `product_name`, `manufacturer_number`, `brand`, `description`, `size`, `pack_quantity`, `pack_unit`, `price`, `price_text`, `currency`, `availability`, `availability_label`, `variant_image`, `image_urls_str`, `extracted_at`, `is_synthetic`).

The aliases `sku`, `name`, and `product_code` all mirror `safco_item_number` / `product_name` so a reviewer scanning the file finds familiar columns without consulting docs.

### specifications.jsonl shape
One line per parent product:
```json
{
  "parent_sku": "DRCDK",
  "parent_name": "Alasta Pro",
  "brand": "Dash",
  "retailer": "Safco Dental",
  "category_path": ["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"],
  "category_path_str": "Dental Supplies > Dental Exam Gloves > Nitrile gloves",
  "product_url": "https://www.safcodental.com/product/alasta-pro",
  "description": "Powder-free nitrile exam gloves...",
  "specifications": {
    "material": "nitrile",
    "powder_free": true,
    "color": "blue",
    "thickness_palm_mils": 3.1,
    "thickness_fingertip_mils": 3.9,
    "case_quantity_boxes": 10
  },
  "variants": [
    {"safco_item_number": "4681214", "size": "X-small",
     "pack_quantity": 200, "pack_unit": "box",
     "manufacturer_number": "ALGA200XS", "brand": "Dash",
     "price": 23.49, "availability": "backorder", ...},
    ...
  ],
  "images": ["https://.../drcdk.jpg"],
  "alternative_products": [],
  "extraction_quality": {
    "spec_source": "glove-rules",
    "has_variants": true,
    "variant_count": 5,
    "placeholder_images_filtered": true,
    "missing_fields": ["alternative_products", "currency"]
  },
  "extracted_at": "..."
}
```

### Specifications parsing
`specifications.jsonl` enriches each parent record with attributes parsed from descriptions using **deterministic rules** (regex — no LLM). Two extractor families:
- **Glove rules**: material, powder_free, latex_free, sterile, color, cuff, texture, chlorinated, ambidextrous, thickness (palm/fingertip/generic, in mils), case_quantity_boxes
- **Surgical rules**: suture_size (e.g. `4-0`), absorbable, sterile, material (silk/nylon/PTFE/...), dimensions (`15 x 20mm`), needle_length_mm

The active rule set is logged per-record in `extraction_quality.spec_source`. Attributes that can't be parsed are omitted — never guessed.

### Image filtering
Magento serves a default "white-placeholder" image until a real photo exists. Any URL containing `placeholder`, `white-placeholder`, or `/placeholder/default/` is **filtered** from `images`, `image_urls_str`, and `variant_image`. Fallback order is variant-image → parent-image → null.

### Text cleanup
Every exported string passes through a shared `clean_export_text` helper that decodes HTML entities (loops to handle double-encoding like `&amp;nbsp;`), strips inline tags, and collapses whitespace. Real symbols (`®`, `™`) survive unchanged.

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
safco crawl                 Run full agent pipeline on all configured seeds
safco crawl -s gloves       Single seed
safco discover              Print URL frontier without crawling
safco report                Tail the latest data-quality report
safco stats                 Quick brand/count table from the live DB
safco schema-dump           Write JSON Schema to output/schema.json

# Seed management — edit config/crawler.yaml without leaving the terminal
safco seeds                 List currently configured seeds
safco add <url> [--label]   Add a new category URL to the seed list
safco remove <id> [--yes]   Remove a seed by its id
```

Use `--config path/to/crawler.yaml` on any command to override the default config.

### Adding a new category — example workflow

The CLI lets you add a Safco catalog URL as a new seed without editing YAML by hand. The seed `id` is auto-derived from the URL slug, and the human-readable `label` from a title-cased version (override with `--label` if you want).

```bash
# 1. Add a new category
safco add https://www.safcodental.com/catalog/infection-control
# → Added seed: id=infection-control, label=Infection Control
#   Suggests: safco discover --seed infection-control

# 2. Preview the product frontier without committing to a full crawl
safco discover --seed infection-control
# → infection-control: 87 products
#     https://www.safcodental.com/product/...

# 3. Crawl just that seed (existing data in the DB is preserved)
safco crawl --seed infection-control

# 4. Inspect what landed
safco seeds                                  # confirm new seed shows in the table
sqlite3 data/products.db "SELECT manufacturer_name, COUNT(*) FROM variants GROUP BY 1 ORDER BY 2 DESC;"
ls output/                                   # products_infection_control.csv now exists
```

**A note on output behavior:**
- `safco crawl` always rewrites `output/*.csv` and `output/*.jsonl` from the SQLite DB. The DB is the source of truth — everything you've ever crawled stays in it (idempotent upserts) until you delete `data/products.db`.
- Per-seed CSVs (`products_<slug>.csv`) are emitted automatically for every seed in `crawler.yaml`, even if you only crawled some of them this run.
- To start clean, `rm data/products.db` before the next crawl.

### Removing a seed

```bash
safco remove infection-control               # prompts to confirm
safco remove infection-control --yes         # script-friendly, no prompt
```

`safco remove` only edits `crawler.yaml` — it doesn't delete crawled rows. To purge an already-crawled category from the database too:

```bash
sqlite3 data/products.db "DELETE FROM products WHERE source_seed='infection-control';"
# Variants cascade-delete via foreign key.
safco crawl                                  # regenerate exports
```

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
48 passed in 1.29s
tests/test_classifier.py   — URL + DOM classification heuristics
tests/test_extractor.py    — JSON-LD + masterData extraction, alasta-pro regression
tests/test_exporters.py    — variant-grain CSV, per-seed split, JSONL shape, spec parsing
tests/test_storage.py      — upsert idempotency, FK cascade, variant replacement
tests/test_validator.py    — Pydantic acceptance, SKU/URL/variant dedup
```

---

## Sample Run (committed)

Captured 2026-05-07 — both assignment seeds (gloves + sutures-surgical-products), 50-product cap per seed. **97 parent products → 463 purchasable variants.**

| Metric | Value |
|---|---|
| Pages visited | 98 |
| Parent products extracted | 97 |
| Variants extracted | 463 |
| Product duplicates skipped | 1 |
| Variant duplicates skipped | 0 |
| Failed pages | 0 |
| LLM fallback calls | 0 |
| Latency p50 | 1172 ms |
| Latency p95 | 1864 ms |
| Extraction method | JSON-LD + masterData (100% deterministic) |
| Pack-size source | description-heuristic 98%, specs-heuristic 2% |

The committed sample lives in `data/samples/` and mirrors what a live crawl emits to `output/`:

```
data/samples/                         ← committed reference dataset
├── products_all.csv                  flat, one row per variant (463 rows)
├── products_gloves.csv               variant rows, gloves only       (188 rows)
├── products_sutures_surgical.csv     variant rows, sutures only      (275 rows)
├── specifications.jsonl              one parent per line             (97 lines)
├── products_grouped.json             pretty-printed JSON array       (97 parents)
├── run-report.md                     human-readable quality report
└── run-report.json                   machine-readable version

output/                               ← live runtime (gitignored)
├── products_all.csv                  same files as samples, regenerated each crawl
├── products_gloves.csv
├── products_sutures_surgical.csv
├── specifications.jsonl
└── products_grouped.json

data/
├── products.db                       SQLite source of truth (gitignored)
└── reports/run-{ts}.{md,json}     per-run quality report
```

### Sample SQL queries

```sql
-- Variants by manufacturer brand (the actual mfr, not the retailer)
SELECT COALESCE(manufacturer_name,'<unknown>') AS brand, COUNT(*) AS n
FROM variants
GROUP BY brand ORDER BY n DESC LIMIT 10;

-- Variants per parent product (catalog depth check)
SELECT p.name AS parent, COUNT(v.dedup_key) AS variant_count
FROM products p JOIN variants v ON v.parent_dedup_key = p.dedup_key
GROUP BY p.name ORDER BY variant_count DESC LIMIT 10;

-- Per-variant availability mix
SELECT availability, COUNT(*) FROM variants GROUP BY availability;

-- Pack size distribution for gloves
SELECT pack_quantity, pack_unit, COUNT(*) AS n
FROM variants v JOIN products p ON p.dedup_key = v.parent_dedup_key
WHERE p.source_seed = 'gloves'
GROUP BY pack_quantity, pack_unit ORDER BY n DESC;

-- Recent run history
SELECT run_id, started_at, finished_at, products_extracted, failures
FROM run_log ORDER BY started_at DESC LIMIT 5;
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
- **Pack parsing is best-effort** — `pack_quantity` / `pack_unit` are parsed from variant descriptions like `"X-small, 200/box"` using a strict regex. Non-matching phrasings fall back to a heuristic search; some sutures items genuinely don't expose a pack size on the page.
- **Currency is intentionally blank** — Safco's variant data exposes a numeric price but no currency code, so we never default to `"USD"`. Parent-level JSON-LD does carry `priceCurrency`; that's reflected on the parent record but not propagated into variant rows.
- **Alternative products are not currently extracted** — Safco doesn't expose related-product links in a structured way for these categories. The field is preserved in the JSONL output (`alternative_products: []`) and surfaces in `extraction_quality.missing_fields` so the gap is auditable.
- **Placeholder images are filtered** — Magento's default "white-placeholder" / `/placeholder/default/` URLs are silently dropped from every export; only real product imagery makes it through.
- **Some surgical specs live only in raw text** — for surgical/suture products that use long-form prose (bone graft materials, hemostatic agents, etc.), dimension and pack info is preserved in the `description` but may not be normalized into the `specifications` object.
- **Spec parsing is rule-based, not exhaustive** — the deterministic rules in `spec_parser.py` cover the most common attributes for gloves and sutures. Attributes that don't match the rules are omitted (never guessed). The active rule set is logged per-record in `extraction_quality.spec_source`.
- **LLM fallback** — requires `ANTHROPIC_API_KEY` and `llm.enabled: true` in config; the default deterministic pipeline handled 100% of the sample run without it.

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

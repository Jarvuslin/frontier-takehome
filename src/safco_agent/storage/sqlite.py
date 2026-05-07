"""SQLite persistence: products + crawl_state + run_log.

Idempotent upserts keyed by `dedup_key`. Re-running the same input updates rows
in place rather than producing duplicates. Child tables are rewritten per upsert
to keep the join model simple — for prototype scale this is fine.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from safco_agent.schema import Product, Variant

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    dedup_key      TEXT PRIMARY KEY,
    sku            TEXT,
    product_code   TEXT,
    name           TEXT NOT NULL,
    brand          TEXT,
    product_url    TEXT NOT NULL,
    price          REAL,
    price_text     TEXT,
    currency       TEXT,
    pack_size      TEXT,
    availability   TEXT,
    description    TEXT,
    category_path  TEXT,    -- JSON array
    source_seed    TEXT,
    extracted_at   TEXT,
    crawl_run_id   TEXT,
    extraction_method TEXT  -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_seed ON products(source_seed);

CREATE TABLE IF NOT EXISTS product_specifications (
    dedup_key TEXT NOT NULL,
    name      TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (dedup_key, name),
    FOREIGN KEY (dedup_key) REFERENCES products(dedup_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_images (
    dedup_key TEXT NOT NULL,
    position  INTEGER NOT NULL,
    url       TEXT NOT NULL,
    PRIMARY KEY (dedup_key, position),
    FOREIGN KEY (dedup_key) REFERENCES products(dedup_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS product_alternatives (
    dedup_key      TEXT NOT NULL,
    alternate_url  TEXT NOT NULL,
    PRIMARY KEY (dedup_key, alternate_url),
    FOREIGN KEY (dedup_key) REFERENCES products(dedup_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crawl_state (
    url            TEXT PRIMARY KEY,
    seed_id        TEXT,
    page_type      TEXT,
    status         TEXT NOT NULL,    -- pending | done | failed | skipped
    attempts       INTEGER DEFAULT 0,
    last_attempt   TEXT,
    error_class    TEXT,
    error_message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_state_status ON crawl_state(status);

CREATE TABLE IF NOT EXISTS run_log (
    run_id         TEXT PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    seeds          TEXT,           -- JSON array of seed ids
    pages_visited  INTEGER DEFAULT 0,
    products_extracted INTEGER DEFAULT 0,
    failures       INTEGER DEFAULT 0,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS variants (
    dedup_key                TEXT PRIMARY KEY,
    parent_dedup_key         TEXT NOT NULL,
    parent_sku               TEXT,
    safco_item_number        TEXT,
    manufacturer_number      TEXT,
    manufacturer_name        TEXT,    -- variant-level brand
    name                     TEXT,
    description              TEXT,
    price                    REAL,
    price_text               TEXT,
    currency                 TEXT,
    availability             TEXT,
    availability_label       TEXT,
    size                     TEXT,
    pack_quantity            INTEGER,
    pack_unit                TEXT,
    image                    TEXT,
    main_image               TEXT,
    is_synthetic             INTEGER DEFAULT 0,
    extraction_method        TEXT,    -- JSON object
    FOREIGN KEY (parent_dedup_key) REFERENCES products(dedup_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_variants_parent ON variants(parent_dedup_key);
CREATE INDEX IF NOT EXISTS idx_variants_brand  ON variants(manufacturer_name);
CREATE INDEX IF NOT EXISTS idx_variants_item   ON variants(safco_item_number);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- products ----------
    def upsert_product(self, p: Product) -> None:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO products (
                    dedup_key, sku, product_code, name, brand, product_url,
                    price, price_text, currency, pack_size, availability,
                    description, category_path, source_seed, extracted_at,
                    crawl_run_id, extraction_method
                ) VALUES (
                    :dedup_key, :sku, :product_code, :name, :brand, :product_url,
                    :price, :price_text, :currency, :pack_size, :availability,
                    :description, :category_path, :source_seed, :extracted_at,
                    :crawl_run_id, :extraction_method
                )
                ON CONFLICT(dedup_key) DO UPDATE SET
                    sku=excluded.sku, product_code=excluded.product_code,
                    name=excluded.name, brand=excluded.brand,
                    product_url=excluded.product_url, price=excluded.price,
                    price_text=excluded.price_text, currency=excluded.currency,
                    pack_size=excluded.pack_size, availability=excluded.availability,
                    description=excluded.description, category_path=excluded.category_path,
                    source_seed=excluded.source_seed, extracted_at=excluded.extracted_at,
                    crawl_run_id=excluded.crawl_run_id,
                    extraction_method=excluded.extraction_method
                """,
                {
                    "dedup_key": p.dedup_key,
                    "sku": p.sku,
                    "product_code": p.product_code,
                    "name": p.name,
                    "brand": p.brand,
                    "product_url": p.product_url,
                    "price": float(p.price) if p.price is not None else None,
                    "price_text": p.price_text,
                    "currency": p.currency,
                    "pack_size": p.pack_size,
                    "availability": p.availability,
                    "description": p.description,
                    "category_path": json.dumps(p.category_path),
                    "source_seed": p.source_seed,
                    "extracted_at": p.extracted_at.isoformat(),
                    "crawl_run_id": p.crawl_run_id,
                    "extraction_method": json.dumps(p.extraction_method),
                },
            )
            c.execute("DELETE FROM product_specifications WHERE dedup_key = ?", (p.dedup_key,))
            c.executemany(
                "INSERT INTO product_specifications (dedup_key, name, value) VALUES (?, ?, ?)",
                [(p.dedup_key, k, v) for k, v in p.specifications.items()],
            )
            c.execute("DELETE FROM product_images WHERE dedup_key = ?", (p.dedup_key,))
            c.executemany(
                "INSERT INTO product_images (dedup_key, position, url) VALUES (?, ?, ?)",
                [(p.dedup_key, i, u) for i, u in enumerate(p.image_urls)],
            )
            c.execute("DELETE FROM product_alternatives WHERE dedup_key = ?", (p.dedup_key,))
            c.executemany(
                "INSERT INTO product_alternatives (dedup_key, alternate_url) VALUES (?, ?)",
                [(p.dedup_key, u) for u in p.alternative_product_urls],
            )

    # ---------- variants ----------
    def upsert_variants(self, parent_key: str, variants: list[Variant]) -> None:
        """Replace all variants for a parent product.

        Delete-then-insert (rather than ON CONFLICT) keeps the variant set in
        lockstep with what masterData currently exposes — variants the page no
        longer offers are removed, never staled.
        """
        with self.tx() as c:
            c.execute("DELETE FROM variants WHERE parent_dedup_key = ?", (parent_key,))
            if not variants:
                return
            c.executemany(
                """
                INSERT INTO variants (
                    dedup_key, parent_dedup_key, parent_sku,
                    safco_item_number, manufacturer_number, manufacturer_name,
                    name, description, price, price_text, currency,
                    availability, availability_label,
                    size, pack_quantity, pack_unit,
                    image, main_image, is_synthetic, extraction_method
                ) VALUES (
                    :dedup_key, :parent_dedup_key, :parent_sku,
                    :safco_item_number, :manufacturer_number, :manufacturer_name,
                    :name, :description, :price, :price_text, :currency,
                    :availability, :availability_label,
                    :size, :pack_quantity, :pack_unit,
                    :image, :main_image, :is_synthetic, :extraction_method
                )
                """,
                [
                    {
                        "dedup_key": v.dedup_key,
                        "parent_dedup_key": v.parent_dedup_key,
                        "parent_sku": v.parent_sku,
                        "safco_item_number": v.safco_item_number,
                        "manufacturer_number": v.manufacturer_number,
                        "manufacturer_name": v.manufacturer_name,
                        "name": v.name,
                        "description": v.description,
                        "price": float(v.price) if v.price is not None else None,
                        "price_text": v.price_text,
                        "currency": v.currency,
                        "availability": v.availability,
                        "availability_label": v.availability_label,
                        "size": v.size,
                        "pack_quantity": v.pack_quantity,
                        "pack_unit": v.pack_unit,
                        "image": v.image,
                        "main_image": v.main_image,
                        "is_synthetic": 1 if v.is_synthetic else 0,
                        "extraction_method": json.dumps(v.extraction_method),
                    }
                    for v in variants
                ],
            )

    def all_variants_with_parent(self) -> list[sqlite3.Row]:
        """One row per variant, joined with the parent product context.

        Source of truth for the variant-level CSV export.
        """
        return list(
            self._conn.execute(
                """
                SELECT
                    v.dedup_key            AS variant_dedup_key,
                    v.parent_dedup_key     AS parent_dedup_key,
                    v.parent_sku           AS parent_sku,
                    v.safco_item_number    AS safco_item_number,
                    v.manufacturer_number  AS manufacturer_number,
                    v.manufacturer_name    AS manufacturer_name,
                    v.name                 AS variant_name,
                    v.description          AS variant_description,
                    v.price                AS price,
                    v.price_text           AS price_text,
                    v.currency             AS currency,
                    v.availability         AS availability,
                    v.availability_label   AS availability_label,
                    v.size                 AS size,
                    v.pack_quantity        AS pack_quantity,
                    v.pack_unit            AS pack_unit,
                    v.image                AS variant_image_thumb,
                    v.main_image           AS variant_image_main,
                    v.is_synthetic         AS is_synthetic,
                    p.dedup_key            AS p_dedup_key,
                    p.sku                  AS p_sku,
                    p.name                 AS parent_name,
                    p.product_url          AS product_url,
                    p.category_path        AS category_path_json,
                    p.description          AS parent_description,
                    p.source_seed          AS source_seed,
                    p.extracted_at         AS extracted_at
                FROM variants v
                JOIN products p ON p.dedup_key = v.parent_dedup_key
                ORDER BY p.name, v.size, v.safco_item_number
                """
            )
        )

    def variant_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0]

    # ---------- crawl_state ----------
    def mark_pending(self, url: str, seed_id: str, page_type: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO crawl_state (url, seed_id, page_type, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT(url) DO NOTHING
                """,
                (url, seed_id, page_type),
            )

    def mark_done(self, url: str) -> None:
        with self.tx() as c:
            c.execute(
                """
                UPDATE crawl_state
                   SET status='done',
                       attempts = attempts + 1,
                       last_attempt = datetime('now'),
                       error_class=NULL,
                       error_message=NULL
                 WHERE url = ?
                """,
                (url,),
            )

    def mark_failed(self, url: str, err_class: str, err_msg: str) -> None:
        with self.tx() as c:
            c.execute(
                """
                UPDATE crawl_state
                   SET status='failed',
                       attempts = attempts + 1,
                       last_attempt = datetime('now'),
                       error_class=?,
                       error_message=?
                 WHERE url = ?
                """,
                (err_class, err_msg, url),
            )

    def pending_urls(self, seed_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM crawl_state WHERE status IN ('pending', 'failed')"
        params: tuple = ()
        if seed_id:
            sql += " AND seed_id = ?"
            params = (seed_id,)
        return list(self._conn.execute(sql, params))

    # ---------- run_log ----------
    def start_run(self, run_id: str, seeds: list[str]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO run_log (run_id, started_at, seeds) VALUES (?, datetime('now'), ?)",
                (run_id, json.dumps(seeds)),
            )

    def finish_run(self, run_id: str, pages: int, products: int, failures: int, notes: str = "") -> None:
        with self.tx() as c:
            c.execute(
                """
                UPDATE run_log SET finished_at = datetime('now'),
                                   pages_visited = ?, products_extracted = ?,
                                   failures = ?, notes = ?
                 WHERE run_id = ?
                """,
                (pages, products, failures, notes, run_id),
            )

    # ---------- helpers ----------
    def all_products(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM products ORDER BY brand, name"))

    def product_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def brand_counts(self, limit: int = 15) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT COALESCE(brand,'<unknown>') as b, COUNT(*) c FROM products GROUP BY b ORDER BY c DESC LIMIT ?",
            (limit,),
        )
        return [(r["b"], r["c"]) for r in rows]

    def specs_for(self, dedup_key: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT name, value FROM product_specifications WHERE dedup_key=? ORDER BY name",
                (dedup_key,),
            )
        )

    def images_for(self, dedup_key: str) -> list[str]:
        return [
            r["url"]
            for r in self._conn.execute(
                "SELECT url FROM product_images WHERE dedup_key=? ORDER BY position",
                (dedup_key,),
            )
        ]

    def alternatives_for(self, dedup_key: str) -> list[str]:
        return [
            r["alternate_url"]
            for r in self._conn.execute(
                "SELECT alternate_url FROM product_alternatives WHERE dedup_key=? ORDER BY alternate_url",
                (dedup_key,),
            )
        ]

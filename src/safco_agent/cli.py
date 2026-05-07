"""CLI entrypoint."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import typer
import yaml
from rich.console import Console
from rich.table import Table

from safco_agent.agents.discovery import DiscoveryAgent
from safco_agent.http.browser import BrowserPool
from safco_agent.http.client import HTTPClient
from safco_agent.observability.logging import configure_logging
from safco_agent.orchestrator import Orchestrator
from safco_agent.settings import DEFAULT_CONFIG, Settings
from safco_agent.storage.sqlite import Store

app = typer.Typer(add_completion=False, help="Safco catalog agent")
console = Console()


# ── seed management helpers ───────────────────────────────────────────────────

def _slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _label_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def _load_config_path(config: Path | None) -> Path:
    return config if config else DEFAULT_CONFIG


def _read_seeds(config_text: str) -> list[dict]:
    return (yaml.safe_load(config_text) or {}).get("seeds", [])


def _insert_seed_text(config_text: str, seed_id: str, url: str, label: str) -> str:
    """Append a new seed entry inside the seeds: block, preserving all comments."""
    new_entry = f'  - id: {seed_id}\n    url: "{url}"\n    label: "{label}"\n'
    lines = config_text.splitlines(keepends=True)
    in_seeds = False
    last_seed_line = -1
    for i, line in enumerate(lines):
        if line.startswith("seeds:"):
            in_seeds = True
            continue
        if in_seeds:
            stripped = line.rstrip()
            # A top-level YAML key: non-indented, non-comment, contains ":"
            if stripped and not stripped[0].isspace() and not stripped.startswith("#") and ":" in stripped:
                break
            if stripped and stripped[0].isspace():
                last_seed_line = i
    if last_seed_line == -1:
        return config_text.rstrip() + "\n" + new_entry
    return "".join(lines[: last_seed_line + 1]) + new_entry + "".join(lines[last_seed_line + 1 :])


def _remove_seed_text(config_text: str, seed_id: str) -> str:
    """Remove a seed entry block from the seeds: section by id."""
    lines = config_text.splitlines(keepends=True)
    result: list[str] = []
    skipping = False
    for line in lines:
        if re.match(r"\s+-\s+id:\s+" + re.escape(seed_id) + r"\s*$", line):
            skipping = True
            continue
        if skipping:
            if line.startswith("    "):  # continuation lines of this seed entry
                continue
            skipping = False
        result.append(line)
    return "".join(result)


@app.command()
def crawl(
    seed: list[str] = typer.Option(None, "--seed", "-s", help="Seed id (omit to crawl all)"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """Run the full agent pipeline against the configured seeds."""
    settings = Settings.load(config) if config else Settings.load()
    orch = Orchestrator(settings)
    asyncio.run(orch.crawl_seeds(seed if seed else None))


@app.command()
def discover(
    seed: list[str] = typer.Option(None, "--seed", "-s"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """Run discovery only — print the product URL frontier without crawling."""
    settings = Settings.load(config) if config else Settings.load()
    configure_logging(settings.log_level, settings.log_format)
    seeds = settings.seeds
    if seed:
        seeds = [s for s in seeds if s.id in seed]

    async def _run() -> None:
        http = HTTPClient(
            user_agent=settings.site.user_agent,
            timeout_seconds=settings.timeouts.http_seconds,
            rps=settings.rate_limit.requests_per_second,
            burst=settings.rate_limit.burst,
        )
        browser = BrowserPool(
            user_agent=settings.site.user_agent,
            max_concurrent=settings.limits.max_concurrent_browser,
            timeout_seconds=settings.timeouts.browser_seconds,
            page_settle_seconds=settings.timeouts.page_settle_seconds,
        )
        try:
            await browser.start()
            agent = DiscoveryAgent(settings, http, browser)
            frontier = await agent.discover(seeds)
            for sid, urls in frontier.items():
                console.print(f"[bold green]{sid}[/]: {len(urls)} products")
                for u in urls[:10]:
                    console.print(f"  {u}")
                if len(urls) > 10:
                    console.print(f"  ... +{len(urls)-10} more")
        finally:
            await browser.close()
            await http.aclose()

    asyncio.run(_run())


@app.command()
def report(
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """List existing run reports and tail the latest."""
    settings = Settings.load(config) if config else Settings.load()
    rdir = settings.repo_path(settings.paths.reports_dir)
    md_files = sorted(rdir.glob("run-*.md"))
    if not md_files:
        console.print("[yellow]No reports yet. Run `safco crawl` first.[/]")
        raise typer.Exit(0)
    latest = md_files[-1]
    console.print(f"[bold]Latest report:[/] {latest}")
    console.print()
    console.print(latest.read_text(encoding="utf-8"))


@app.command()
def stats(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """Quick DB stats (brand counts, totals)."""
    settings = Settings.load(config) if config else Settings.load()
    db = settings.repo_path(settings.paths.sqlite)
    if not db.exists():
        console.print("[yellow]DB not found — run `safco crawl` first.[/]")
        raise typer.Exit(0)
    store = Store(db)
    n = store.product_count()
    console.print(f"[bold]Products in DB:[/] {n}")
    table = Table("Brand", "Count")
    for brand, count in store.brand_counts():
        table.add_row(brand, str(count))
    console.print(table)
    store.close()


@app.command()
def schema_dump(out: Path = typer.Option(Path("data/exports/schema.json"))) -> None:
    """Dump the JSON Schema for the Product record."""
    from safco_agent.schema import Product
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Product.model_json_schema(), indent=2), encoding="utf-8")
    console.print(f"Wrote {out}")


@app.command()
def seeds(config: Path = typer.Option(None, "--config", "-c")) -> None:
    """List all seeds currently configured in crawler.yaml."""
    cfg_path = _load_config_path(config)
    current = _read_seeds(cfg_path.read_text(encoding="utf-8"))
    if not current:
        console.print("[yellow]No seeds configured.[/]")
        raise typer.Exit(0)
    table = Table("ID", "URL", "Label", title=f"Seeds in {cfg_path.name}")
    for s in current:
        table.add_row(s.get("id", ""), s.get("url", ""), s.get("label", ""))
    console.print(table)


@app.command()
def add(
    url: str = typer.Argument(..., help="Category URL to add, e.g. https://www.safcodental.com/catalog/anesthetics"),
    label: str = typer.Option(None, "--label", "-l", help="Human-readable label (auto-derived from URL slug if omitted)"),
    config: Path = typer.Option(None, "--config", "-c"),
) -> None:
    """Add a category URL to the crawl seed list in crawler.yaml."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        console.print(f"[red]Invalid URL:[/] {url}")
        raise typer.Exit(1)

    seed_id = _slug_from_url(url)
    if not seed_id:
        console.print("[red]Could not derive a seed id from that URL. Check the path.[/]")
        raise typer.Exit(1)

    seed_label = label or _label_from_slug(seed_id)
    cfg_path = _load_config_path(config)
    config_text = cfg_path.read_text(encoding="utf-8")
    current = _read_seeds(config_text)

    if any(s.get("id") == seed_id for s in current):
        console.print(f"[yellow]Seed '{seed_id}' already exists — no change made.[/]")
        raise typer.Exit(0)

    updated = _insert_seed_text(config_text, seed_id, url, seed_label)
    cfg_path.write_text(updated, encoding="utf-8")

    console.print(f"[green]Added seed:[/]")
    console.print(f"  id    : {seed_id}")
    console.print(f"  url   : {url}")
    console.print(f"  label : {seed_label}")
    console.print()
    console.print(f"Run [bold]safco discover --seed {seed_id}[/] to preview the product frontier.")
    console.print(f"Run [bold]safco crawl --seed {seed_id}[/] to extract and store products.")


@app.command()
def remove(
    seed_id: str = typer.Argument(..., help="Seed id to remove (use `safco seeds` to list ids)"),
    config: Path = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove a seed from crawler.yaml by its id."""
    cfg_path = _load_config_path(config)
    config_text = cfg_path.read_text(encoding="utf-8")
    current = _read_seeds(config_text)

    match = next((s for s in current if s.get("id") == seed_id), None)
    if not match:
        console.print(f"[yellow]Seed '{seed_id}' not found.[/]")
        raise typer.Exit(1)

    if not yes:
        confirmed = typer.confirm(f"Remove seed '{seed_id}' ({match.get('url', '')})?")
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit(0)

    updated = _remove_seed_text(config_text, seed_id)
    cfg_path.write_text(updated, encoding="utf-8")
    console.print(f"[green]Removed seed '{seed_id}'.[/]")


if __name__ == "__main__":
    app()

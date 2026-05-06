"""CLI entrypoint."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from safco_agent.agents.discovery import DiscoveryAgent
from safco_agent.http.browser import BrowserPool
from safco_agent.http.client import HTTPClient
from safco_agent.observability.logging import configure_logging
from safco_agent.orchestrator import Orchestrator
from safco_agent.settings import Settings
from safco_agent.storage.sqlite import Store

app = typer.Typer(add_completion=False, help="Safco catalog agent")
console = Console()


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
    for row in store._conn.execute(
        "SELECT COALESCE(brand,'<unknown>') as b, COUNT(*) c FROM products GROUP BY b ORDER BY c DESC LIMIT 15"
    ):
        table.add_row(row["b"], str(row["c"]))
    console.print(table)
    store.close()


@app.command()
def schema_dump(out: Path = typer.Option(Path("data/exports/schema.json"))) -> None:
    """Dump the JSON Schema for the Product record."""
    from safco_agent.schema import Product
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Product.model_json_schema(), indent=2), encoding="utf-8")
    console.print(f"Wrote {out}")


if __name__ == "__main__":
    app()

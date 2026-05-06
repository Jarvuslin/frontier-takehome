"""Playwright wrapper used ONLY for client-rendered category listing pages.

Product detail pages are server-rendered and fetched via the HTTP client —
launching a browser per product would dominate runtime and be wasteful.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from safco_agent.observability.logging import get_logger

log = get_logger("http.browser")


# Non-destructive popup/modal dismissals. We never click anything that changes
# state (no logins, no add-to-cart, no checkout). Order matters; first match wins.
POPUP_SELECTORS: tuple[str, ...] = (
    "button[aria-label*='close' i]",
    "button[aria-label*='dismiss' i]",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
    "button:has-text('No thanks')",
    ".modal .close",
)


@dataclass
class RenderResult:
    url: str
    html: str
    final_url: str
    status: int


class BrowserPool:
    """Simple bounded browser pool. One browser, many pages, max_concurrent guard."""

    def __init__(
        self,
        user_agent: str,
        max_concurrent: int = 2,
        timeout_seconds: int = 30,
        page_settle_seconds: int = 4,
    ) -> None:
        self.user_agent = user_agent
        self._sem = asyncio.Semaphore(max_concurrent)
        self.timeout_ms = timeout_seconds * 1000
        self.page_settle_ms = page_settle_seconds * 1000
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=False,
        )
        # Block heavy resources to speed up listing renders. The product grid is HTML/JSON.
        await self._context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,webm}",
            lambda route: route.abort(),
        )

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        if self._context is None:
            await self.start()
        assert self._context is not None
        async with self._sem:
            page = await self._context.new_page()
            try:
                yield page
            finally:
                await page.close()

    async def render(self, url: str, screenshot_path: Path | None = None) -> RenderResult:
        async with self.page() as page:
            log.info("browser.goto", url=url)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            status = resp.status if resp else 0
            await self._dismiss_popups(page)
            # Wait for product cards or known content to settle. We don't fail if
            # no cards appear — caller will inspect DOM and decide.
            try:
                await page.wait_for_load_state("networkidle", timeout=self.page_settle_ms)
            except Exception:
                pass
            # Best-effort: scroll once to trigger lazy-load infinite scrollers.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(min(self.page_settle_ms, 2000))
            html = await page.content()
            if screenshot_path is not None:
                try:
                    await page.screenshot(path=str(screenshot_path), full_page=False)
                except Exception as e:  # screenshot is best-effort
                    log.warning("browser.screenshot_failed", error=str(e))
            return RenderResult(url=url, html=html, final_url=page.url, status=status)

    async def _dismiss_popups(self, page: Page) -> None:
        for sel in POPUP_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=200):
                    await loc.click(timeout=500)
                    log.debug("browser.popup_dismissed", selector=sel)
                    await page.wait_for_timeout(150)
                    return
            except Exception:
                continue
        # Fallback: press Escape twice (closes most modals)
        try:
            await page.keyboard.press("Escape")
            await page.keyboard.press("Escape")
        except Exception:
            pass

"""Async HTTP client wrapper with token-bucket rate limit, classified retries.

The retry policy mirrors what an SRE would expect:
- Network/timeout      -> exponential backoff up to N attempts
- 5xx                  -> exponential backoff
- 429                  -> respect Retry-After header
- 4xx (other)          -> no retry; surfaced for the navigator to skip
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from safco_agent.observability.logging import get_logger

log = get_logger("http.client")


class RetryableError(Exception):
    """Errors the navigator should consider transient."""


class FatalHTTPError(Exception):
    """Permanent errors (4xx other than 429); navigator should mark page failed."""

    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableError, httpx.TimeoutException, httpx.TransportError))


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    headers: httpx.Headers
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HTTPClient:
    """Async HTTP client with per-host rate limiting and classified retries."""

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: int = 20,
        rps: float = 1.0,
        burst: int = 2,
        max_attempts: int = 3,
        max_concurrent: int = 4,
    ) -> None:
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"},
        )
        # aiolimiter: `burst` permits per (1/rps * burst) seconds.
        self._limiter = AsyncLimiter(max_rate=burst, time_period=burst / max(rps, 0.01))
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_attempts = max_attempts

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        async def _attempt() -> FetchResult:
            async with self._limiter, self._semaphore:
                t0 = asyncio.get_running_loop().time()
                resp = await self._client.get(url, **kwargs)
                elapsed = int((asyncio.get_running_loop().time() - t0) * 1000)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After") or 5)
                    log.warning("http.429", url=url, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    raise RetryableError(f"429 {url}")
                if 500 <= resp.status_code < 600:
                    raise RetryableError(f"{resp.status_code} {url}")
                if 400 <= resp.status_code < 500:
                    raise FatalHTTPError(resp.status_code, url)
                return FetchResult(
                    url=str(resp.url),
                    status=resp.status_code,
                    text=resp.text,
                    headers=resp.headers,
                    elapsed_ms=elapsed,
                )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential(multiplier=1, min=1, max=20) + wait_random(0, 1),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    return await _attempt()
        except FatalHTTPError:
            raise
        except RetryError as e:
            raise RetryableError(str(e)) from e
        # Unreachable but keeps type checkers happy
        raise RuntimeError("retry loop exited without result")

"""NavigatorAgent — robots.txt enforcement + frontier admission control.

The token-bucket pacing lives in the HTTPClient itself; the navigator owns
the *policy* of which URLs we are allowed to visit at all.
"""
from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from safco_agent.http.client import HTTPClient
from safco_agent.observability.logging import get_logger

log = get_logger("agent.navigator")


class NavigatorAgent:
    def __init__(self, base_url: str, user_agent: str, http: HTTPClient):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.http = http
        self._robots: RobotFileParser | None = None

    async def load_robots(self) -> None:
        url = f"{self.base_url}/robots.txt"
        try:
            r = await self.http.fetch(url)
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            self._robots = rp
            log.info("navigator.robots_loaded", lines=len(r.text.splitlines()))
        except Exception as e:
            log.warning("navigator.robots_failed", error=str(e))
            self._robots = None

    def allowed(self, url: str) -> bool:
        """Return True iff robots.txt permits this URL for our user agent."""
        if self._robots is None:
            return True
        p = urlparse(url)
        # Skip out-of-host URLs entirely.
        if p.netloc and not self.base_url.endswith(p.netloc):
            return False
        try:
            return self._robots.can_fetch(self.user_agent, url) or self._robots.can_fetch("*", url)
        except Exception:
            return True

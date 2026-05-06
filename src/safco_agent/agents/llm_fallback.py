"""LLM fallback: Claude Haiku via tool-use.

This is invoked ONLY when the deterministic extractor failed to recover
critical fields AND the operator explicitly enabled it. Cost cap is enforced
per run via `max_calls_per_run`. The model is asked to return a strict JSON
object via a tool schema; we never trust free-form text output.
"""
from __future__ import annotations

import json
import re
from typing import Any

from selectolax.parser import HTMLParser

from safco_agent.observability.logging import get_logger
from safco_agent.settings import LLMFallback

log = get_logger("agent.llm_fallback")

EXTRACT_TOOL = {
    "name": "record_product_fields",
    "description": "Record the structured product fields you extracted from the page snippet.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "brand": {"type": ["string", "null"]},
            "sku": {"type": ["string", "null"]},
            "price": {"type": ["string", "null"], "description": "raw price text e.g. '$15.99' or 'From $20.00'"},
            "availability": {"type": ["string", "null"], "description": "in_stock | out_of_stock | unknown"},
            "description": {"type": ["string", "null"]},
            "pack_size": {"type": ["string", "null"]},
            "specifications": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "key-value attributes (size, material, color, etc.)",
            },
            "category_path": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    },
}

SYSTEM_PROMPT = (
    "You are a careful information extractor. Given a snippet of HTML from a "
    "dental supply product page, extract the requested fields verbatim from the "
    "page text. If a field is not present, return null. Do not invent values. "
    "Always reply via the record_product_fields tool — never plain text."
)


def _strip_html(html: str, max_chars: int = 12_000) -> str:
    """Reduce the HTML to a tractable signal-rich snippet for the LLM."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "noscript", "svg", "header", "footer", "nav"):
        for n in tree.css(sel):
            n.decompose()
    main = tree.css_first("main") or tree.css_first("[role=main]") or tree.body
    if main is None:
        return html[:max_chars]
    text = main.html or ""
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


class LLMFallbackAgent:
    def __init__(self, cfg: LLMFallback):
        self.cfg = cfg
        self.calls_made = 0
        self._client: Any = None
        if cfg.enabled and cfg.api_key:
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=cfg.api_key)
            except Exception as e:  # SDK not installed or import failure
                log.warning("llm.disabled", reason=str(e))

    @property
    def available(self) -> bool:
        return self.cfg.enabled and self._client is not None and self.calls_made < self.cfg.max_calls_per_run

    def extract(self, url: str, html: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        snippet = _strip_html(html)
        try:
            resp = self._client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[EXTRACT_TOOL],
                tool_choice={"type": "tool", "name": "record_product_fields"},
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Page URL: {url}\n\n"
                            f"HTML snippet:\n{snippet}\n\n"
                            "Extract the product fields and call record_product_fields."
                        ),
                    }
                ],
            )
        except Exception as e:
            log.warning("llm.call_failed", error=str(e), url=url)
            return None
        self.calls_made += 1
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_product_fields":
                return block.input  # already a dict
        return None

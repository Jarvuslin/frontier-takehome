"""Per-failure debug bundle: html.gz + error.json + (optional) screenshot."""
from __future__ import annotations

import gzip
import hashlib
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _hash(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def save(
    debug_dir: Path,
    url: str,
    error: BaseException,
    *,
    html: str | None = None,
    page_type: str | None = None,
    attempts: int = 1,
    extra: dict | None = None,
) -> Path:
    """Write a debug bundle and return its directory."""
    bundle = debug_dir / _hash(url)
    bundle.mkdir(parents=True, exist_ok=True)
    if html:
        with gzip.open(bundle / "page.html.gz", "wt", encoding="utf-8") as f:
            f.write(html)
    err_payload = {
        "url": url,
        "page_type": page_type,
        "attempts": attempts,
        "error_class": type(error).__name__,
        "error_message": str(error),
        "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "extra": extra or {},
    }
    (bundle / "error.json").write_text(json.dumps(err_payload, indent=2), encoding="utf-8")
    return bundle

"""Data-quality run report (Markdown + JSON sidecar).

Aggregates per-run stats: counts, failure breakdown by error class,
missing-field rates, extraction-method distribution, latency p50/p95.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunStats:
    run_id: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    seeds: list[str] = field(default_factory=list)

    pages_visited: int = 0
    products_extracted: int = 0
    products_rejected: int = 0
    duplicates: int = 0
    failures: int = 0

    failures_by_class: dict[str, int] = field(default_factory=dict)
    extraction_methods: dict[str, Counter] = field(default_factory=dict)  # field -> Counter
    missing_fields: Counter = field(default_factory=Counter)
    latencies_ms: list[int] = field(default_factory=list)
    llm_calls: int = 0

    def record_extraction(self, method_map: dict[str, str], missing: list[str]) -> None:
        for field_name, method in method_map.items():
            self.extraction_methods.setdefault(field_name, Counter())[method] += 1
        for f in missing:
            self.missing_fields[f] += 1

    def record_failure(self, error_class: str) -> None:
        self.failures += 1
        self.failures_by_class[error_class] = self.failures_by_class.get(error_class, 0) + 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["extraction_methods"] = {k: dict(v) for k, v in self.extraction_methods.items()}
        d["missing_fields"] = dict(self.missing_fields)
        d["latency_ms_p50"] = int(statistics.median(self.latencies_ms)) if self.latencies_ms else None
        d["latency_ms_p95"] = (
            int(statistics.quantiles(self.latencies_ms, n=20)[-1])
            if len(self.latencies_ms) >= 20
            else (max(self.latencies_ms) if self.latencies_ms else None)
        )
        return d

    def render_markdown(self) -> str:
        d = self.to_dict()
        lines = [
            f"# Crawl Run Report — `{self.run_id}`",
            "",
            f"- **Started:**  {self.started_at}",
            f"- **Finished:** {self.finished_at or 'n/a'}",
            f"- **Seeds:**    {', '.join(self.seeds) or 'n/a'}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Pages visited | {self.pages_visited} |",
            f"| Products extracted (accepted) | {self.products_extracted} |",
            f"| Products rejected (validator) | {self.products_rejected} |",
            f"| Duplicates skipped | {self.duplicates} |",
            f"| Failed pages | {self.failures} |",
            f"| LLM fallback calls | {self.llm_calls} |",
            f"| Latency p50 (ms) | {d['latency_ms_p50']} |",
            f"| Latency p95 (ms) | {d['latency_ms_p95']} |",
            "",
            "## Failures by error class",
            "",
        ]
        if not self.failures_by_class:
            lines.append("_None._")
        else:
            for k, v in sorted(self.failures_by_class.items(), key=lambda x: -x[1]):
                lines.append(f"- `{k}`: {v}")
        lines += ["", "## Missing-field rate (per accepted product)", ""]
        if not self.missing_fields:
            lines.append("_All tracked fields populated._")
        else:
            denom = max(self.products_extracted, 1)
            for k, v in self.missing_fields.most_common():
                pct = 100 * v / denom
                lines.append(f"- `{k}`: {v}/{denom}  ({pct:.1f}%)")
        lines += ["", "## Extraction-method distribution", ""]
        for fname, counter in self.extraction_methods.items():
            total = sum(counter.values())
            chunks = ", ".join(f"{m}={c} ({100*c/total:.0f}%)" for m, c in counter.most_common())
            lines.append(f"- **{fname}**: {chunks}")
        return "\n".join(lines) + "\n"


def write_report(stats: RunStats, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"run-{stats.run_id}.md"
    js = reports_dir / f"run-{stats.run_id}.json"
    md.write_text(stats.render_markdown(), encoding="utf-8")
    js.write_text(json.dumps(stats.to_dict(), indent=2, default=str), encoding="utf-8")
    return md, js

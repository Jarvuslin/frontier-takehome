"""Deterministic specification parser — best-effort regex over descriptions.

The Safco product pages don't expose a clean attributes table for most items;
the descriptions are bullet-point prose. We pull out the most useful structured
attributes (material, thickness, sterility, suture size, dimensions, etc.) so
that the JSONL export carries something a downstream catalog can index, while
leaving the original description intact.

Design rules:
- Never hallucinate. If a value isn't clearly present, omit it (or set null).
- Cheap and explicit — one regex per attribute, well-commented.
- No LLM. The deterministic pipeline must work offline.
- Two flavors: glove-style and surgical/suture-style. We auto-detect based on
  the parent's category_path; both extractors are always allowed to run on the
  same text since they don't conflict.

Returns: (specs: dict, telemetry: dict) where telemetry carries source and
which fields ran but came up empty.
"""
from __future__ import annotations

import re
from typing import Any

# ── Glove attributes ────────────────────────────────────────────────────────

_MATERIAL_GLOVE_RE = re.compile(r"\b(nitrile|latex|vinyl|chloroprene|neoprene|polyisoprene)\b", re.I)
_COLOR_RE = re.compile(
    r"\b(blue|black|white|green|natural|purple|pink|lavender|aqua|teal|grey|gray|"
    r"violet|orange|yellow|red|cobalt|cyan|tan|beige)\s+(?:color|gloves?)?\b",
    re.I,
)
_THICKNESS_PALM_RE = re.compile(
    r"(?:at\s+)?palm[^\d]{0,15}(\d+(?:\.\d+)?)\s*mils?", re.I
)
_THICKNESS_FINGER_RE = re.compile(
    r"(?:at\s+)?finger(?:tips?)?[^\d]{0,15}(\d+(?:\.\d+)?)\s*mils?", re.I
)
_THICKNESS_GENERIC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mils?\b", re.I)
_CASE_QTY_RE = re.compile(
    r"order\s+(\d+)\s+boxes?\s+to\s+purchase\s+a\s+case", re.I
)
_CUFF_RE = re.compile(r"\b(beaded|rolled|elastic|reinforced)\s+cuff\b", re.I)
_TEXTURE_RE = re.compile(
    r"(textured\s+(?:fingertips?|whole\s+hand|finish)|smooth\s+finish|micro-?textured)", re.I
)


def _flag(text: str, present_pat: str, absent_pat: str | None = None) -> bool | None:
    """Three-state boolean: True if positive matches, False if negation matches, None otherwise."""
    if re.search(present_pat, text, re.I):
        return True
    if absent_pat and re.search(absent_pat, text, re.I):
        return False
    return None


def _parse_glove_specs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}

    if (m := _MATERIAL_GLOVE_RE.search(text)):
        out["material"] = m.group(1).lower()

    powder = _flag(text, r"powder[-\s]?free", r"\bpowdered\b")
    if powder is not None:
        out["powder_free"] = powder

    latex_free = _flag(text, r"\blatex[-\s]?free\b")
    if latex_free is not None:
        out["latex_free"] = latex_free

    sterile = _flag(text, r"\bsterile\b", r"\bnon[-\s]?sterile\b")
    if sterile is not None:
        out["sterile"] = sterile

    chlor = _flag(text, r"\bchlorinated\b", r"\bnon[-\s]?chlorinated\b")
    if chlor is not None:
        out["chlorinated"] = chlor

    ambidextrous = _flag(text, r"\bambidextrous\b")
    if ambidextrous is not None:
        out["ambidextrous"] = ambidextrous

    if (m := _COLOR_RE.search(text)):
        out["color"] = m.group(1).lower()
    if (m := _CUFF_RE.search(text)):
        out["cuff"] = m.group(0).lower()
    if (m := _TEXTURE_RE.search(text)):
        out["texture"] = m.group(1).lower()

    if (m := _THICKNESS_PALM_RE.search(text)):
        out["thickness_palm_mils"] = float(m.group(1))
    if (m := _THICKNESS_FINGER_RE.search(text)):
        out["thickness_fingertip_mils"] = float(m.group(1))
    if "thickness_palm_mils" not in out and "thickness_fingertip_mils" not in out:
        # one global thickness rather than two
        if (m := _THICKNESS_GENERIC_RE.search(text)):
            out["thickness_mils"] = float(m.group(1))

    if (m := _CASE_QTY_RE.search(text)):
        out["case_quantity_boxes"] = int(m.group(1))

    return out


# ── Surgical / suture attributes ────────────────────────────────────────────

_SUTURE_SIZE_RE = re.compile(r"\b(\d+-0|\d+/0)\b")
_DIMENSIONS_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:x|×|by)\s*\d+(?:\.\d+)?\s*(?:mm|cm))\b", re.I
)
_NEEDLE_LENGTH_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:mm|cm)\s+needle\b", re.I
)
_MATERIAL_SURGICAL_RE = re.compile(
    r"\b(silk|nylon|gut|chromic|PTFE|polypropylene|polyester|collagen|"
    r"polyglactin|polydioxanone|catgut|monofilament)\b",
    re.I,
)


def _parse_surgical_specs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}

    if (m := _SUTURE_SIZE_RE.search(text)):
        out["suture_size"] = m.group(1).replace("/0", "-0")

    absorbable = _flag(text, r"\b(?:absorbable|resorbable)\b", r"\bnon[-\s]?absorbable\b")
    if absorbable is not None:
        out["absorbable"] = absorbable

    sterile = _flag(text, r"\bsterile\b", r"\bnon[-\s]?sterile\b")
    if sterile is not None:
        out["sterile"] = sterile

    if (m := _MATERIAL_SURGICAL_RE.search(text)):
        out["material"] = m.group(1).lower()

    if (m := _DIMENSIONS_RE.search(text)):
        out["dimensions"] = m.group(1).strip()

    if (m := _NEEDLE_LENGTH_RE.search(text)):
        out["needle_length_mm"] = float(m.group(1))

    return out


# ── Public entrypoint ───────────────────────────────────────────────────────

def parse_specifications(
    parent_description: str | None,
    variant_descriptions: list[str] | None = None,
    category_path: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Run both parsers; pick the family that best matches the category.

    Returns (specs_dict, source) where source describes which extractor
    produced the data — useful telemetry for `extraction_quality.spec_source`.
    """
    text_parts = [parent_description or ""]
    if variant_descriptions:
        text_parts.extend(d for d in variant_descriptions if d)
    text = " ".join(text_parts)
    if not text.strip():
        return {}, "empty"

    cat_blob = " ".join(category_path or []).lower()
    is_glove = "glove" in cat_blob
    is_surgical = any(k in cat_blob for k in ("suture", "surgical", "implant", "graft"))

    glove_specs = _parse_glove_specs(text) if (is_glove or not is_surgical) else {}
    surgical_specs = _parse_surgical_specs(text) if (is_surgical or not is_glove) else {}

    if glove_specs and not surgical_specs:
        return glove_specs, "glove-rules"
    if surgical_specs and not glove_specs:
        return surgical_specs, "surgical-rules"
    if glove_specs and surgical_specs:
        # Both fired (overlapping vocab). Prefer the one matching the category;
        # otherwise merge with glove rules taking precedence on conflicts.
        if is_glove:
            merged = {**surgical_specs, **glove_specs}
            return merged, "glove-rules+surgical-rules"
        if is_surgical:
            merged = {**glove_specs, **surgical_specs}
            return merged, "surgical-rules+glove-rules"
        merged = {**surgical_specs, **glove_specs}
        return merged, "glove-rules+surgical-rules"
    return {}, "rules-no-match"

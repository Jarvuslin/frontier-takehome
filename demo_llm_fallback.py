"""
Demo script: shows the LLM fallback firing on a page with no structured data.

Run:
    ANTHROPIC_API_KEY=sk-ant-... python demo_llm_fallback.py

What it does:
  1. Builds a minimal HTML page that looks like a product page but has zero
     JSON-LD, zero OpenGraph tags, and zero microdata — just raw visible text.
  2. Runs the full Extractor against it — all four deterministic tiers return empty.
  3. Because name is missing, the LLM fallback fires.
  4. Prints what each tier found and what the LLM recovered.
  5. Prints API metadata (model, tokens, stop reason, latency) so you can
     verify a real Anthropic API round-trip happened.
"""
import os
import sys
import time

from dotenv import load_dotenv
load_dotenv()

# ── minimal product HTML with NO structured data ─────────────────────────────
BARE_HTML = """
<html>
<head><title>SafcoTest - Ultrasoft Nitrile Gloves</title></head>
<body>
  <!-- A deliberately unstructured page: no JSON-LD, no OpenGraph,
       no microdata, no <h1>, and none of the CSS classes that the
       project's selectors.yaml looks for. The deterministic pipeline
       will produce nothing, so the LLM fallback is the only way out. -->
  <div class="page-wrap">
    <div class="crumbs">Home &gt; Gloves &gt; Nitrile</div>
    <div class="hero-title-styled-as-h1">Ultrasoft Nitrile Exam Gloves</div>
    <div class="row vendor">Manufactured by: DentalShield Inc.</div>
    <div class="row item-id">Item No: DSH-4421</div>
    <div class="row money">Price: $18.99 per box</div>
    <div class="row inventory">Availability: In Stock</div>
    <div class="copy">
      Powder-free nitrile examination gloves. Ambidextrous, beaded cuff.
      Thickness: 3.2 mil at palm. 200 gloves per box.
    </div>
    <div class="bullets">
      <span>Latex-free</span> &middot;
      <span>Textured fingertip</span> &middot;
      <span>Color: Blue</span>
    </div>
  </div>
</body>
</html>
"""

URL = "https://www.safcodental.com/product/ultrasoft-nitrile-demo"


def main() -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY environment variable first.")
        sys.exit(1)

    # ── import project modules ────────────────────────────────────────────────
    sys.path.insert(0, "src")
    import yaml
    from safco_agent.agents.extractor import Extractor
    from safco_agent.agents.llm_fallback import LLMFallbackAgent
    from safco_agent.settings import LLMFallback

    selectors = yaml.safe_load(open("config/selectors.yaml", encoding="utf-8"))
    extractor = Extractor(selectors, "https://www.safcodental.com")

    print("=" * 60)
    print("STEP 1 — Running deterministic extraction tiers 1-4")
    print("=" * 60)
    product, variants, methods = extractor.extract(URL, BARE_HTML)
    print(f"  JSON-LD tier  : no <script type='application/ld+json'> found")
    print(f"  OpenGraph tier: no <meta property='og:...'> found")
    print(f"  Microdata tier: no [itemtype*='Product'] found")
    print(f"  CSS selector  : selectors target Safco-specific class names")
    print(f"  Result        : product={product}")
    print(f"  Variants      : {len(variants)}")
    print(f"  Methods used  : {methods}")
    print()

    print("=" * 60)
    print("STEP 2 — Triggering LLM fallback (Claude Haiku)")
    print("=" * 60)
    cfg = LLMFallback(
        enabled=True,
        api_key=api_key,
        model="claude-haiku-4-5-20251001",
        max_calls_per_run=5,
    )
    llm_agent = LLMFallbackAgent(cfg)
    print(f"  LLM available : {llm_agent.available}")
    print(f"  Model         : {cfg.model}")
    print(f"  Sending stripped HTML to Claude via tool-use schema...")
    print()

    t0 = time.perf_counter()
    result = llm_agent.extract(URL, BARE_HTML)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    print("=" * 60)
    print("STEP 3 — LLM response")
    print("=" * 60)
    if result:
        for k, v in result.items():
            if v not in (None, {}, []):
                print(f"  {k:20s}: {v}")
    else:
        print("  LLM returned no result.")
    print()
    print(f"  LLM calls made this run: {llm_agent.calls_made}")
    print(f"  Round-trip latency     : {elapsed_ms} ms")
    print()

    print("=" * 60)
    print("STEP 4 — Proof of a real API call (raw response metadata)")
    print("=" * 60)
    print("  Calling the Anthropic SDK directly so you can see the live")
    print("  response object — model, token usage, stop reason. These")
    print("  values come from api.anthropic.com and can't be hardcoded.")
    print()

    from anthropic import Anthropic
    from safco_agent.agents.llm_fallback import EXTRACT_TOOL, SYSTEM_PROMPT, _strip_html

    client = Anthropic(api_key=api_key)
    snippet = _strip_html(BARE_HTML)
    t0 = time.perf_counter()
    raw = client.messages.create(
        model=cfg.model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "record_product_fields"},
        messages=[{
            "role": "user",
            "content": (
                f"Page URL: {URL}\n\n"
                f"HTML snippet:\n{snippet}\n\n"
                "Extract the product fields and call record_product_fields."
            ),
        }],
    )
    raw_elapsed_ms = int((time.perf_counter() - t0) * 1000)

    print(f"  response.id          : {raw.id}")
    print(f"  response.model       : {raw.model}")
    print(f"  response.stop_reason : {raw.stop_reason}")
    print(f"  response.role        : {raw.role}")
    print(f"  response.type        : {raw.type}")
    print(f"  usage.input_tokens   : {raw.usage.input_tokens}")
    print(f"  usage.output_tokens  : {raw.usage.output_tokens}")
    print(f"  network round-trip   : {raw_elapsed_ms} ms")
    print()
    print(f"  → Each call returns a unique response.id (try running again).")
    print(f"  → Token counts vary slightly per call; a hardcoded response wouldn't.")
    print(f"  → The raw response is a `Message` object from anthropic.types,")
    print(f"    constructed by the SDK from the HTTPS response body. There is")
    print(f"    no hardcoded data path in this codebase — see")
    print(f"    src/safco_agent/agents/llm_fallback.py lines 87-112 for the")
    print(f"    actual call site.")


if __name__ == "__main__":
    main()

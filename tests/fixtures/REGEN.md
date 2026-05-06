# Regenerating fixtures

These HTML fixtures were captured against live product pages on 2026-05-06
and are committed for offline tests. To refresh:

```python
import asyncio, httpx
async def go():
    urls = [
        "https://www.safcodental.com/product/aquasoft",
        "https://www.safcodental.com/product/lavender-nitrile",
        "https://www.safcodental.com/product/clearance-item",
    ]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "SafcoCatalogBot/0.1"}) as c:
        for u in urls:
            r = await c.get(u, follow_redirects=True)
            slug = u.rsplit("/", 1)[1]
            open(f"tests/fixtures/product_{slug}.html", "w", encoding="utf-8").write(r.text)
asyncio.run(go())
```

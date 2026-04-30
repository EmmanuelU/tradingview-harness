"""Prove DOM data feed: read live OHLCV + price from open TradingView window."""

import asyncio
import sys
sys.path.insert(0, "mcp_server")

from browser import get_page, close


async def main():
    page = await get_page()
    print(f"URL: {page.url}\n")

    data = await page.evaluate("""() => {
        const result = {};

        // Current price — top bar or legend
        const priceEl = document.querySelector('.lastPrice-JWoJqCpY, .price-qWcO4bp9, [class*="lastPrice"], [class*="priceWrapper"] span');
        result.price = priceEl?.innerText?.trim() || null;

        // OHLCV from chart legend (top-left of chart)
        const legendItems = [...document.querySelectorAll('[class*="legendMainSourceWrapper"] [class*="valueItem"], [class*="legend-"] [class*="value"]')];
        result.legend = legendItems.map(el => el.innerText?.trim()).filter(Boolean).slice(0, 8);

        // Symbol from title bar
        const symEl = document.querySelector('[class*="titleWrapper"] [class*="title"], .title-l31H9iuA, [data-name="legend-source-title"]');
        result.symbol = symEl?.innerText?.trim() || null;

        // Timeframe from active button
        const tfEl = document.querySelector('[class*="isActive"][class*="button"], .isActive-GBlBNBW4');
        result.timeframe = tfEl?.innerText?.trim() || null;

        // Broader price fallback — any visible price-looking text
        const allPrices = [...document.querySelectorAll('[class*="price"],[class*="Price"]')]
            .map(el => el.innerText?.trim())
            .filter(t => t && /^\\d[\\d,\\.]+$/.test(t))
            .slice(0, 5);
        result.price_candidates = allPrices;

        return result;
    }""")

    print("=== LIVE DOM READ ===")
    for k, v in data.items():
        print(f"  {k}: {v}")

    # Also grab a fresh screenshot
    png = await page.screenshot(type="png")
    with open("dom_proof.png", "wb") as f:
        f.write(png)
    print(f"\nScreenshot: dom_proof.png ({len(png)//1024}KB)")

    await close()


asyncio.run(main())

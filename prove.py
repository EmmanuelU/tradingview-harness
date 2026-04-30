"""Proof: open TradingView, set AAPL 1D, screenshot, save to proof.png."""

import asyncio
import base64
import sys
sys.path.insert(0, "mcp_server")

from browser import get_page, close


async def main():
    print("→ launching browser...")
    page = await get_page()

    print("→ navigating to TradingView...")
    await page.goto("https://www.tradingview.com/chart/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(4000)

    print("→ setting symbol NASDAQ:AAPL 1D...")
    await page.keyboard.press("/")
    await page.wait_for_timeout(600)
    await page.keyboard.press("Control+a")
    await page.keyboard.type("NASDAQ:AAPL", delay=60)
    await page.wait_for_timeout(1200)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)

    print("→ capturing screenshot...")
    png = await page.screenshot(type="png")
    with open("proof.png", "wb") as f:
        f.write(png)

    print(f"✓ proof.png saved ({len(png)//1024} KB) — chart is live")
    print("  browser stays open. Ctrl+C to close.")

    # Keep browser open for user to inspect
    try:
        await asyncio.sleep(99999)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await close()


asyncio.run(main())

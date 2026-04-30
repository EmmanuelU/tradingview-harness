"""TV navigation + DOM read utilities. No imports from server.py — safe for paper_tv."""

# Common TV popup close selectors — data-name/aria only (no class hashes)
_POPUP_CLOSE_SELS = [
    "[data-name='close-button']",
    "[data-name='dialog-close-button']",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "[data-name='notification-close-button']",
]

TV_CHART = "https://www.tradingview.com/chart/"

TF_CODES = {
    "1m": "1",   "3m": "3",   "5m": "5",   "15m": "15",  "30m": "30",
    "1h": "60",  "2h": "120", "4h": "240",
    "1D": "D",   "1W": "W",   "1M": "M",
}


async def dismiss_popups(page) -> list[str]:
    """
    Dismiss TV announcement/modal popups. Safe to call anytime — no-op if nothing open.
    Returns list of selectors that matched.
    """
    dismissed = []
    # Escape closes most modal dialogs first
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    except Exception:
        pass
    for sel in _POPUP_CLOSE_SELS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                await el.click(timeout=300)
                dismissed.append(sel)
                await page.wait_for_timeout(150)
        except Exception:
            pass
    return dismissed


async def navigate(page, symbol: str, tf: str):
    """Navigate to TV chart URL. Polls until title contains live price."""
    code = TF_CODES.get(tf, "D")
    url  = f"{TV_CHART}?symbol={symbol}&interval={code}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    ticker_root = symbol.split(":")[-1]
    for _ in range(30):
        title = await page.title()
        if ticker_root in title and any(ch.isdigit() for ch in title):
            break
        await page.wait_for_timeout(200)


async def parse_title(page) -> dict:
    """Parse live price from TV <title>: 'AAPL 270.22 ▲ +0.50%'"""
    title = await page.title()
    parts = title.split()
    result = {"symbol": parts[0] if parts else "?", "raw_title": title}
    if len(parts) >= 2:
        result["price"] = parts[1]
    if len(parts) >= 4:
        result["direction"] = parts[2]
        result["change"]    = parts[3]
    return result


async def read_ohlcv(page) -> dict:
    """Read OHLCV from DOM header row (top 120px leaf text nodes)."""
    texts = await page.evaluate("""() =>
        [...document.querySelectorAll('*')]
            .filter(el => el.children.length === 0 && el.offsetParent !== null
                       && el.getBoundingClientRect().top < 120)
            .map(el => el.innerText?.trim())
            .filter(t => t && t.length > 0)
    """)
    ohlcv: dict = {}
    key_map = {"O": "open", "H": "high", "L": "low", "C": "close"}
    last = None
    for t in texts:
        if t in key_map:
            last = key_map[t]
        elif last:
            ohlcv[last] = t
            last = None
    return ohlcv

"""Playwright — persistent Chrome, multi-page (one per pane), CDP-first attach."""

from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page

_pw = None
_context: BrowserContext | None = None
_pages: list[Page] = []

USER_DATA_DIR = Path.home() / ".crackincorrelation" / "chrome-profile"
CDP_PORT = 9223
TV_BASE = "https://www.tradingview.com/chart/"


async def _connect_cdp(pw):
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{CDP_PORT}", timeout=3000
        )
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx:
            return ctx
    except Exception:
        pass
    return None


async def _launch_fresh(pw):
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = USER_DATA_DIR / "SingletonLock"
    if lock.exists():
        lock.unlink()
    return await pw.chromium.launch_persistent_context(
        str(USER_DATA_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--start-maximized", f"--remote-debugging-port={CDP_PORT}"],
    )


async def _get_context() -> BrowserContext:
    global _pw, _context
    if _context:
        return _context
    _pw = await async_playwright().start()
    _context = await _connect_cdp(_pw)
    if not _context:
        _context = await _launch_fresh(_pw)
    return _context


async def get_page(index: int = 0) -> Page:
    """Return page at index, creating new tabs as needed."""
    global _pages
    ctx = await _get_context()

    # Sync _pages with actual context pages (handles external opens/closes)
    live = [p for p in ctx.pages if not p.is_closed()]
    # Fill _pages up to index
    while len(live) <= index:
        live.append(await ctx.new_page())
    _pages = live
    return _pages[index]


async def ensure_tv(index: int = 0) -> Page:
    """Get page at index, navigate to TV chart if not already there."""
    page = await get_page(index)
    if "tradingview.com/chart" not in page.url:
        await page.goto(TV_BASE, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    return page


async def page_count() -> int:
    ctx = await _get_context()
    return len([p for p in ctx.pages if not p.is_closed()])


async def close():
    global _pw, _context, _pages
    if _context:
        await _context.close()
    if _pw:
        await _pw.stop()
    _context = None
    _pages = []
    _pw = None

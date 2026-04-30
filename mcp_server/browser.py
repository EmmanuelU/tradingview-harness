"""Playwright — persistent Chrome, crash-immune, CDP-first, resource-bounded."""

import asyncio
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page

_pw            = None
_context: BrowserContext | None = None
_pages: list[Page] = []
_recovering    = False   # guard against concurrent recovery

USER_DATA_DIR  = Path.home() / ".crackincorrelation" / "chrome-profile"
CDP_PORT       = 9223
TV_BASE        = "https://www.tradingview.com/chart/"
TV_LOGIN_URL   = "tradingview.com/accounts/signin"
MAX_TABS       = 20      # hard resource ceiling


# ── health ─────────────────────────────────────────────────────────────────────

async def is_healthy() -> bool:
    """True if Chrome is up and context has at least one live page."""
    global _context
    if not _context:
        return False
    try:
        live = [p for p in _context.pages if not p.is_closed()]
        if not live:
            return False
        # Lightweight CDP ping — title read on first page
        await asyncio.wait_for(_context.pages[0].title(), timeout=3)
        return True
    except Exception:
        return False


async def is_logged_in(page: Page) -> bool:
    """False if page has been redirected to TV login."""
    try:
        url = page.url
        return TV_LOGIN_URL not in url
    except Exception:
        return False


def _kill_stale_chrome():
    """Kill any Chrome process using CDP port (stale after crash)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{CDP_PORT}"], capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
    except Exception:
        pass


# ── launch / connect ───────────────────────────────────────────────────────────

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
    _kill_stale_chrome()
    return await pw.chromium.launch_persistent_context(
        str(USER_DATA_DIR),
        headless=False,
        viewport={"width": 1920, "height": 1080},
        args=["--start-maximized", f"--remote-debugging-port={CDP_PORT}"],
    )


async def _get_context() -> BrowserContext:
    global _pw, _context
    if _context and await is_healthy():
        return _context
    # Stale or missing — reset and reconnect
    await _reset_silent()
    _pw = await async_playwright().start()
    _context = await _connect_cdp(_pw)
    if not _context:
        _context = await _launch_fresh(_pw)
    return _context


# ── recovery ───────────────────────────────────────────────────────────────────

async def _reset_silent():
    """Teardown stale context/playwright without raising."""
    global _pw, _context, _pages
    try:
        if _context:
            await _context.close()
    except Exception:
        pass
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _context = None
    _pages   = []
    _pw      = None


async def recover() -> bool:
    """
    Full crash recovery: reset → relaunch Chrome → return True on success.
    Caller must call restore_layout() after this to reopen tabs.
    """
    global _recovering
    if _recovering:
        return False
    _recovering = True
    try:
        print("[BROWSER] recovering Chrome...", flush=True)
        await _reset_silent()
        await _get_context()           # relaunch
        healthy = await is_healthy()
        print(f"[BROWSER] recovery {'✓' if healthy else '✗'}", flush=True)
        return healthy
    except Exception as e:
        print(f"[BROWSER] recovery failed: {e}", flush=True)
        return False
    finally:
        _recovering = False


# ── page access ────────────────────────────────────────────────────────────────

async def get_page(index: int = 0) -> Page:
    """Return page at index, creating tabs as needed. Enforces MAX_TABS."""
    global _pages
    if index >= MAX_TABS:
        raise ValueError(f"tab index {index} exceeds MAX_TABS={MAX_TABS}")
    ctx = await _get_context()
    live = [p for p in ctx.pages if not p.is_closed()]
    while len(live) <= index:
        live.append(await ctx.new_page())
    _pages = live
    return _pages[index]


async def ensure_tv(index: int = 0) -> Page:
    """Get page at index. Navigate to TV chart if not already there. Login-aware."""
    page = await get_page(index)
    url  = page.url
    if "tradingview.com/chart" not in url:
        await page.goto(TV_BASE, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    if TV_LOGIN_URL in page.url:
        raise RuntimeError("TV session expired — login required")
    return page


async def close_orphan_tabs(keep: int):
    """Close tabs beyond `keep` count. Prevents resource accumulation."""
    try:
        ctx  = await _get_context()
        live = [p for p in ctx.pages if not p.is_closed()]
        for page in live[keep:]:
            await page.close()
    except Exception:
        pass


async def page_count() -> int:
    ctx = await _get_context()
    return len([p for p in ctx.pages if not p.is_closed()])


async def close():
    """Explicit teardown. Only call when intentionally shutting down."""
    await _reset_silent()

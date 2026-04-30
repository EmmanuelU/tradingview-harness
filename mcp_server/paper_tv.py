"""TradingView Paper Trading — DOM-native order execution. Zero external deps."""

import asyncio
from browser import ensure_tv
from server import _navigate, _parse_title

# Confirmed selectors (probed 2026-04-30)
SEL_BUY_SIDE   = "[data-name='side-control-buy']"
SEL_SELL_SIDE  = "[data-name='side-control-sell']"
SEL_QTY        = "input#quantity-field"
SEL_PLACE      = "[data-name='place-and-modify-button']"
SEL_POSITIONS  = "[data-name='Paper.positions-table']"
SEL_BUY_BTN    = "[data-name='buy-order-button']"
SEL_ORDER_TYPE = "[data-name='order-panel']"


async def _ensure_paper_connected(page) -> bool:
    """Connect Paper Trading broker if not already open. Returns True on success."""
    # Panel is connected if place-and-modify-button is present
    connected = await page.evaluate(
        "() => !!document.querySelector(\"[data-name='place-and-modify-button']\")"
    )
    if connected:
        return True

    # Open broker dialog
    buy_btn = page.locator(SEL_BUY_BTN).first
    await buy_btn.dispatch_event('click')
    await page.wait_for_timeout(800)

    # Select Paper Trading
    selected = await page.evaluate("""() => {
        const dialog = document.querySelector("[data-name='select-broker-dialog']");
        if (!dialog) return false;
        const pt = [...dialog.querySelectorAll('*')]
            .find(el => el.innerText?.trim() === 'Paper Trading' && el.children.length <= 2);
        if (pt) { pt.click(); return true; }
        return false;
    }""")
    if not selected:
        return False
    await page.wait_for_timeout(800)

    # Click Connect
    await page.evaluate("""() => {
        const dialog = document.querySelector("[data-name='broker-login-dialog']");
        if (!dialog) return;
        const btn = [...dialog.querySelectorAll('button')]
            .find(el => el.innerText?.trim() === '' || el.innerText?.trim() === 'Connect');
        if (btn) btn.click();
    }""")
    await page.wait_for_timeout(1500)

    return await page.evaluate(
        "() => !!document.querySelector(\"[data-name='place-and-modify-button']\")"
    )


async def place_order(tv_symbol: str, side: str, qty: float, pane: int = 0) -> dict:
    """
    Place a paper order on TradingView. Zero external dependencies.

    tv_symbol: 'NASDAQ:AAPL' | 'BINANCE:BTCUSDT'
    side: 'buy' | 'sell'
    qty: number of units
    pane: tab index

    Returns: {ok, side, symbol, qty, button_text, error?}
    """
    page = await ensure_tv(pane)

    # Navigate to symbol (no-op if already there)
    sym_root = tv_symbol.split(":")[-1]
    title = await page.title()
    if sym_root not in title:
        tf = "1D"  # default; caller should set pane before calling
        await _navigate(page, tv_symbol, tf)
        await page.wait_for_timeout(500)

    # Ensure paper panel open
    connected = await _ensure_paper_connected(page)
    if not connected:
        return {"ok": False, "error": "paper trading panel not connected"}

    # Pass args to JS to avoid selector quote issues
    side_sel = SEL_BUY_SIDE if side.lower() == "buy" else SEL_SELL_SIDE
    result_js = await page.evaluate(
        """async ([sideSel, qtySel, placeSel, qtyVal]) => {
        const delay = ms => new Promise(r => setTimeout(r, ms));

        const sideEl = document.querySelector(sideSel);
        if (!sideEl) return {ok: false, error: 'side button not found: ' + sideSel};
        sideEl.click();
        await delay(200);

        const qtyInput = document.querySelector(qtySel);
        if (!qtyInput) return {ok: false, error: 'qty input not found'};
        qtyInput.focus();
        qtyInput.select();
        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        nativeSetter.call(qtyInput, qtyVal);
        qtyInput.dispatchEvent(new Event('input', {bubbles: true}));
        qtyInput.dispatchEvent(new Event('change', {bubbles: true}));
        await delay(300);

        const placeBtn = document.querySelector(placeSel);
        const btnText = placeBtn?.innerText?.trim() || null;
        if (!placeBtn) return {ok: false, error: 'place button not found'};
        placeBtn.click();
        await delay(500);

        const confirmBtn = [...document.querySelectorAll('button')]
            .find(b => b.offsetParent !== null && /confirm|ok|yes|place/i.test(b.innerText));
        if (confirmBtn) confirmBtn.click();

        return {ok: true, btn_text: btnText, confirm_dialog: !!confirmBtn};
        }""",
        [side_sel, SEL_QTY, SEL_PLACE, str(qty)],
    )

    if not result_js.get("ok"):
        return {"ok": False, "error": result_js.get("error"), "symbol": tv_symbol, "side": side, "qty": qty}

    await page.wait_for_timeout(300)
    confirmed = result_js.get("confirm_dialog", False)
    btn_text   = result_js.get("btn_text")

    return {
        "ok": True,
        "side": side,
        "symbol": tv_symbol,
        "qty": qty,
        "button_text": btn_text,
        "confirm_dialog": confirmed,
    }


async def get_positions(pane: int = 0) -> list:
    """Read open positions from paper trading panel."""
    page = await ensure_tv(pane)
    await _ensure_paper_connected(page)

    rows = await page.evaluate("""() => {
        const table = document.querySelector("[data-name='Paper.positions-table']");
        if (!table) return [];
        const cells = [...table.querySelectorAll('td')].map(td => td.innerText?.trim());
        // Group into rows of ~12 columns
        const rows = [];
        for (let i = 0; i < cells.length; i += 12) {
            const row = cells.slice(i, i + 12);
            if (row[0] && row[0].length > 0) rows.push(row);
        }
        return rows;
    }""")
    return rows


async def get_account_metrics(pane: int = 0) -> dict:
    """Read account metrics (equity, P&L) from paper panel."""
    page = await ensure_tv(pane)
    await _ensure_paper_connected(page)

    return await page.evaluate("""() => {
        const qa = sel => [...document.querySelectorAll(sel)];
        const metrics = {};
        qa('[class*="metric"],[class*="Metric"],[class*="account"],[class*="equity"],[class*="balance"],[class*="pl"],[class*="PL"]')
            .filter(el => el.offsetParent !== null && el.innerText?.trim())
            .forEach(el => {
                const text = el.innerText?.trim();
                if (text && text.length < 60) {
                    const key = el.getAttribute('data-name') || el.className.split(' ')[0];
                    metrics[key] = text;
                }
            });

        // Also get the round-tabs metrics area
        const metricsTab = document.querySelector("[data-name='round-tabs-buttons']");
        if (metricsTab) metrics['_panel_tabs'] = metricsTab.innerText?.trim();

        return metrics;
    }""")

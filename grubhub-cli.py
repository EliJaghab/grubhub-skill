#!/usr/bin/env python3
"""
Grubhub CLI -- read-only API client for Claude Code /grubhub skill.

Auth: Cookie-based. Extracts cookies from the live Playwright Chrome browser
via Chrome DevTools Protocol (CDP), or from a cached session file.

Usage:
    python3 grubhub-cli.py whoami
    python3 grubhub-cli.py search "sushi"
    python3 grubhub-cli.py menu 8519672
    python3 grubhub-cli.py history
    python3 grubhub-cli.py favorites
    python3 grubhub-cli.py offers 8519672
    python3 grubhub-cli.py ratings 8519672

All output is JSON to stdout. Use --format table for human-readable output.
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Fix SSL on macOS -- use certifi's CA bundle
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # Try system default; if it fails, user needs to install certifi
    SSL_CONTEXT = ssl.create_default_context()

BASE_URL = "https://api-gtm.grubhub.com"
SESSION_FILE = Path.home() / ".grubhub-session.json"
DINER_ID = "52089da0-cd66-11f0-84a1-bd5e2b68cd84"

# Default location: 135 Madison Ave, NYC 10016
DEFAULT_LAT = 40.74587631
DEFAULT_LNG = -73.98403168
DEFAULT_GEOHASH = "dr5ru3w3zjge"


def find_cdp_port():
    """Find the Chrome DevTools Protocol port for the Playwright browser."""
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True, timeout=5
        )
        for line in out.splitlines():
            if "ms-playwright/mcp-chrome" in line and "--remote-debugging-port=" in line:
                for part in line.split():
                    if part.startswith("--remote-debugging-port="):
                        return int(part.split("=")[1])
    except Exception:
        pass
    return None


def extract_cookies_from_cdp():
    """Extract Grubhub cookies from the live Playwright Chrome browser via CDP."""
    port = find_cdp_port()
    if not port:
        return None

    try:
        # Get list of pages
        req = urllib.request.Request(f"http://localhost:{port}/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            pages = json.loads(resp.read().decode())

        # Find the grubhub page
        ws_url = None
        for page in pages:
            if "grubhub.com" in page.get("url", ""):
                ws_url = page.get("webSocketDebuggerUrl")
                break

        if not ws_url:
            # Use the first page
            if pages:
                ws_url = pages[0].get("webSocketDebuggerUrl")

        if not ws_url:
            return None

        # Use websockets to get cookies via CDP
        # We'll use a subprocess to avoid import issues
        script = f'''
import asyncio, json, websockets
async def get():
    async with websockets.connect("{ws_url}", max_size=10*1024*1024) as ws:
        await ws.send(json.dumps({{"id":1,"method":"Network.getCookies","params":{{"urls":["https://api-gtm.grubhub.com","https://www.grubhub.com"]}}}}))\n        r = await ws.recv()
        d = json.loads(r)
        cookies = d.get("result",{{}}).get("cookies",[])
        # Build cookie dict for api-gtm.grubhub.com
        result = {{}}
        for c in cookies:
            if c["domain"] in (".grubhub.com", "api-gtm.grubhub.com", "www.grubhub.com"):
                result[c["name"]] = c["value"]
        print(json.dumps(result))
asyncio.run(get())
'''
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
    except Exception as e:
        return {"_error": str(e)}

    return None


def load_session():
    """Load cached session from disk."""
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            if data.get("expires_at", 0) > time.time() and data.get("cookies"):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def save_session(cookies, diner_id=None, expires_in=3600):
    """Cache session to disk."""
    data = {
        "cookies": cookies,
        "diner_id": diner_id or DINER_ID,
        "expires_at": time.time() + expires_in,
        "saved_at": time.time(),
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2))
    return data


def get_cookies():
    """Get auth cookies from session cache or live browser."""
    session = load_session()
    if session and session.get("cookies"):
        return session["cookies"]

    # Try extracting from live Playwright browser
    cookies = extract_cookies_from_cdp()
    if cookies and "_error" not in cookies and len(cookies) > 5:
        save_session(cookies)
        return cookies

    return None


def api_request(path, method="GET", params=None, body=None, cookies=None):
    """Make an authenticated request to the Grubhub API using cookies."""
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://www.grubhub.com",
        "Referer": "https://www.grubhub.com/",
    }

    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Cookie"] = cookie_str

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            body = resp.read().decode()
            if not body:
                return {"status": resp.status}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": e.code, "reason": e.reason, "body": error_body[:500], "url": url}
    except urllib.error.URLError as e:
        return {"error": "connection_failed", "reason": str(e.reason), "url": url}


def get_diner_id():
    """Get diner ID from session."""
    session = load_session()
    if session and session.get("diner_id"):
        return session["diner_id"]
    return DINER_ID


def _cents_to_dollars(amount):
    """Convert cents to dollars. Grubhub API returns all prices in cents."""
    if isinstance(amount, (int, float)):
        return round(amount / 100.0, 2)
    return amount


# --- Subcommands ---

def cmd_whoami(cookies, args):
    """Show current user info and delivery address."""
    diner_id = get_diner_id()
    details = api_request(f"/diners/{diner_id}/details", cookies=cookies)
    addresses = api_request(f"/diners/{diner_id}/addresses", cookies=cookies)
    return {
        "diner_id": diner_id,
        "details": details,
        "addresses": addresses,
    }


def cmd_search(cookies, args):
    """Search for restaurants."""
    if not args:
        return {"error": "Usage: search <query>"}

    query = " ".join(args)
    params = {
        "orderMethod": "delivery",
        "locationMode": "DELIVERY",
        "facetSet": "umamiV6",
        "pageSize": "20",
        "hideHateos": "true",
        "searchMetrics": "true",
        "queryText": query,
        "latitude": DEFAULT_LAT,
        "longitude": DEFAULT_LNG,
        "preciseLocation": "true",
        "geohash": DEFAULT_GEOHASH,
        "sortSetId": "umamiV3",
        "countOmittingTimes": "true",
        "includeOffers": "true",
    }
    result = api_request("/restaurants/search/search_listing", params=params, cookies=cookies)

    if "error" in result:
        return result

    restaurants = []
    for r in result.get("results", []):
        ratings = r.get("ratings", {})
        delivery_fee = r.get("delivery_fee", {})
        slug = r.get("merchant_url_path") or r.get("slug_name") or ""
        info = {
            "id": r.get("restaurant_id"),
            "name": r.get("name"),
            "slug": slug,
            "rating": ratings.get("rating_bayesian_half_point") or ratings.get("rating_value"),
            "rating_count": ratings.get("rating_count"),
            "delivery_fee": _cents_to_dollars(delivery_fee.get("price") if isinstance(delivery_fee, dict) else delivery_fee),
            "delivery_time_estimate": r.get("delivery_time_estimate"),
            "cuisine": [c.get("name") if isinstance(c, dict) else c for c in r.get("cuisines", [])],
            "price_rating": r.get("price_rating"),
            "url": f"https://www.grubhub.com/restaurant/{slug}/{r.get('restaurant_id')}",
        }
        if info["id"]:
            restaurants.append(info)

    return {"query": query, "count": len(restaurants), "restaurants": restaurants}


def cmd_menu(cookies, args):
    """Show restaurant menu."""
    if not args:
        return {"error": "Usage: menu <restaurant_id>"}

    restaurant_id = args[0]
    params = {
        "hideChoiceCategories": "true",
        "version": "4",
        "variationId": "rtpFreeItems",
        "orderType": "standard",
        "hideUnavailableMenuItems": "true",
    }

    info = api_request(f"/restaurants/{restaurant_id}", params=params, cookies=cookies)

    if "error" in info:
        return {"error": "Failed to fetch restaurant", "details": info}

    # Parse restaurant info and menu from the combined response
    restaurant = info.get("restaurant", info)
    restaurant_name = restaurant.get("name", f"Restaurant {restaurant_id}")
    slug = restaurant.get("merchant_url_path") or restaurant.get("slug_name") or ""

    # Menu categories are nested in the restaurant response
    categories = {}
    for cat in restaurant.get("menu_category_list", []):
        cat_name = cat.get("name", "Uncategorized")
        items = []
        for item in cat.get("menu_item_list", []):
            price = item.get("price", {})
            amount = price.get("amount") if isinstance(price, dict) else price
            items.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "price": _cents_to_dollars(amount),
                "description": (item.get("description") or "")[:120],
                "popular": item.get("popular", False),
            })
        if items:
            categories[cat_name] = items

    return {
        "restaurant_id": restaurant_id,
        "name": restaurant_name,
        "slug": slug,
        "url": f"https://www.grubhub.com/restaurant/{slug}/{restaurant_id}",
        "categories": categories,
    }


def cmd_history(cookies, args):
    """Show recent order history."""
    diner_id = get_diner_id()
    params = {
        "pageSize": "10",
        "pageNum": "1",
        "facet": "scheduled:false",
        "includePartnerOrders": "true",
        "sorts": "default",
    }
    result = api_request(f"/diners/{diner_id}/search_listing", params=params, cookies=cookies)

    if "error" in result:
        return result

    orders = []
    for r in result.get("results", []):
        restaurants = r.get("restaurants", [])
        restaurant_names = [rest.get("name") for rest in restaurants]
        restaurant_ids = [rest.get("id") for rest in restaurants]
        charges = r.get("charges", {})
        lines = charges.get("lines", {})
        line_items = lines.get("line_items", [])
        fees = charges.get("fees", {})
        taxes = charges.get("taxes", {})
        tip = charges.get("tip", {})
        payments_list = r.get("payments", {}).get("payments", [])

        payment_breakdown = []
        for pay in payments_list:
            entry = {
                "type": pay.get("type"),
                "amount": _cents_to_dollars(pay.get("amount")),
            }
            meta = pay.get("metadata", {})
            if pay.get("type") == "CORPORATE_LINE_OF_CREDIT":
                entry["corp_name"] = meta.get("corp_name")
            elif pay.get("type") == "CREDIT_CARD":
                entry["card"] = f"{meta.get('credit_card_type', '')} ...{meta.get('cc_last_four', '')}"
            elif pay.get("type") == "PROMO_CODE":
                entry["source"] = meta.get("source_type", "")
            payment_breakdown.append(entry)

        orders.append({
            "order_id": r.get("id"),
            "restaurants": restaurant_names,
            "restaurant_ids": restaurant_ids,
            "time_placed": r.get("time_placed"),
            "state": r.get("state"),
            "items": [
                {"name": li.get("name"), "qty": li.get("quantity"), "price": _cents_to_dollars(li.get("diner_total"))}
                for li in line_items
            ],
            "receipt": {
                "subtotal": _cents_to_dollars(charges.get("diner_subtotal")),
                "delivery_fee": _cents_to_dollars(fees.get("delivery")),
                "service_fee": _cents_to_dollars(fees.get("service")),
                "tax": _cents_to_dollars(taxes.get("total")),
                "tip": _cents_to_dollars(tip.get("amount")),
                "total": _cents_to_dollars(charges.get("diner_grand_total")),
            },
            "payments": payment_breakdown,
        })

    return {"count": len(orders), "recent_orders": orders}


def cmd_favorites(cookies, args):
    """Show favorite restaurants."""
    diner_id = get_diner_id()
    result = api_request(f"/diners/{diner_id}/favorites/restaurants", cookies=cookies)

    if "error" in result:
        return result

    favorites = []
    for r in result.get("favorite_restaurants", result.get("restaurants", result.get("results", []))):
        favorites.append({
            "restaurant_id": r.get("restaurant_id"),
            "name": r.get("name"),
            "slug": r.get("slug_name"),
            "url": f"https://www.grubhub.com/restaurant/{r.get('slug_name')}/{r.get('restaurant_id')}",
        })

    return {"count": len(favorites), "favorites": favorites}


def cmd_offers(cookies, args):
    """Show available offers for a restaurant."""
    if not args:
        return {"error": "Usage: offers <restaurant_id>"}

    restaurant_id = args[0]
    params = {
        "orderType": "STANDARD",
        "locationMode": "DELIVERY",
        "deliveryLatitude": DEFAULT_LAT,
        "deliveryLongitude": DEFAULT_LNG,
        "nonAnchorCart": "false",
    }
    result = api_request(f"/offers/availability/{restaurant_id}", params=params, cookies=cookies)
    return {"restaurant_id": restaurant_id, "offers": result}


def cmd_ratings(cookies, args):
    """Show ratings for a restaurant."""
    if not args:
        return {"error": "Usage: ratings <restaurant_id>"}

    restaurant_id = args[0]
    params = {"pageSize": "10", "pageNum": "1"}
    result = api_request(f"/ratings/search/restaurant/{restaurant_id}", params=params, cookies=cookies)
    return {"restaurant_id": restaurant_id, "ratings": result}


def cmd_auth(cookies, args):
    """Show auth status and instructions for refreshing."""
    session = load_session()
    cdp_port = find_cdp_port()

    status = {
        "session_file": str(SESSION_FILE),
        "session_valid": session is not None and bool(session.get("cookies")),
        "session_expires_at": session.get("expires_at") if session else None,
        "cdp_port": cdp_port,
        "playwright_browser_running": cdp_port is not None,
    }

    if cookies:
        # Test auth by hitting a real endpoint
        test = api_request(f"/diners/{DINER_ID}/details", cookies=cookies)
        status["auth_test"] = "success" if "error" not in test else test

    if not status["session_valid"]:
        status["instructions"] = (
            "No valid session. To authenticate:\n"
            "1. Ensure Playwright MCP browser is open with grubhub.com loaded\n"
            "2. Run: grubhub-cli.py refresh\n"
            "Or: grubhub-cli.py set-cookies '{\"__Secure-access\":\"...\", ...}'"
        )

    return status


def cmd_refresh(cookies, args):
    """Refresh cookies from the live Playwright Chrome browser."""
    fresh_cookies = extract_cookies_from_cdp()
    if not fresh_cookies or "_error" in (fresh_cookies or {}):
        return {
            "error": "Could not extract cookies from browser",
            "details": fresh_cookies,
            "hint": "Ensure Playwright browser is running with grubhub.com loaded",
        }

    save_session(fresh_cookies)

    # Test the cookies
    test = api_request(f"/diners/{DINER_ID}/details", cookies=fresh_cookies)
    success = "error" not in test

    return {
        "status": "refreshed" if success else "cookies_saved_but_auth_failed",
        "cookie_count": len(fresh_cookies),
        "auth_test": "success" if success else test,
        "session_file": str(SESSION_FILE),
    }


def cmd_set_cookies(cookies, args):
    """Manually set cookies. Usage: set-cookies '<json_dict>'"""
    if not args:
        return {"error": "Usage: set-cookies '{\"__Secure-access\":\"...\", ...}'"}

    try:
        new_cookies = json.loads(args[0])
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    session = save_session(new_cookies)
    return {"status": "saved", "cookie_count": len(new_cookies), "session_file": str(SESSION_FILE)}


def cmd_set_location(cookies, args):
    """Update default delivery location. Usage: set-location <lat> <lng> [geohash]"""
    if len(args) < 2:
        return {"error": "Usage: set-location <lat> <lng> [geohash]"}

    global DEFAULT_LAT, DEFAULT_LNG, DEFAULT_GEOHASH
    DEFAULT_LAT = float(args[0])
    DEFAULT_LNG = float(args[1])
    if len(args) > 2:
        DEFAULT_GEOHASH = args[2]

    return {
        "status": "updated",
        "lat": DEFAULT_LAT,
        "lng": DEFAULT_LNG,
        "geohash": DEFAULT_GEOHASH,
        "note": "Location change is for this session only. "
                "For persistent change, edit DEFAULT_LAT/LNG in grubhub-cli.py",
    }


def _cdp_run(script_body):
    """Run an async CDP script in a subprocess. Returns parsed JSON or error dict."""
    port = find_cdp_port()
    if not port:
        return {"error": "Playwright browser not running. Open grubhub.com in Playwright first."}

    # Find a grubhub page
    try:
        req = urllib.request.Request(f"http://localhost:{port}/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            pages = json.loads(resp.read().decode())
        ws_url = None
        for pg in pages:
            if "grubhub.com" in pg.get("url", ""):
                ws_url = pg.get("webSocketDebuggerUrl")
                break
        if not ws_url and pages:
            ws_url = pages[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return {"error": "No browser page found"}
    except Exception as e:
        return {"error": f"CDP connection failed: {e}"}

    full_script = f'''
import asyncio, json, websockets, base64, time
from pathlib import Path

WS_URL = "{ws_url}"

async def cdp_send(ws, method, params=None, id=1):
    msg = {{"id": id, "method": method}}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if r.get("id") == id:
            return r
        # Skip events

async def cdp_eval(ws, expression, id=1):
    r = await cdp_send(ws, "Runtime.evaluate", {{"expression": expression, "returnByValue": True, "awaitPromise": True}}, id)
    return r.get("result", {{}}).get("result", {{}}).get("value")

async def cdp_click(ws, x, y):
    for evt in ["mousePressed", "mouseReleased"]:
        await cdp_send(ws, "Input.dispatchMouseEvent", {{"type": evt, "x": x, "y": y, "button": "left", "clickCount": 1}}, 99)

async def cdp_screenshot(ws, path="/tmp/grubhub-step.png"):
    r = await cdp_send(ws, "Page.captureScreenshot", {{"format": "png"}}, 98)
    Path(path).write_bytes(base64.b64decode(r["result"]["data"]))
    return path

async def cdp_navigate(ws, url):
    await cdp_send(ws, "Page.navigate", {{"url": url}}, 97)
    await asyncio.sleep(4)
    # Drain events
    while True:
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.5)
        except:
            break

async def cdp_url(ws):
    return await cdp_eval(ws, "location.href", 96)

async def main():
    async with websockets.connect(WS_URL, max_size=10*1024*1024) as ws:
{script_body}

asyncio.run(main())
'''
    proc = subprocess.run(
        [sys.executable, "-c", full_script],
        capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        return {"error": "CDP script failed", "stderr": proc.stderr[-500:] if proc.stderr else ""}
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "Invalid output", "stdout": proc.stdout[-500:] if proc.stdout else ""}


def cmd_add_to_cart(cookies, args):
    """Add item to cart via browser. Usage: add-to-cart <restaurant_slug_or_url> <item_name>"""
    if len(args) < 2:
        return {"error": "Usage: add-to-cart <restaurant_slug/id> \"<item name>\""}

    restaurant = args[0]
    item_name = " ".join(args[1:])

    # Build restaurant URL
    if restaurant.startswith("http"):
        restaurant_url = restaurant
    elif "/" in restaurant:
        restaurant_url = f"https://www.grubhub.com/restaurant/{restaurant}"
    else:
        restaurant_url = f"https://www.grubhub.com/restaurant/{restaurant}"

    script = f'''
        restaurant_url = "{restaurant_url}"
        item_name = """{item_name}"""

        # Navigate to restaurant
        await cdp_navigate(ws, restaurant_url)
        await asyncio.sleep(2)

        # Find and click the menu item by name
        find_item_js = """
        (function() {{
            const btns = [...document.querySelectorAll('button, article')];
            const match = btns.find(b => b.textContent.includes('""" + item_name.replace("'", "\\'") + """'));
            if (match) {{
                match.scrollIntoView({{block: 'center'}});
                const r = match.getBoundingClientRect();
                return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2, text: match.textContent.substring(0, 60)}});
            }}
            return JSON.stringify({{error: 'Item not found on page'}});
        }})()
        """
        item_info = json.loads(await cdp_eval(ws, find_item_js, 10))
        if "error" in item_info:
            print(json.dumps(item_info))
            return

        await cdp_click(ws, item_info["x"], item_info["y"])
        await asyncio.sleep(3)

        # Take screenshot of the item detail / customization page
        await cdp_screenshot(ws, "/tmp/grubhub-item-detail.png")
        current_url = await cdp_url(ws)

        # Check what's on the page - required choices? Add to bag visible?
        page_state_js = """
        (function() {{
            const btns = [...document.querySelectorAll('button')];
            const visible = btns.filter(b => {{
                const r = b.getBoundingClientRect();
                return r.width > 30 && r.top >= 0 && r.top < window.innerHeight;
            }});
            const addBag = visible.filter(b => b.textContent.includes('Add to bag'));
            const required = visible.filter(b => b.textContent.includes('required choice') || b.textContent.includes('Make required'));
            return JSON.stringify({{
                add_to_bag: addBag.map(b => ({{text: b.textContent.trim().substring(0, 60), x: b.getBoundingClientRect().x + b.getBoundingClientRect().width/2, y: b.getBoundingClientRect().y + b.getBoundingClientRect().height/2}})),
                required_choices: required.map(b => b.textContent.trim().substring(0, 60)),
                all_buttons: visible.map(b => b.textContent.trim().substring(0, 50)).slice(0, 20)
            }});
        }})()
        """
        page_state = json.loads(await cdp_eval(ws, page_state_js, 11))

        if page_state.get("required_choices"):
            # Has required customization - report what's needed
            print(json.dumps({{
                "status": "requires_customization",
                "item": item_name,
                "required_choices": page_state["required_choices"],
                "screenshot": "/tmp/grubhub-item-detail.png",
                "url": current_url,
                "hint": "Use 'customize' command to make selections, or use the browser directly"
            }}))
            return

        # No required choices - try to click Add to bag
        if page_state.get("add_to_bag"):
            bag_btn = page_state["add_to_bag"][0]
            await cdp_click(ws, bag_btn["x"], bag_btn["y"])
            await asyncio.sleep(3)

            # Verify item was added - check bag count
            bag_count_js = """
            (function() {{
                // Look for bag icon with count
                const els = document.querySelectorAll('*');
                for (const el of els) {{
                    const r = el.getBoundingClientRect();
                    if (r.top < 60 && r.right > window.innerWidth - 100 && el.textContent.trim().match(/^\\d+$/)) {{
                        return el.textContent.trim();
                    }}
                }}
                return '0';
            }})()
            """
            bag_count = await cdp_eval(ws, bag_count_js, 12)
            await cdp_screenshot(ws, "/tmp/grubhub-after-add.png")

            print(json.dumps({{
                "status": "added",
                "item": item_name,
                "bag_count": bag_count,
                "screenshot": "/tmp/grubhub-after-add.png"
            }}))
        else:
            print(json.dumps({{
                "status": "no_add_button",
                "item": item_name,
                "buttons": page_state.get("all_buttons", []),
                "screenshot": "/tmp/grubhub-item-detail.png",
                "hint": "Item may need customization or the page layout is different"
            }}))
'''
    return _cdp_run(script)


def cmd_view_cart(cookies, args):
    """View current cart in browser."""
    script = '''
        # Navigate to grubhub and open cart
        current_url = await cdp_url(ws)
        if "grubhub.com" not in (current_url or ""):
            await cdp_navigate(ws, "https://www.grubhub.com")

        # Click the bag icon
        bag_js = """
        (function() {
            const els = [...document.querySelectorAll('button, a, [role="button"]')];
            const bag = els.find(el => {
                const r = el.getBoundingClientRect();
                return r.top < 60 && r.right > window.innerWidth - 100 && r.width > 20 && r.width < 100;
            });
            if (bag) {
                const r = bag.getBoundingClientRect();
                return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
            }
            return JSON.stringify({error: 'bag icon not found'});
        })()
        """
        bag_info = json.loads(await cdp_eval(ws, bag_js, 10))
        if "x" in bag_info:
            await cdp_click(ws, bag_info["x"], bag_info["y"])
            await asyncio.sleep(2)

        await cdp_screenshot(ws, "/tmp/grubhub-cart-view.png")
        print(json.dumps({"status": "ok", "screenshot": "/tmp/grubhub-cart-view.png"}))
'''
    return _cdp_run(script)


def cmd_checkout_preview(cookies, args):
    """Navigate to checkout page and screenshot it. Does NOT place order."""
    script = '''
        # Find and click "Proceed to Checkout"
        current_url = await cdp_url(ws)
        if "grubhub.com" not in (current_url or ""):
            await cdp_navigate(ws, "https://www.grubhub.com")
            await asyncio.sleep(2)

        # First JS click to open the cart sidebar
        checkout_js1 = """
        (function() {
            const btns = [...document.querySelectorAll('button, a')];
            const checkout = btns.find(b => b.textContent.includes('Proceed to Checkout'));
            if (checkout) {
                checkout.click();
                return JSON.stringify({clicked: 'js'});
            }
            return JSON.stringify({error: 'No Proceed to Checkout button found'});
        })()
        """
        click_result = json.loads(await cdp_eval(ws, checkout_js1, 10))
        if "error" in click_result:
            print(json.dumps(click_result))
            return

        await asyncio.sleep(2)

        # Now find the VISIBLE "Proceed to Checkout" button and real-click it
        checkout_js2 = """
        (function() {
            const btns = [...document.querySelectorAll('button, a')];
            const visible = btns.filter(b => {
                const r = b.getBoundingClientRect();
                return b.textContent.includes('Proceed to Checkout') && r.width > 50 && r.top >= 0 && r.top < window.innerHeight;
            });
            if (visible.length > 0) {
                const r = visible[0].getBoundingClientRect();
                return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
            }
            return JSON.stringify({error: 'No visible checkout button'});
        })()
        """
        btn_info = json.loads(await cdp_eval(ws, checkout_js2, 11))
        if "x" in btn_info:
            await cdp_click(ws, btn_info["x"], btn_info["y"])
        else:
            print(json.dumps(btn_info))
            return

        await asyncio.sleep(5)
        checkout_url = await cdp_url(ws)
        await cdp_screenshot(ws, "/tmp/grubhub-checkout-preview.png")

        # Scroll down to see payment section
        await cdp_eval(ws, "window.scrollBy(0, 400)", 11)
        await asyncio.sleep(1)
        await cdp_screenshot(ws, "/tmp/grubhub-checkout-payment.png")

        print(json.dumps({
            "status": "checkout_preview",
            "url": checkout_url,
            "screenshots": ["/tmp/grubhub-checkout-preview.png", "/tmp/grubhub-checkout-payment.png"],
            "note": "Review screenshots. User must click Place Order themselves."
        }))
'''
    return _cdp_run(script)


def cmd_clear_cart(cookies, args):
    """Clear the cart via API."""
    if not cookies:
        return {"error": "No auth cookies"}

    # Get current carts
    result = api_request("/carts", cookies=cookies)
    if "error" in result:
        return result

    carts = result.get("carts", {})
    if not carts:
        return {"status": "already_empty"}

    deleted = []
    for cart_id in carts:
        del_result = api_request(f"/carts/{cart_id}", method="DELETE", cookies=cookies)
        deleted.append(cart_id)

    return {"status": "cleared", "deleted_carts": deleted}


COMMANDS = {
    "whoami": cmd_whoami,
    "search": cmd_search,
    "menu": cmd_menu,
    "history": cmd_history,
    "favorites": cmd_favorites,
    "offers": cmd_offers,
    "ratings": cmd_ratings,
    "auth": cmd_auth,
    "refresh": cmd_refresh,
    "set-cookies": cmd_set_cookies,
    "set-location": cmd_set_location,
    "add-to-cart": cmd_add_to_cart,
    "view-cart": cmd_view_cart,
    "checkout-preview": cmd_checkout_preview,
    "clear-cart": cmd_clear_cart,
}


def format_table(data, indent=0):
    """Simple table formatter for --format table output."""
    prefix = "  " * indent
    if isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                print(f"{prefix}[{i}]")
                format_table(item, indent + 1)
            else:
                print(f"{prefix}- {item}")
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                print(f"{prefix}{k}:")
                format_table(v, indent + 1)
            else:
                print(f"{prefix}{k}: {v}")
    else:
        print(f"{prefix}{data}")


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print("Usage: grubhub-cli.py <command> [args...] [--format table]")
        print(f"\nCommands: {', '.join(COMMANDS.keys())}")
        print("\nOptions:")
        print("  --format table    Human-readable output (default: JSON)")
        sys.exit(0)

    # Parse --format flag
    output_format = "json"
    if "--format" in args:
        idx = args.index("--format")
        if idx + 1 < len(args):
            output_format = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    command = args[0]
    cmd_args = args[1:]

    if command not in COMMANDS:
        print(json.dumps({"error": f"Unknown command: {command}", "available": list(COMMANDS.keys())}))
        sys.exit(1)

    # Get cookies (not required for auth/refresh/set-cookies)
    cookies = get_cookies()
    if not cookies and command not in ("auth", "refresh", "set-cookies"):
        print(json.dumps({
            "error": "No auth cookies available",
            "instructions": "Run one of:\n"
                           "  1. grubhub-cli.py refresh  (extract from live Playwright browser)\n"
                           "  2. grubhub-cli.py auth     (check status)\n"
                           "  3. grubhub-cli.py set-cookies '{...}'  (manual)",
        }))
        sys.exit(1)

    result = COMMANDS[command](cookies, cmd_args)

    if output_format == "table":
        format_table(result)
    else:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

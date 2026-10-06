#!/usr/bin/env python3
"""Qantas Marketplace bonus-points deal monitor.

Reads the Marketplace "Bonus Points" listing every few minutes, works out the
cost per Qantas Point for every in-stock item, and sends a phone push (via
ntfy) the moment one drops to or below your threshold.

Cost per point = price / (bonus points + normal points earned on the price).
The $99 earbuds with 15,000 bonus points were about 0.65 cents a point.

Settings (environment variables):
  NTFY_TOPIC            your private ntfy topic name (required for pushes)
  MAX_CENTS_PER_POINT   alert at or below this cost per point (default 1.0)
  CHECK_EVERY_SECONDS   time between checks (default 180)
  RUN_MINUTES           how long one run keeps looping (default 330)

Uses only the Python standard library.
"""
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

LISTING_URL = os.environ.get(
    "LISTING_URL", "https://marketplace.qantas.com/au/c/shop{page}?BonusPoints=1"
)
PRODUCT_URL = "https://marketplace.qantas.com/au/p/{slug}/{key}"
USER_AGENT = "Mozilla/5.0 (compatible; personal-deal-monitor/1.0)"

MAX_CPP = float(os.environ.get("MAX_CENTS_PER_POINT") or 1.0)
INTERVAL = max(30, int(os.environ.get("CHECK_EVERY_SECONDS") or 180))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 330)
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
STATE_FILE = os.environ.get("STATE_FILE") or "state.json"

PAGE_SIZE = 24          # products per listing page
MAX_PAGES = 5
FAILS_BEFORE_WARNING = 3
WARNING_GAP_HOURS = 12  # at most one "monitor is broken" push per this period
MAX_ALERTS_PER_CHECK = 5


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), msg, flush=True)


# ---------------------------------------------------------------- fetching

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


# ----------------------------------------------------------------- parsing

_PUSH = 'self.__next_f.push([1,"'
_DECODER = json.JSONDecoder()


def page_data(html):
    """Join the data the page ships to the browser in its script chunks."""
    parts, pos = [], 0
    while True:
        i = html.find(_PUSH, pos)
        if i < 0:
            break
        start = i + len(_PUSH) - 1          # the opening quote
        j = start + 1
        while True:                         # find the closing, unescaped quote
            j = html.find('"', j)
            if j < 0:
                break
            k, slashes = j - 1, 0
            while html[k] == "\\":
                slashes += 1
                k -= 1
            if slashes % 2 == 0:
                break
            j += 1
        if j < 0:
            break
        try:
            parts.append(json.loads(html[start:j + 1]))
        except ValueError:
            pass
        pos = j + 1
    return "".join(parts)


def find_products(html):
    """Return {product key: product} or None if the page could not be read."""
    data = page_data(html)
    needle, pos, readable, products = '"products":[', 0, False, {}
    while True:
        i = data.find(needle, pos)
        if i < 0:
            break
        pos = i + len(needle)
        try:
            items, _ = _DECODER.raw_decode(data, pos - 1)
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        if not items:
            readable = True
            continue
        for p in items:
            if isinstance(p, dict) and p.get("key") and isinstance(p.get("variants"), list):
                readable = True
                products.setdefault(p["key"], p)
    return products if readable else None


def fetch_all_products():
    """Fetch every page of the bonus-points listing. Raises if page 1 fails."""
    paged = "{page}" in LISTING_URL
    html = fetch(LISTING_URL.format(page="") if paged else LISTING_URL)
    products = find_products(html)
    if products is None:
        raise ValueError("page loaded but the product data was not where expected")
    new_on_last_page, page = len(products), 1
    while paged and new_on_last_page >= PAGE_SIZE and page < MAX_PAGES:
        page += 1
        try:
            more = find_products(fetch(LISTING_URL.format(page="/page-%d" % page))) or {}
        except Exception:
            break
        fresh = {k: v for k, v in more.items() if k not in products}
        products.update(fresh)
        new_on_last_page = len(fresh)
    return products


# -------------------------------------------------------------- deal maths

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bonus_offers(products):
    """One entry per product that has an in-stock variant with bonus points,
    using that product's cheapest cost per point."""
    offers = []
    for key, p in products.items():
        if p.get("isOutOfStock"):
            continue
        best, colours = None, []
        for v in p.get("variants") or []:
            if not isinstance(v, dict) or not v.get("available"):
                continue
            price = v.get("currentPrice") or {}
            cash = number((price.get("cashPrice") or {}).get("amount"))
            bonus = number(price.get("bonusPoint"))
            if cash <= 0 or bonus <= 0:
                continue
            points = bonus + number(price.get("pointsEarnConversion")) * cash
            attrs = v.get("allAttributes") or {}
            offer = {
                "key": key,
                "name": p.get("name") or key,
                "brand": (attrs.get("brand") or {}).get("label") or "",
                "url": PRODUCT_URL.format(slug=p.get("slug") or "", key=key),
                "cash": cash,
                "bonus": int(bonus),
                "points": int(points),
                "cpp": cash / points * 100,
                "needs_points": int(number(price.get("minimumPointsToSpend")))
                if price.get("paymentOption") not in (None, "fullCashAllowed") else 0,
            }
            if attrs.get("colour"):
                colours.append(str(attrs["colour"]))
            if best is None or offer["cpp"] < best["cpp"]:
                best = offer
        if best:
            best["colours"] = colours
            offers.append(best)
    return sorted(offers, key=lambda o: o["cpp"])


def money(amount):
    return "${:,.0f}".format(amount) if amount == int(amount) else "${:,.2f}".format(amount)


def describe(o):
    name = (o["brand"] + " " + o["name"]).strip()
    text = "{} - {} gets {:,} points ({:,} bonus).".format(name, money(o["cash"]), o["points"], o["bonus"])
    if o["colours"]:
        text += " In stock: " + ", ".join(o["colours"]) + "."
    if o["needs_points"]:
        text += " Needs {:,} points as part payment.".format(o["needs_points"])
    if o["cash"] < 300:
        text += " Delivery is extra under $300."
    return text


# ----------------------------------------------------------- notifications

def push(title, message, click=None, priority=3, tags=None):
    if not NTFY_TOPIC:
        log("(no NTFY_TOPIC set, not sent) {} | {}".format(title, message))
        return False
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(
        NTFY_SERVER + "/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as exc:                 # never let a failed push stop the loop
        log("push failed: {}".format(exc))
        return False


# ------------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    state.setdefault("live", [])            # product keys already alerted and still live
    state.setdefault("last_warning", 0)
    state.setdefault("down", False)
    return state


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


# ------------------------------------------------------------------- check

def check(state, fails, announce=False):
    """Run one check. Returns the new count of consecutive failures."""
    try:
        products = fetch_all_products()
    except Exception as exc:
        reason = "HTTP {}".format(exc.code) if isinstance(exc, urllib.error.HTTPError) else str(exc)
        fails += 1
        log("check failed ({} in a row): {}".format(fails, reason))
        overdue = time.time() - state["last_warning"] > WARNING_GAP_HOURS * 3600
        if announce or (fails >= FAILS_BEFORE_WARNING and overdue):
            if push("Qantas monitor can't read the page",
                    "Reason: {}. Deals will not be detected until this clears.".format(reason),
                    priority=3, tags=["warning"]):
                state["last_warning"] = time.time()
                state["down"] = True
        return fails

    offers = bonus_offers(products)
    deals = [o for o in offers if o["cpp"] <= MAX_CPP]
    best = offers[0] if offers else None
    log("ok: {} bonus-point items, best {}, {} at or under {:g}c".format(
        len(offers), "{:.2f}c/pt".format(best["cpp"]) if best else "n/a", len(deals), MAX_CPP))

    if state["down"]:
        push("Qantas monitor is working again", "The page is readable again.", tags=["white_check_mark"])
        state["down"] = False

    already = set(state["live"])
    fresh = [o for o in deals if o["key"] not in already]
    for o in fresh[:MAX_ALERTS_PER_CHECK]:
        push("Qantas deal: {:.2f}c per point".format(o["cpp"]), describe(o) + " Tap to open.",
             click=o["url"], priority=5, tags=["rotating_light"])
        log("ALERT {:.2f}c/pt {}".format(o["cpp"], o["url"]))
    if len(fresh) > MAX_ALERTS_PER_CHECK:
        push("Qantas: {} more deals".format(len(fresh) - MAX_ALERTS_PER_CHECK),
             "More items are at or under {:g}c per point. Tap to see the list.".format(MAX_CPP),
             click=LISTING_URL.format(page="") if "{page}" in LISTING_URL else LISTING_URL,
             priority=5, tags=["rotating_light"])
    state["live"] = sorted(o["key"] for o in deals)   # a deal that leaves and returns alerts again

    if announce:
        summary = "Watching {} bonus-point items, checking every {} seconds. ".format(len(offers), INTERVAL)
        if best:
            summary += "Best right now: {} at {:.2f}c per point. ".format(best["name"], best["cpp"])
        summary += "You get an urgent alert at {:g}c per point or less.".format(MAX_CPP)
        push("Qantas monitor is running", summary, tags=["white_check_mark"])
    return 0


def main():
    once = "--once" in sys.argv
    announce = "--announce" in sys.argv or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state = load_state()
    save_state(state)                       # make sure the file exists
    deadline = time.time() + RUN_MINUTES * 60
    fails = 0
    while True:
        fails = check(state, fails, announce)
        save_state(state)
        announce = False
        nap = INTERVAL + random.uniform(-10, 10)
        if once or time.time() + nap >= deadline:
            break
        time.sleep(nap)


if __name__ == "__main__":
    main()

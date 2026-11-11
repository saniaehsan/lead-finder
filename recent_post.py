"""Google Maps "From the owner" post ki age (e.g. "3 days ago") nikalta hai.

Bina kisi API key ke: Playwright se har business ka Maps page browser me
khulta hai aur page ka text parha jata hai.

Streamlit ke andar se seedha Playwright chalana (especially Windows par)
asyncio ke masle deta hai, isliye scraper.py is file ko ALAG PROCESS me
chalata hai:   python recent_post.py   (JSON stdin -> JSON stdout)

Input : [{"id": "...", "link": "https://www.google.com/maps/..."}, ...]
Output: {"<id>": "3 days ago" | "No posts" | "N/A", ...}
"""
import json
import random
import re
import sys

AGO_RE = re.compile(
    r"\b(?:\d+|an?)\s+(?:minute|hour|day|week|month|year)s?\s+ago\b",
    re.I
)

OWNER_MARKERS = ("from the owner", "updates from the owner")


def extract_recent_post(page_text):
    """Page text -> '3 days ago' / 'No posts' / 'N/A'.

    Sirf "From the owner" section ke baad wala pehla '... ago' liya
    jata hai (reviews me bhi '... ago' hota hai, unhe ignore karna hai).
    """
    low = page_text.lower()
    start = -1

    for marker in OWNER_MARKERS:
        start = low.find(marker)
        if start != -1:
            break

    if start == -1:
        return "No posts"

    # Owner card chhota hota hai; agle section tak ki hi limit
    chunk = page_text[start:start + 500]

    # Agar chunk me "Photos & videos" / "Reviews" pehle aa jaye to
    # wahan se kaat do taa ke review ki date na uth jaye.
    for stop in ("photos & videos", "photos and videos", "reviews", "about"):
        i = chunk.lower().find(stop, 20)
        if i != -1:
            chunk = chunk[:i]

    m = AGO_RE.search(chunk)
    return m.group(0) if m else "N/A"


def _dismiss_consent(page):
    for label in ("Accept all", "Reject all", "I agree"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(1000)
                return
        except Exception:
            pass


def check_one(page, link):
    sep = "&" if "?" in link else "?"
    page.goto(f"{link}{sep}hl=en", wait_until="domcontentloaded",
              timeout=30000)
    _dismiss_consent(page)

    try:
        page.wait_for_selector("h1", timeout=15000)
    except Exception:
        return "N/A"            # page load nahi hua / captcha

    page.wait_for_timeout(2000)

    # Side panel ko scroll karo taa ke "From the owner" load ho jaye
    page.mouse.move(250, 600)
    for _ in range(3):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(600)

    text = page.inner_text("body")

    if "unusual traffic" in text.lower():
        return "N/A"

    return extract_recent_post(text)


def main():
    from playwright.sync_api import sync_playwright

    items = json.load(sys.stdin)
    out = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        page = context.new_page()

        for it in items:
            try:
                out[it["id"]] = check_one(page, it["link"])
            except Exception:
                out[it["id"]] = "N/A"
            page.wait_for_timeout(int(random.uniform(1000, 2500)))

        browser.close()

    print(json.dumps(out))


if __name__ == "__main__":
    main()

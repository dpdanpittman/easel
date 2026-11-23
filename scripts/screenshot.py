#!/usr/bin/env python3
"""Take the README screenshot: headless Chromium (Playwright) at 1920x1080 against a running scripts/demo.py.

    python3 scripts/demo.py --port 8099 &
    python3 scripts/screenshot.py [--url http://127.0.0.1:8099/] [--out docs/screenshot.png]

Needs `pip install playwright && playwright install chromium`. Waits for the panels to fill, then saves one PNG.
"""
import argparse
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099/")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshot.png"))
    ap.add_argument("--wait", type=float, default=6.0, help="seconds to let the polls and the scene settle")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(args.url)
        page.wait_for_selector("#tasklist li", timeout=20000)
        time.sleep(args.wait)
        page.screenshot(path=args.out)
        browser.close()
    print("saved", args.out)


if __name__ == "__main__":
    main()

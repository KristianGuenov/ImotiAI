#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://realistimo.com/bg/buy/sofia-oblast-bg/?addressIds%5B0%5D=4668&currency=EUR"

def main() -> int:
    url = os.getenv("REALISTIMO_BOOTSTRAP_URL") or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)

    # Where to save Playwright storage_state (cookies/localStorage)
    state_path = os.getenv("REALISTIMO_STORAGE_STATE")
    if not state_path:
        d = os.getenv("REALISTIMO_PROFILE_DIR") or "./data/realistimo-profile"
        Path(d).mkdir(parents=True, exist_ok=True)
        state_path = str(Path(d) / "storage_state.json")

    print(f"[realistimo bootstrap] URL: {url}")
    print(f"[realistimo bootstrap] Saving storage_state to: {state_path}")
    print("")
    print("1) A browser window will open.")
    print("2) If Cloudflare/Turnstile appears, solve it normally.")
    print("3) When listings are visible, this script will save cookies and exit.")
    print("")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            user_agent=os.getenv('REALISTIMO_USER_AGENT', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
            locale=os.getenv('REALISTIMO_LOCALE','bg-BG'),
            extra_http_headers={'Accept-Language': os.getenv('REALISTIMO_ACCEPT_LANGUAGE','bg-BG,bg;q=0.9,en-US;q=0.8,en;q=0.7')},
            viewport={"width": 1365, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)

        # Realistimo sometimes renders listings only after a scroll.
        try:
            page.mouse.wheel(0, 900)
        except Exception:
            page.evaluate("window.scrollBy(0, 900)")
        page.wait_for_timeout(800)

        # Wait until at least one listing link is visible.
        page.wait_for_selector("a[href*='offer-']", timeout=600_000)  # 10 minutes

        ctx.storage_state(path=state_path)
        print(f"[realistimo bootstrap] OK - saved: {state_path}")

        browser.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

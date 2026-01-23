# file: scrapers/pagination_click.py
#
# Implements "Option A" click-based pagination for SPA sites like imoteka.bg:
# - NEVER follows href for "next"
# - ALWAYS clicks the right-arrow control (next button)
# - Waits for the results to change (DOM fingerprint), not URL
# - Recovers if a mis-click dumps you on the homepage (/)
#
# Drop-in usage:
#   paginator = ClickPaginator(page, cfg)
#   while True:
#       items = await extract_items(page)
#       ...
#       ok = await paginator.next_page()
#       if not ok:
#           break
#
# Required cfg keys (example):
#   cfg = {
#     "start_url": "https://imoteka.bg/sale",
#     "next_click_selector": "nav[aria-label*='Pagination'] button:last-child",  # set to the actual '>' button
#     "results_fingerprint_selector": ".listing-card a",  # selector that matches listing links/cards
#     "wait_after_click_ms": 8000,
#     "timeout_ms": 15000,
#     "home_recover_url": "https://imoteka.bg/sale",
#     "home_urls": ["https://imoteka.bg", "https://imoteka.bg/"],
#   }
#
# Notes:
# - You MUST set next_click_selector to the actual pager ">" control for Imoteka.
# - results_fingerprint_selector should match the listings on the page (anchors/cards). The code uses the first few.
#
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError


@dataclass
class ClickPaginationConfig:
    start_url: str

    # Selector that clicks the ">" control (right arrow) in the pager.
    next_click_selector: str

    # Selector used to fingerprint the visible results list.
    # Should match listing anchors/cards in DOM order.
    results_fingerprint_selector: str

    # Optional: if you want to scope selectors to a container:
    pagination_container_selector: Optional[str] = None

    # How long to sleep after a click (helps SPA settle / similar to your 8000ms approach).
    wait_after_click_ms: int = 8000

    # Max time to wait for results to change after clicking next.
    timeout_ms: int = 15000

    # If a bad click sends you to home, recover by navigating here and retrying once.
    home_recover_url: Optional[str] = None

    # What URLs count as "home" (used for recovery).
    home_urls: Tuple[str, ...] = ("https://imoteka.bg", "https://imoteka.bg/")

    # When the fingerprint can’t be collected, fallback to container text
    fallback_container_selector: str = "body"

    # How many result elements to sample for fingerprinting.
    fingerprint_sample: int = 5


class ClickPaginator:
    """
    Click-based paginator for SPA pagination bars (like Imoteka).

    Guarantees:
      - Does not follow href
      - Clicks next control
      - Waits for results to change by fingerprint
      - Home recovery if navigation lands on /
    """

    def __init__(self, page: Page, cfg: ClickPaginationConfig):
        self.page = page
        self.cfg = cfg
        self._click_attempts = 0

    async def _fingerprint(self) -> str:
        """
        Create a robust fingerprint of the current results.
        Prefer hrefs; fallback to text.
        """
        sel = self.cfg.results_fingerprint_selector
        loc = self.page.locator(sel)

        try:
            count = await loc.count()
        except Exception:
            count = 0

        # Sample first N elements
        sample_n = min(max(self.cfg.fingerprint_sample, 1), count)

        if sample_n > 0:
            parts: list[str] = []
            for i in range(sample_n):
                el = loc.nth(i)
                # Prefer href if available
                href = None
                try:
                    href = await el.get_attribute("href")
                except Exception:
                    href = None
                if href:
                    parts.append(f"href:{href}")
                    continue

                # Otherwise text
                try:
                    txt = (await el.inner_text())[:200]
                except Exception:
                    txt = ""
                parts.append(f"txt:{txt}")

            return "|".join(parts)

        # Fallback: container snapshot (short)
        try:
            snap = (await self.page.locator(self.cfg.fallback_container_selector).inner_text())[:1000]
        except Exception:
            snap = ""
        return f"fallback:{snap}"

    def _next_locator(self):
        if self.cfg.pagination_container_selector:
            return self.page.locator(self.cfg.pagination_container_selector).locator(self.cfg.next_click_selector)
        return self.page.locator(self.cfg.next_click_selector)

    async def _maybe_recover_from_home(self) -> bool:
        """
        If we landed on the homepage, recover and return True.
        """
        cur = (self.page.url or "").rstrip("/") + "/"
        home_set = {u.rstrip("/") + "/" for u in self.cfg.home_urls}
        if cur in home_set:
            recover = self.cfg.home_recover_url or self.cfg.start_url
            await self.page.goto(recover, wait_until="domcontentloaded")
            # Give SPA time to render listings
            await self.page.wait_for_timeout(self.cfg.wait_after_click_ms)
            return True
        return False

    async def next_page(self) -> bool:
        """
        Click the pager ">" once and wait until results change.
        Returns False if we can't advance (no change / click fails / timeout).
        """
        before = await self._fingerprint()

        next_btn = self._next_locator()
        try:
            # Ensure the control exists and is interactable
            await next_btn.wait_for(state="visible", timeout=self.cfg.timeout_ms)
        except PlaywrightTimeoutError:
            return False

        # Try click -> wait change. If sent to home, recover & retry once.
        for attempt in range(2):
            try:
                # IMPORTANT: click the element (do not read href / goto)
                await next_btn.click()
            except Exception:
                # Sometimes an overlay blocks; try a small scroll and retry click once
                try:
                    await self.page.mouse.wheel(0, 400)
                    await asyncio.sleep(0.2)
                    await next_btn.click()
                except Exception:
                    return False

            # Let SPA settle (your proven approach)
            await self.page.wait_for_timeout(self.cfg.wait_after_click_ms)

            # Home recovery guard
            if await self._maybe_recover_from_home():
                if attempt == 0:
                    # Re-acquire locator after navigation
                    next_btn = self._next_locator()
                    continue
                return False

            # Wait for fingerprint to change (polling)
            try:
                await self._wait_for_fingerprint_change(before, timeout_ms=self.cfg.timeout_ms)
                return True
            except PlaywrightTimeoutError:
                # No change. If first attempt, do not blindly loop; just one retry max.
                if attempt == 0:
                    continue
                return False

        return False

    async def _wait_for_fingerprint_change(self, before: str, timeout_ms: int) -> None:
        """
        Poll fingerprint until it changes or timeout.
        """
        deadline = self.page.context._connection._loop.time() + (timeout_ms / 1000.0)
        while True:
            now = self.page.context._connection._loop.time()
            if now >= deadline:
                raise PlaywrightTimeoutError("Timed out waiting for results fingerprint to change")

            after = await self._fingerprint()
            if after and after != before:
                return

            await asyncio.sleep(0.25)


# Convenience builder for dict-based configs
def build_click_pagination_config(d: dict) -> ClickPaginationConfig:
    return ClickPaginationConfig(
        start_url=d["start_url"],
        next_click_selector=d["next_click_selector"],
        results_fingerprint_selector=d["results_fingerprint_selector"],
        pagination_container_selector=d.get("pagination_container_selector"),
        wait_after_click_ms=int(d.get("wait_after_click_ms", 8000)),
        timeout_ms=int(d.get("timeout_ms", 15000)),
        home_recover_url=d.get("home_recover_url"),
        home_urls=tuple(d.get("home_urls", ("https://imoteka.bg", "https://imoteka.bg/"))),
        fallback_container_selector=d.get("fallback_container_selector", "body"),
        fingerprint_sample=int(d.get("fingerprint_sample", 5)),
    )

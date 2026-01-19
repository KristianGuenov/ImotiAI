import argparse
import asyncio
import json
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


# -----------------------------
# Data model
# -----------------------------

@dataclass(frozen=True)
class ScrapeTarget:
    """
    One URL target to scrape.

    mode:
      - "pagination": extract -> next -> repeat (max_pages)
      - "load_more_then_extract": load more (scroll) -> extract once
      - "extract_once": just extract current page once

    post_strategy:
      - "per_page": POST every page as a separate extraction (current behavior)
      - "per_target": merge all pages and POST once (fastest, avoids huge ID jumps)
    """
    name: str
    url: str
    mode: str = "extract_once"

    # pagination settings
    max_pages: int = 25
    delay_ms: int = 1500

    # load-more settings (passed directly to content.js LoadMore.run)
    load_more: Dict[str, Any] = field(default_factory=dict)

    # browser behavior
    wait_until: str = "domcontentloaded"
    timeout_ms: int = 45_000

    # posting behavior
    post_strategy: str = "per_page"


class TargetsLoader:
    """
    Loads ScrapeTarget entries from a YAML file.
    """
    def __init__(self, targets_file: str):
        self.targets_file = targets_file

    def load(self) -> List[ScrapeTarget]:
        with open(self.targets_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or []
        if not isinstance(raw, list):
            raise ValueError("targets.yml must contain a YAML list of targets")

        targets: List[ScrapeTarget] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"targets.yml entry #{i} must be a mapping/object")

            name = str(item.get("name") or f"target-{i}")
            url = str(item.get("url") or "").strip()
            if not url:
                raise ValueError(f"targets.yml entry '{name}' is missing url")

            targets.append(
                ScrapeTarget(
                    name=name,
                    url=url,
                    mode=str(item.get("mode") or "extract_once").strip(),
                    max_pages=int(item.get("max_pages") or 25),
                    delay_ms=int(item.get("delay_ms") or 1500),
                    load_more=dict(item.get("load_more") or {}),
                    wait_until=str(item.get("wait_until") or "domcontentloaded"),
                    timeout_ms=int(item.get("timeout_ms") or 45_000),
                    post_strategy=str(item.get("post_strategy") or "per_page").strip(),
                )
            )

        return targets


# -----------------------------
# API posting
# -----------------------------

class ApiClient:
    """
    Posts extracted payloads to your existing FastAPI endpoint:
      POST /api/v1/extractions
    with header:
      x-api-key: <API_KEY>
    """
    def __init__(self, endpoint: str, api_key: str, timeout_s: float = 60.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ApiClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post_extraction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized (use 'async with ApiClient(...)')")

        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key or "",
        }
        r = await self._client.post(self.endpoint, headers=headers, json=payload)
        if r.status_code >= 400:
            text = (r.text or "")[:800]
            raise RuntimeError(f"API error {r.status_code}: {text}")
        return r.json()


# -----------------------------
# Playwright scraping
# -----------------------------

class PlaywrightExtractor:
    """
    Opens pages with Playwright, injects your content.js,
    calls window.__imotiExtractor.*, and posts results to API.

    Enhancements:
      - pagination stop early if signature repeats
      - post_strategy:
          * per_page: POST each page
          * per_target: merge pages and POST once
      - avoids duplicate POSTs across retries using a shared posted-signature set
    """

    _BLOCKED_URL_PARTS = (
        "doubleclick.net",
        "googlesyndication.com",
        "google-analytics.com",
        "googletagmanager.com",
        "googletagservices.com",
        "securepubads.g.doubleclick.net",
        "adservice.google.",
        "/gpt/",
        "prebid",
        "adsystem",
        "taboola",
        "outbrain",
        "criteo",
        "scorecardresearch",
        "quantserve",
        "facebook.net/tr",
        "tiktok.com/i18n/pixel",
    )

    def __init__(
        self,
        content_script_path: str,
        headless: bool = True,
        user_agent: Optional[str] = None,
        log_console_noise: bool = False,
    ):
        self.content_script_path = content_script_path
        self.headless = headless
        self.user_agent = user_agent
        self.log_console_noise = log_console_noise
        self._script_source: Optional[str] = None

    def _load_content_script(self) -> str:
        if self._script_source is None:
            with open(self.content_script_path, "r", encoding="utf-8") as f:
                self._script_source = f.read()
        return self._script_source

    async def _new_context(self, browser: Browser) -> BrowserContext:
        ua = self.user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        ctx = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1365, "height": 900},
            java_script_enabled=True,
            locale="bg-BG",
            ignore_https_errors=True,
            bypass_csp=True,
        )

        # Inject extractor at document-start for every navigation
        script = self._load_content_script()
        await ctx.add_init_script(script=script)

        # Block noisy/slow ad & tracking requests
        async def _route_handler(route, request):
            url = (request.url or "").lower()
            if any(part in url for part in self._BLOCKED_URL_PARTS):
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", _route_handler)
        return ctx

    async def _ensure_extractor(self, page: Page) -> None:
        async def _has() -> bool:
            try:
                return bool(await page.evaluate("() => !!(window.__imotiExtractor && window.__imotiExtractor.run)"))
            except Exception:
                return False

        if await _has():
            return

        # Fallback injection
        script = self._load_content_script()
        try:
            await page.add_script_tag(content=script)
        except Exception:
            pass

        await page.wait_for_function(
            "() => !!(window.__imotiExtractor && window.__imotiExtractor.run)",
            timeout=12_000,
        )

    async def _extract(self, page: Page) -> Dict[str, Any]:
        await self._ensure_extractor(page)
        out = await page.evaluate("() => window.__imotiExtractor.run()")

        if not isinstance(out, dict) or not out.get("ok"):
            err = out.get("error") if isinstance(out, dict) else None
            raise RuntimeError(err or "Extraction failed (no ok:true)")

        result = out.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Extraction returned ok:true but missing result object")

        return json.loads(json.dumps(result, ensure_ascii=False))

    async def _navigate_next(self, page: Page) -> bool:
        await self._ensure_extractor(page)
        did = await page.evaluate("() => window.__imotiExtractor.navigateNext()")
        return bool(did)

    async def _load_more(self, page: Page, options: Dict[str, Any]) -> None:
        await self._ensure_extractor(page)
        await page.evaluate("(opts) => window.__imotiExtractor.loadMore(opts || {})", options or {})

    def _attach_debug_listeners(self, page: Page, target_name: str) -> None:
        page.on("pageerror", lambda exc: print(f"[pageerror] {target_name}: {exc}"))

        def _console_handler(msg):
            if self.log_console_noise and msg.type in ("error", "warning"):
                print(f"[console:{msg.type}] {target_name}: {msg.text}")

        page.on("console", _console_handler)

    def _page_signature(self, extracted_payload: Dict[str, Any], max_urls: int = 15) -> str:
        items = extracted_payload.get("items") if isinstance(extracted_payload, dict) else None
        if not isinstance(items, list) or not items:
            return "no-items"

        urls: List[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            u = it.get("url")
            if isinstance(u, str) and u:
                urls.append(u)
            if len(urls) >= max_urls:
                break

        return "|".join(urls) if urls else "no-urls"

    def _item_key(self, item: Dict[str, Any]) -> str:
        """
        Key for item dedupe when merging pages.
        Prefer URL, fall back to title+rawText.
        """
        u = item.get("url")
        if isinstance(u, str) and u:
            return f"url::{u}"
        t = item.get("title") or ""
        r = item.get("rawText") or ""
        return f"txt::{t}::{r[:120]}"

    async def scrape_target(
        self,
        api: ApiClient,
        target: ScrapeTarget,
        browser: Browser,
        posted_signatures: Optional[set] = None,
    ) -> Tuple[int, int, Optional[int]]:
        """
        Returns:
          (pages_visited, posts_done, last_extraction_id)
        """
        posted_signatures = posted_signatures if posted_signatures is not None else set()

        ctx = await self._new_context(browser)
        page = await ctx.new_page()
        self._attach_debug_listeners(page, target.name)

        pages_visited = 0
        posts_done = 0
        last_id: Optional[int] = None

        try:
            await page.goto(target.url, wait_until=target.wait_until, timeout=target.timeout_ms)
            await page.wait_for_timeout(700)
            await self._ensure_extractor(page)

            # Single page modes
            if target.mode == "extract_once":
                payload = await self._extract(page)
                sig = self._page_signature(payload)

                if sig not in posted_signatures:
                    resp = await api.post_extraction(payload)
                    last_id = resp.get("id") if isinstance(resp, dict) else last_id
                    posts_done += 1
                    posted_signatures.add(sig)

                pages_visited = 1
                return pages_visited, posts_done, last_id

            if target.mode == "load_more_then_extract":
                await self._load_more(page, target.load_more)
                await page.wait_for_timeout(int(max(300, min(5000, target.delay_ms))))
                payload = await self._extract(page)
                sig = self._page_signature(payload)

                if sig not in posted_signatures:
                    resp = await api.post_extraction(payload)
                    last_id = resp.get("id") if isinstance(resp, dict) else last_id
                    posts_done += 1
                    posted_signatures.add(sig)

                pages_visited = 1
                return pages_visited, posts_done, last_id

            # Pagination mode
            if target.mode == "pagination":
                max_pages = max(1, min(200, int(target.max_pages)))
                delay_ms = max(0, min(20_000, int(target.delay_ms)))

                # loop detection in THIS attempt
                seen_this_attempt: set = set()

                # merge buffers for per_target
                merged_payload: Optional[Dict[str, Any]] = None
                merged_items: Dict[str, Dict[str, Any]] = {}
                page_urls: List[str] = []

                post_strategy = (target.post_strategy or "per_page").lower().strip()
                if post_strategy not in ("per_page", "per_target"):
                    post_strategy = "per_page"

                for _ in range(max_pages):
                    payload = await self._extract(page)
                    pages_visited += 1

                    sig = self._page_signature(payload)
                    page_urls.append(page.url)

                    # Stop if we’re looping / last page
                    if sig in seen_this_attempt:
                        break
                    seen_this_attempt.add(sig)

                    # If we already posted/merged this signature (e.g., retry after crash),
                    # skip posting/merging but still try to navigate next.
                    if sig not in posted_signatures:
                        if post_strategy == "per_page":
                            resp = await api.post_extraction(payload)
                            last_id = resp.get("id") if isinstance(resp, dict) else last_id
                            posts_done += 1

                        else:  # per_target
                            if merged_payload is None:
                                merged_payload = payload
                                # make result represent the target, not just the current page
                                merged_payload["sourceUrl"] = target.url
                                merged_payload.setdefault("meta", {})
                                merged_payload["meta"]["targetName"] = target.name
                                merged_payload["meta"]["postStrategy"] = "per_target"

                            items = payload.get("items") if isinstance(payload, dict) else None
                            if isinstance(items, list):
                                for it in items:
                                    if isinstance(it, dict):
                                        merged_items[self._item_key(it)] = it

                        posted_signatures.add(sig)

                    did_nav = await self._navigate_next(page)
                    if not did_nav:
                        break

                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=target.timeout_ms)
                    except Exception:
                        pass
                    await page.wait_for_timeout(500)
                    await self._ensure_extractor(page)

                    if delay_ms:
                        jitter = random.randint(0, min(600, delay_ms))
                        await page.wait_for_timeout(delay_ms + jitter)

                # If per_target, POST once at the end
                if post_strategy == "per_target" and merged_payload is not None:
                    merged_payload["items"] = list(merged_items.values())
                    merged_payload.setdefault("meta", {})
                    merged_payload["meta"]["pageUrls"] = page_urls
                    merged_payload["meta"]["pagesVisited"] = pages_visited

                    resp = await api.post_extraction(merged_payload)
                    last_id = resp.get("id") if isinstance(resp, dict) else last_id
                    posts_done += 1

                return pages_visited, posts_done, last_id

            raise ValueError(f"Unknown mode '{target.mode}' for target '{target.name}'")

        finally:
            await ctx.close()






class ScrapeRunner:
    """
    Orchestrates loading targets and running them with a single browser instance.
    Includes relaunch/retry if Chromium crashes (TargetClosedError scenarios).
    """
    def __init__(
        self,
        targets_file: str,
        content_script_path: str,
        api_endpoint: str,
        api_key: str,
        headless: bool = True,
    ):
        self.targets_file = targets_file
        self.content_script_path = content_script_path
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.headless = headless

        self.loader = TargetsLoader(targets_file)
        self.extractor = PlaywrightExtractor(content_script_path=content_script_path, headless=headless)

        # ✅ keep signatures per target across retries in this run
        self._posted_sigs_by_target: Dict[str, set] = {}

    async def _launch_browser(self, p) -> Browser:
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        return await p.chromium.launch(headless=self.headless, args=args)

    async def run_once(self) -> int:
        targets = self.loader.load()
        if not targets:
            print("No targets found. (targets.yml is empty)")
            return 0

        async with ApiClient(self.api_endpoint, self.api_key) as api:
            async with async_playwright() as p:
                browser: Optional[Browser] = None
                try:
                    browser = await self._launch_browser(p)
                except Exception as e:
                    print(f"❌ Failed to launch browser: {e}")
                    return 0

                ok = 0
                try:
                    for t in targets:
                        print(f"\n=== {t.name} ===")
                        print(f"URL: {t.url}")
                        print(f"Mode: {t.mode} | post_strategy: {t.post_strategy}")

                        if browser is None or not browser.is_connected():
                            try:
                                browser = await self._launch_browser(p)
                            except Exception as e:
                                print(f"❌ Browser relaunch failed: {e}")
                                continue

                        posted = self._posted_sigs_by_target.setdefault(t.name, set())

                        try:
                            pages_visited, posts_done, last_id = await self.extractor.scrape_target(
                                api, t, browser, posted_signatures=posted
                            )
                            ok += 1
                            print(f"✅ pages_visited={pages_visited} posts_done={posts_done} last_extraction_id={last_id}")
                        except Exception as e:
                            msg = str(e)
                            if "Target page, context or browser has been closed" in msg:
                                print("⚠️ Browser closed/crashed. Relaunching and retrying once...")
                                try:
                                    try:
                                        await browser.close()
                                    except Exception:
                                        pass
                                    browser = await self._launch_browser(p)

                                    pages_visited, posts_done, last_id = await self.extractor.scrape_target(
                                        api, t, browser, posted_signatures=posted
                                    )
                                    ok += 1
                                    print(f"✅ (retry) pages_visited={pages_visited} posts_done={posts_done} last_extraction_id={last_id}")
                                except Exception as e2:
                                    print(f"❌ failed target '{t.name}' after retry: {e2}")
                            else:
                                print(f"❌ failed target '{t.name}': {e}")

                    return ok
                finally:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass




# -----------------------------
# CLI entrypoint
# -----------------------------

async def _amain(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Playwright scraper worker (posts to FastAPI /extractions).")
    parser.add_argument("--targets", default=os.getenv("TARGETS_FILE", "/app/targets.yml"))
    parser.add_argument("--content-script", default=os.getenv("CONTENT_SCRIPT_PATH", "/app/content.js"))
    parser.add_argument("--endpoint", default=os.getenv("API_ENDPOINT", "http://api:8787/api/v1/extractions"))
    parser.add_argument("--api-key", default=os.getenv("API_KEY", "dev-key-change-me"))
    parser.add_argument("--headful", action="store_true", help="Run with visible browser (debug)")
    args = parser.parse_args(argv)

    runner = ScrapeRunner(
        targets_file=args.targets,
        content_script_path=args.content_script,
        api_endpoint=args.endpoint,
        api_key=args.api_key,
        headless=not args.headful,
    )
    ok = await runner.run_once()
    return 0 if ok > 0 else 2


def main() -> None:
    try:
        code = asyncio.run(_amain(sys.argv[1:]))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()

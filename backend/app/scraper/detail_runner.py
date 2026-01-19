import argparse
import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


BLOCKED_URL_PARTS = (
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


class ApiClient:
    def __init__(self, api_base: str, api_key: str, timeout_s: float = 60.0):
        self.api_base = api_base.rstrip("/")
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

    def _auth_headers(self) -> Dict[str, str]:
        return {"x-api-key": self.api_key or ""}

    async def get_detail_queue(self, domain: str | None, url_contains: str | None, limit: int) -> List[str]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")

        params: Dict[str, Any] = {"limit": int(limit)}
        if domain:
            params["domain"] = domain
        if url_contains:
            params["url_contains"] = url_contains

        url = f"{self.api_base}/api/v1/detail-queue"
        r = await self._client.get(url, params=params, headers=self._auth_headers())
        r.raise_for_status()
        data = r.json()
        urls = data.get("urls")
        if not isinstance(urls, list):
            return []
        return [u for u in urls if isinstance(u, str) and u.startswith("http")]

    async def post_extraction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")
        url = f"{self.api_base}/api/v1/extractions"
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key or "",
        }
        r = await self._client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"API error {r.status_code}: {(r.text or '')[:800]}")
        return r.json()


class DetailExtractor:
    def __init__(self, detail_script_path: str):
        self.detail_script_path = detail_script_path
        self._script_source: Optional[str] = None

    def _load_script(self) -> str:
        if self._script_source is None:
            with open(self.detail_script_path, "r", encoding="utf-8") as f:
                self._script_source = f.read()
        return self._script_source

    async def new_context(self, browser: Browser) -> BrowserContext:
        ua = (
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

        script = self._load_script()
        await ctx.add_init_script(script=script)

        async def _route_handler(route, request):
            url = (request.url or "").lower()
            if any(part in url for part in BLOCKED_URL_PARTS):
                await route.abort()
                return
            await route.continue_()

        await ctx.route("**/*", _route_handler)
        return ctx

    async def ensure(self, page: Page) -> None:
        try:
            ok = await page.evaluate(
                "() => !!(window.__listingDetailExtractor && window.__listingDetailExtractor.extract)"
            )
            if ok:
                return
        except Exception:
            pass

        script = self._load_script()
        try:
            await page.add_script_tag(content=script)
        except Exception:
            pass

        await page.wait_for_function(
            "() => !!(window.__listingDetailExtractor && window.__listingDetailExtractor.extract)",
            timeout=10_000,
        )

    async def extract_description(self, page: Page) -> Tuple[Optional[str], Optional[str]]:
        await self.ensure(page)
        out = await page.evaluate("() => window.__listingDetailExtractor.extract()")
        if not isinstance(out, dict) or not out.get("ok"):
            raise RuntimeError("Detail extraction failed")
        desc = out.get("description")
        title = out.get("title")
        desc = desc.strip() if isinstance(desc, str) else None
        title = title.strip() if isinstance(title, str) else None
        return title, desc


def make_detail_payload(listing_url: str, title: Optional[str], description: Optional[str]) -> Dict[str, Any]:
    return {
        "dataVersion": 1,
        "sourceUrl": listing_url,
        "pageTitle": title,
        "extractedAt": _now_iso(),
        "meta": {
            "mode": "detail",
            "domain": _domain(listing_url),
            "fields": ["description"],
        },
        "items": [
            {
                "title": title,
                "url": listing_url,
                "image": None,
                "images": [],
                "texts": [],
                "rawText": description or "",
            }
        ],
    }


async def scrape_one(
    browser: Browser,
    extractor: DetailExtractor,
    api: ApiClient,
    url: str,
    timeout_ms: int,
) -> Optional[int]:
    ctx = await extractor.new_context(browser)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(500)

        title, desc = await extractor.extract_description(page)

        if not desc or len(desc) < 40:
            return None

        payload = make_detail_payload(url, title, desc)
        resp = await api.post_extraction(payload)
        rid = resp.get("id") if isinstance(resp, dict) else None
        return int(rid) if isinstance(rid, int) else None
    finally:
        await ctx.close()


async def main_async(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Detail runner: extract full listing description.")
    parser.add_argument("--api-base", default=os.getenv("API_BASE", "http://api:8787"))
    parser.add_argument("--api-key", default=os.getenv("API_KEY", "dev-key-change-me"))

    parser.add_argument("--queue-domain", default=os.getenv("DETAIL_QUEUE_DOMAIN", "imot.bg"))
    parser.add_argument("--queue-url-contains", default=os.getenv("DETAIL_QUEUE_URL_CONTAINS", "/obiava/"))
    parser.add_argument("--queue-limit", type=int, default=int(os.getenv("DETAIL_QUEUE_LIMIT", "200")))

    parser.add_argument("--concurrency", type=int, default=int(os.getenv("DETAIL_CONCURRENCY", "3")))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("DETAIL_TIMEOUT_MS", "45000")))
    parser.add_argument("--max-urls", type=int, default=int(os.getenv("DETAIL_MAX_URLS", "200")))

    parser.add_argument("--detail-script", default=os.getenv("DETAIL_SCRIPT_PATH", "/app/scraper/detail_extractor.js"))
    args = parser.parse_args(argv)

    async with ApiClient(args.api_base, args.api_key) as api:
        urls = await api.get_detail_queue(
            domain=args.queue_domain,
            url_contains=args.queue_url_contains,
            limit=args.queue_limit,
        )

    # dedupe + cap
    seen: Set[str] = set()
    final: List[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        final.append(u)
        if len(final) >= int(args.max_urls):
            break

    if not final:
        print("No listing URLs found (queue empty or filters too strict).")
        return 2

    concurrency = max(1, min(10, int(args.concurrency)))
    timeout_ms = max(5_000, min(120_000, int(args.timeout_ms)))

    extractor = DetailExtractor(args.detail_script)

    async with httpx.AsyncClient() as _:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

            sem = asyncio.Semaphore(concurrency)

            async def worker(u: str) -> Optional[int]:
                async with sem:
                    try:
                        async with ApiClient(args.api_base, args.api_key) as api2:
                            return await scrape_one(browser, extractor, api2, u, timeout_ms)
                    except Exception as e:
                        print(f"❌ detail failed: {u} -> {e}")
                        return None

            results = await asyncio.gather(*[asyncio.create_task(worker(u)) for u in final])
            await browser.close()

    ok = [r for r in results if isinstance(r, int)]
    print(f"✅ detail scraped: {len(ok)}/{len(final)}")
    if ok:
        print(f"last_extraction_id={ok[-1]}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async(os.sys.argv[1:])))


if __name__ == "__main__":
    main()

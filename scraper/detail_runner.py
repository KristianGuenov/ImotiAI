import argparse
import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import yaml
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


# Block obvious ad/analytics endpoints; KEEP images (you want them in index runs).
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


async def _auto_scroll(
    page: Page, max_steps: int = 12, step_delay_ms: int = 350
) -> None:
    """Best-effort auto-scroll to trigger lazy-loaded content (e.g., image galleries)."""
    try:
        await page.evaluate(
            """async (maxSteps, delayMs) => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                let lastH = -1;
                for (let i = 0; i < maxSteps; i++) {
                    window.scrollTo(0, document.body.scrollHeight);
                    await sleep(delayMs);
                    const h = document.body.scrollHeight;
                    if (h === lastH) break;
                    lastH = h;
                }
                window.scrollTo(0, 0);
            }""",
            max_steps,
            step_delay_ms,
        )
    except Exception:
        return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _nonempty_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _structured_signal_count(value: Any) -> int:
    """Count non-empty structured evidence without judging whether values are plausible."""
    if isinstance(value, dict):
        return 1 if any(v not in (None, "", [], {}) for v in value.values()) else 0
    if isinstance(value, list):
        return sum(1 for item in value if item not in (None, "", [], {}))
    return 1 if value not in (None, "", [], {}) else 0


def has_meaningful_detail(extracted: Dict[str, Any], min_desc_len: int) -> Tuple[bool, str]:
    """
    Decide whether extraction succeeded strongly enough to mark detail_done later.

    This is intentionally liberal:
    - images are optional and do NOT decide success;
    - prices/areas/rooms are not sanity-checked here;
    - useful text OR structured raw evidence is enough.
    """
    desc = _nonempty_text(extracted.get("description"))
    text_blocks = extracted.get("raw_text_blocks") or []

    # A normal description is sufficient. Keep the threshold small; the purpose is
    # to reject empty/block/error pages, not poor-quality property advertisements.
    desc_threshold = max(20, min(int(min_desc_len or 0), 30))
    if len(desc) >= desc_threshold:
        return True, f"description:{len(desc)}"

    # Some sites expose the useful body as text blocks rather than description.
    if isinstance(text_blocks, list):
        block_text_len = 0
        for block in text_blocks:
            if isinstance(block, str):
                block_text_len += len(block.strip())
            elif isinstance(block, dict):
                for key in ("text", "value", "content", "label"):
                    value = block.get(key)
                    if isinstance(value, str):
                        block_text_len += len(value.strip())
        if block_text_len >= 30:
            return True, f"raw_text_blocks:{block_text_len}"

    structured_count = sum(
        _structured_signal_count(extracted.get(key))
        for key in ("raw_kv", "raw_jsonld", "raw_state_blobs")
    )
    if structured_count > 0:
        return True, f"structured:{structured_count}"

    # A shorter description can still be useful when accompanied by listing-specific
    # contacts/signals, but images alone never make a scrape successful.
    secondary_count = (
        _structured_signal_count(extracted.get("raw_contacts"))
        + _structured_signal_count(extracted.get("signals"))
    )
    if len(desc) >= 10 and secondary_count > 0:
        return True, f"short_description:{len(desc)}+signals:{secondary_count}"

    return False, f"insufficient_content:description={len(desc)},structured={structured_count},secondary={secondary_count}"


@dataclass
class DomainRule:
    domain: str
    concurrency: int
    min_desc_len: int
    wait_until: str
    timeout_ms: int
    extractor_script: str
    include_any: List[str]
    exclude_any: List[str]
    include_regex: List[re.Pattern]
    exclude_regex: List[re.Pattern]

    def matches_host(self, host: str) -> bool:
        host = (host or "").lower()
        d = (self.domain or "").lower().lstrip(".")
        return host == d or host.endswith("." + d)

    def allows_url(self, url: str) -> bool:
        u = (url or "").lower()

        for s in self.exclude_any:
            if s and s.lower() in u:
                return False
        for rx in self.exclude_regex:
            if rx.search(u):
                return False

        if self.include_any or self.include_regex:
            ok = False
            for s in self.include_any:
                if s and s.lower() in u:
                    ok = True
                    break
            if not ok:
                for rx in self.include_regex:
                    if rx.search(u):
                        ok = True
                        break
            return ok

        return True


def load_rules(path: str) -> Tuple[DomainRule, List[DomainRule]]:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    defaults = raw.get("defaults") or {}

    def _mk_rule(domain: str, dct: Dict[str, Any]) -> DomainRule:
        inc_any = list(dct.get("include_any") or [])
        exc_any = list(dct.get("exclude_any") or [])
        inc_rx = [
            re.compile(p, re.I)
            for p in (dct.get("include_regex") or [])
            if isinstance(p, str) and p
        ]
        exc_rx = [
            re.compile(p, re.I)
            for p in (dct.get("exclude_regex") or [])
            if isinstance(p, str) and p
        ]

        return DomainRule(
            domain=domain,
            concurrency=max(1, min(10, _safe_int(dct.get("concurrency"), 2))),
            min_desc_len=max(0, _safe_int(dct.get("min_desc_len"), 60)),
            wait_until=str(dct.get("wait_until") or "domcontentloaded"),
            timeout_ms=max(
                5_000, min(180_000, _safe_int(dct.get("timeout_ms"), 45_000))
            ),
            extractor_script=str(
                dct.get("extractor_script") or "/app/scraper/detail_extractor.js"
            ),
            include_any=[str(x) for x in inc_any if isinstance(x, (str, int, float))],
            exclude_any=[str(x) for x in exc_any if isinstance(x, (str, int, float))],
            include_regex=inc_rx,
            exclude_regex=exc_rx,
        )

    default_rule = _mk_rule("*", defaults)

    rules: List[DomainRule] = []
    for r in raw.get("domains") or []:
        if not isinstance(r, dict):
            continue
        dom = str(r.get("domain") or "").strip().lower()
        if not dom:
            continue
        merged = dict(defaults)
        merged.update(r)
        rules.append(_mk_rule(dom, merged))

    return default_rule, rules


def pick_rule(
    host: str, default_rule: DomainRule, rules: List[DomainRule]
) -> DomainRule:
    host = (host or "").lower()
    for r in rules:
        if r.matches_host(host):
            return r
    return default_rule


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

    async def get_detail_queue(
        self,
        domain: Optional[str],
        url_contains: Optional[str],
        limit: int,
    ) -> List[str]:
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

    async def post_extractions_batch(
        self, payloads: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Optional speed path: batch endpoint (falls back to per-item if not available)."""
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")

        url = f"{self.api_base}/api/v1/extractions/batch"
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key or "",
        }

        r = await self._client.post(url, headers=headers, json={"items": payloads})
        if r.status_code == 404:
            # Older backend: no batch endpoint
            out: List[Dict[str, Any]] = []
            for p in payloads:
                out.append(await self.post_extraction(p))
            return out

        if r.status_code >= 400:
            raise RuntimeError(f"API error {r.status_code}: {(r.text or '')[:800]}")

        data = r.json()
        results = data.get("results")
        if isinstance(results, list):
            return [x for x in results if isinstance(x, dict)]
        return []


class DetailExtractor:
    """Loads JS extractor scripts and executes them on pages."""

    def __init__(self):
        self._cache: Dict[str, str] = {}

    def load_script(self, path: str) -> str:
        if path not in self._cache:
            with open(path, "r", encoding="utf-8") as f:
                self._cache[path] = f.read()
        return self._cache[path]

    async def ensure(self, page: Page, script_path: str) -> None:
        try:
            ok = await page.evaluate(
                "() => !!(window.__listingDetailExtractor && window.__listingDetailExtractor.extract)"
            )
            if ok:
                return
        except Exception:
            pass

        script = self.load_script(script_path)
        try:
            await page.add_script_tag(content=script)
        except Exception:
            pass

        await page.wait_for_function(
            "() => !!(window.__listingDetailExtractor && window.__listingDetailExtractor.extract)",
            timeout=10_000,
        )

    async def extract_detail(self, page: Page, script_path: str) -> Dict[str, Any]:
        """
        Executes the in-page extractor and returns the full extraction dict.
        The extractor should return at minimum: {ok, title, description, image, images}
        and may additionally return v2 raw harvest fields.
        """
        await self.ensure(page, script_path)
        out = await page.evaluate("() => window.__listingDetailExtractor.extract()")
        if not isinstance(out, dict) or not out.get("ok"):
            raise RuntimeError("Detail extraction failed")

        # Basic normalization for core fields
        if isinstance(out.get("description"), str):
            out["description"] = out["description"].strip()
        if isinstance(out.get("title"), str):
            out["title"] = out["title"].strip()
        if isinstance(out.get("image"), str):
            out["image"] = out["image"].strip()
        if not isinstance(out.get("images"), list):
            out["images"] = []
        else:
            out["images"] = [str(x).strip() for x in out["images"] if isinstance(x, str) and x.strip()]

        return out

    async def extract_description(
        self, page: Page, script_path: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
        """
        Legacy adapter: returns (desc, title, cover_image, images) for older call sites.
        """
        out = await self.extract_detail(page, script_path)
        return (
            out.get("description"),
            out.get("title"),
            out.get("image"),
            out.get("images") or [],
        )
        desc = desc.strip() if isinstance(desc, str) else None
        title = title.strip() if isinstance(title, str) else None
        image = image.strip() if isinstance(image, str) else None
        if isinstance(images, list):
            images = [
                str(x).strip() for x in images if isinstance(x, str) and str(x).strip()
            ]
        else:
            images = []
        return title, desc, image, images


class ContextPool:
    """Reuse BrowserContext per domain rule for speed (cookies, JS init script, etc.)."""

    def __init__(self, browser: Browser, extractor: DetailExtractor):
        self.browser = browser
        self.extractor = extractor
        self._contexts: Dict[str, BrowserContext] = {}
        self._context_locks: Dict[str, asyncio.Lock] = {}

    async def get(self, rule: DomainRule) -> BrowserContext:
        key = f"{rule.domain}::{rule.extractor_script}"
        ctx = self._contexts.get(key)
        if ctx is not None:
            return ctx

        # Multiple detail workers may request the same domain context at once.
        # Serialize context creation only; page scraping remains concurrent.
        lock = self._context_locks.setdefault(key, asyncio.Lock())
        async with lock:
            ctx = self._contexts.get(key)
            if ctx is not None:
                return ctx

            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )

            ctx = await self.browser.new_context(
                user_agent=ua,
                viewport={"width": 1365, "height": 900},
                java_script_enabled=True,
                locale="bg-BG",
                ignore_https_errors=True,
                bypass_csp=True,
            )

            script = self.extractor.load_script(rule.extractor_script)
            await ctx.add_init_script(script=script)

            async def _route_handler(route, request):
                url = (request.url or "").lower()
                if any(part in url for part in BLOCKED_URL_PARTS):
                    await route.abort()
                    return

                # Speed wins without breaking images:
                # - keep images so they are harvested when available
                # - block fonts/media (almost never needed for detail text)
                rtype = (request.resource_type or "").lower()
                if rtype in ("font", "media"):
                    await route.abort()
                    return

                await route.continue_()

            await ctx.route("**/*", _route_handler)
            self._contexts[key] = ctx
            return ctx

    async def close(self) -> None:
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._context_locks.clear()


def make_detail_payload(
    listing_url: str,
    extracted: Dict[str, Any],
) -> Dict[str, Any]:
    """
    v2 detail payload:
      - keep legacy top-level keys for compatibility
      - add raw harvest fields (jsonld/state/kv/text blocks/contacts/media/signals)
    """
    title = extracted.get("title")
    description = extracted.get("description") or ""
    image = extracted.get("image")
    images = extracted.get("images") or []

    # Raw harvest fields (may be missing depending on extractor version)
    raw_jsonld = extracted.get("raw_jsonld") or []
    raw_state_blobs = extracted.get("raw_state_blobs") or []
    raw_kv = extracted.get("raw_kv") or []
    raw_text_blocks = extracted.get("raw_text_blocks") or []
    raw_contacts = extracted.get("raw_contacts") or {}
    raw_media = extracted.get("raw_media") or {}
    signals = extracted.get("signals") or {}

    fields = [
        # legacy
        "description",
        "rawText",
        "image",
        "images",
        # v2 raw harvest
        "raw_jsonld",
        "raw_state_blobs",
        "raw_kv",
        "raw_text_blocks",
        "raw_contacts",
        "raw_media",
        "signals",
    ]

    return {
        "dataVersion": 2,
        "sourceUrl": listing_url,
        "pageTitle": title,
        "extractedAt": _now_iso(),
        "meta": {
            "mode": "detail",
            "domain": _domain(listing_url),
            "fields": fields,
        },
        "items": [
            {
                # legacy fields
                "title": title,
                "url": listing_url,
                "description": description,
                "rawText": description,
                "image": image,
                "images": images,
                "texts": [],
                # v2 raw harvest
                "raw_jsonld": raw_jsonld,
                "raw_state_blobs": raw_state_blobs,
                "raw_kv": raw_kv,
                "raw_text_blocks": raw_text_blocks,
                "raw_contacts": raw_contacts,
                "raw_media": raw_media,
                "signals": signals,
            }
        ],
    }


async def scrape_one(
    pool: ContextPool,
    extractor: DetailExtractor,
    rule: DomainRule,
    url: str,
) -> Optional[Dict[str, Any]]:
    ctx = await pool.get(rule)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until=rule.wait_until, timeout=rule.timeout_ms)
        await page.wait_for_timeout(350)
        await _auto_scroll(page)
        await page.wait_for_timeout(250)

        extracted = await extractor.extract_detail(page, rule.extractor_script)
        title = extracted.get("title")
        desc = extracted.get("description")
        image = extracted.get("image")
        images = extracted.get("images") or []

        print(
            f"🖼️ images extracted: {len(images)} | cover={image} | sample={images[:5]}",
            flush=True,
        )

        meaningful, reason = has_meaningful_detail(extracted, rule.min_desc_len)
        if not meaningful:
            print(f"⏭️ incomplete detail: {url} -> {reason}")
            return None

        print(f"🧾 meaningful detail: {url} -> {reason}")
        return make_detail_payload(url, extracted)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def post_payloads(
    api: ApiClient, payloads: List[Dict[str, Any]], batch_size: int
) -> List[int]:
    ids: List[int] = []
    if not payloads:
        return ids

    batch_size = max(1, min(200, int(batch_size)))

    for i in range(0, len(payloads), batch_size):
        chunk = payloads[i : i + batch_size]
        try:
            results = (
                await api.post_extractions_batch(chunk)
                if batch_size > 1
                else [await api.post_extraction(chunk[0])]
            )
        except Exception as e:
            # Fallback: try individual posts so one bad payload doesn't nuke the whole batch.
            print(f"⚠️ batch post failed ({len(chunk)} items): {e} -> trying individual")
            results = []
            for p in chunk:
                try:
                    results.append(await api.post_extraction(p))
                except Exception as e2:
                    print(f"❌ post failed: {p.get('sourceUrl')} -> {e2}")

        # --- ADDED: per-post confirmation (true end-to-end signal) ---
        for r in results:
            rid = r.get("id") if isinstance(r, dict) else None
            if isinstance(rid, int):
                ids.append(rid)
                src = r.get("sourceUrl") if isinstance(r, dict) else None
                if isinstance(src, str) and src:
                    print(f"📌 posted: id={rid} url={src}")
                else:
                    print(f"📌 posted: id={rid}")
        # ------------------------------------------------------------

    return ids


async def run_once(
    api_base: str,
    api_key: str,
    queue_domain: Optional[str],
    queue_url_contains: Optional[str],
    queue_limit: int,
    max_urls: int,
    rules_file: str,
    global_concurrency: int,
    post_batch_size: int,
    retry_attempts: int,
    retry_delay_s: float,
) -> Tuple[int, int, Optional[int]]:
    """Returns (urls_seen, urls_scraped, last_id)."""

    default_rule, domain_rules = load_rules(rules_file)

    async with ApiClient(api_base, api_key) as api:
        urls = await api.get_detail_queue(
            domain=queue_domain, url_contains=queue_url_contains, limit=queue_limit
        )

        # dedupe + cap
        seen: Set[str] = set()
        final: List[Tuple[str, DomainRule]] = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)

            host = _domain(u)
            rule = pick_rule(host, default_rule, domain_rules)
            if not rule.allows_url(u):
                continue

            final.append((u, rule))
            if len(final) >= max_urls:
                break

        if not final:
            return (len(urls), 0, None)

        global_concurrency = max(1, min(20, int(global_concurrency)))

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

            extractor = DetailExtractor()
            pool = ContextPool(browser, extractor)

            # Per-domain semaphores (domain rule concurrency)
            domain_sems: Dict[str, asyncio.Semaphore] = {}
            for _u, rule in final:
                key = rule.domain
                if key not in domain_sems:
                    domain_sems[key] = asyncio.Semaphore(rule.concurrency)

            global_sem = asyncio.Semaphore(global_concurrency)

            async def worker(u: str, rule: DomainRule) -> Optional[Dict[str, Any]]:
                async with global_sem:
                    async with domain_sems[rule.domain]:
                        attempts = max(1, min(5, int(retry_attempts)))
                        last_error: Optional[str] = None

                        for attempt in range(1, attempts + 1):
                            try:
                                payload = await scrape_one(pool, extractor, rule, u)
                                if isinstance(payload, dict):
                                    items = payload.get("items") or []
                                    raw = ""
                                    if items and isinstance(items[0], dict):
                                        raw = items[0].get("rawText") or ""
                                    raw_len = len(raw) if isinstance(raw, str) else 0
                                    print(f"✅ scraped: {u} (rawText_len={raw_len}, attempt={attempt})")
                                    return payload

                                last_error = "no meaningful detail content"
                            except Exception as e:
                                last_error = str(e)

                            if attempt < attempts:
                                delay = max(0.0, float(retry_delay_s)) * attempt
                                print(
                                    f"↻ detail retry {attempt + 1}/{attempts}: {u} "
                                    f"after {delay:.1f}s ({last_error})"
                                )
                                await asyncio.sleep(delay)

                        print(f"❌ detail failed after {attempts} attempts: {u} -> {last_error}")
                        return None

            payloads = await asyncio.gather(
                *[asyncio.create_task(worker(u, rule)) for (u, rule) in final]
            )
            await pool.close()
            await browser.close()

        good_payloads = [p for p in payloads if isinstance(p, dict)]
        ids = await post_payloads(api, good_payloads, post_batch_size)

    last_id = ids[-1] if ids else None
    return (len(urls), len(ids), last_id)


async def main_async(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detail runner (multi-domain): pulls listing URLs from the DB queue and overwrites raw_text "
            "on the existing index item (no new rows)."
        )
    )

    parser.add_argument("--api-base", default=os.getenv("API_BASE", "http://api:8787"))
    parser.add_argument("--api-key", default=os.getenv("API_KEY", "dev-key-change-me"))

    # By default we run across ALL domains. You can still filter if needed.
    parser.add_argument(
        "--queue-domain", default=os.getenv("DETAIL_QUEUE_DOMAIN") or None
    )
    parser.add_argument(
        "--queue-url-contains", default=os.getenv("DETAIL_QUEUE_URL_CONTAINS") or None
    )
    parser.add_argument(
        "--queue-limit", type=int, default=int(os.getenv("DETAIL_QUEUE_LIMIT", "200"))
    )
    parser.add_argument(
        "--max-urls", type=int, default=int(os.getenv("DETAIL_MAX_URLS", "200"))
    )

    parser.add_argument(
        "--rules",
        default=os.getenv("DETAIL_RULES_FILE", "/app/scraper/detail_rules.yml"),
    )

    parser.add_argument(
        "--global-concurrency",
        type=int,
        default=int(os.getenv("DETAIL_GLOBAL_CONCURRENCY", "6")),
        help="Max parallel pages across all domains",
    )

    parser.add_argument(
        "--post-batch-size",
        type=int,
        default=int(os.getenv("DETAIL_POST_BATCH_SIZE", "25")),
        help="How many detail payloads to POST in one HTTP request (requires backend batch endpoint; auto-fallback if missing)",
    )

    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=int(os.getenv("DETAIL_RETRY_ATTEMPTS", "3")),
        help="Maximum scrape attempts per listing within the current batch",
    )

    parser.add_argument(
        "--retry-delay-s",
        type=float,
        default=float(os.getenv("DETAIL_RETRY_DELAY_S", "2.0")),
        help="Base retry delay; retries use a small linear backoff",
    )

    parser.add_argument(
        "--drain",
        action="store_true",
        help="Keep scraping batches until the queue is empty",
    )

    parser.add_argument(
        "--batch-sleep-s",
        type=float,
        default=float(os.getenv("DETAIL_BATCH_SLEEP_S", "0.5")),
        help="Sleep between drained batches",
    )

    args = parser.parse_args(argv)

    queue_limit = max(1, min(500, int(args.queue_limit)))
    max_urls = max(1, min(500, int(args.max_urls)))

    total_seen = 0
    total_done = 0
    last_id: Optional[int] = None

    print(
        f"DETAIL CONCURRENCY: {max(1, min(20, int(args.global_concurrency)))} | "
        f"retry_attempts={max(1, min(5, int(args.retry_attempts)))}"
    )

    while True:
        print(
            f"▶️ queue request: domain={args.queue_domain!r}, contains={args.queue_url_contains!r}, limit={queue_limit}"
        )

        seen, done, lid = await run_once(
            api_base=args.api_base,
            api_key=args.api_key,
            queue_domain=args.queue_domain,
            queue_url_contains=args.queue_url_contains,
            queue_limit=queue_limit,
            max_urls=max_urls,
            rules_file=args.rules,
            global_concurrency=args.global_concurrency,
            post_batch_size=args.post_batch_size,
            retry_attempts=args.retry_attempts,
            retry_delay_s=args.retry_delay_s,
        )

        total_seen += seen
        total_done += done
        last_id = lid or last_id

        print(f"✅ batch done: scraped={done}, queue_returned={seen}")

        if not args.drain:
            break

        # drain mode: stop when API returns 0 candidates
        if seen == 0:
            break

        # If queue returned URLs but none were posted, you might have too-strict rules.
        # We still continue draining to allow other domains to proceed in the next fetch.
        await asyncio.sleep(max(0.0, float(args.batch_sleep_s)))

    print(f"✅ detail total: posted={total_done}")
    if last_id is not None:
        print(f"last_extraction_id={last_id}")

    return 0 if total_done > 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(main_async(os.sys.argv[1:])))


if __name__ == "__main__":
    main()
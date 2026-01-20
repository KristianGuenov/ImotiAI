import argparse
import asyncio
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


def utc_now_iso() -> str:
    # UTC timestamp in ISO-8601 format
    return datetime.now(timezone.utc).isoformat()


# -----------------------------
# Data model
# -----------------------------


@dataclass(frozen=True)
class ScrapeTarget:
    """One target to scrape."""

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
    # - per_page: POST each page
    # - per_target: dedupe across pages and POST in batches (see post_batch_pages)
    post_strategy: str = "per_page"

    # If post_strategy == per_target and mode == pagination:
    # POST one batch per N pages (0 disables batching and posts once at end).
    post_batch_pages: int = 50


class TargetsLoader:
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
                    post_batch_pages=int(item.get("post_batch_pages") or 50),
                )
            )

        return targets


# -----------------------------
# API posting
# -----------------------------


class ApiClient:
    def __init__(self, endpoint: str, api_key: str, timeout_s: float = 60.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ApiClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Best-effort close.
        #
        # When the process is interrupted (Ctrl+C / SIGTERM), asyncio may already be
        # shutting down while httpx/anyio tries to close transports. In that case you can
        # get noisy secondary exceptions like `anyio.NoEventLoopError`.
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def post_extraction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")

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

        # Create a fresh context. We try to disable service workers because some sites
        # register SW/worker scripts that reference `chrome.*` and crash in headless.
        # (Blocking SWs also improves determinism and performance for scraping.)
        ctx_kwargs = dict(
            user_agent=ua,
            viewport={"width": 1365, "height": 900},
            java_script_enabled=True,
            locale="bg-BG",
            ignore_https_errors=True,
            bypass_csp=True,
        )
        try:
            ctx = await browser.new_context(**ctx_kwargs, service_workers="block")
        except TypeError:
            # Older Playwright
            ctx = await browser.new_context(**ctx_kwargs)

        # Some sites (and some extension-originated scripts) reference `chrome.*` directly.
        # In Playwright there is NO extension API, so `chrome` is often undefined which can crash page JS
        # and prevent our extractor from booting.
        #
        # We install a minimal, harmless stub that:
        #   - defines the global identifier `chrome`
        #   - defines window.chrome
        #   - provides no-op runtime/onMessage + storage.sync.get/set
        #
        # This is ONLY for scraping; in a real extension context Chrome provides the real API.
        # Robust `chrome` stub.
        # NOTE: we also use an *indirect eval* to create a true global `var chrome` binding
        # in case site scripts reference the identifier `chrome` very early.
        await ctx.add_init_script(
            script=(
                "(() => {\n"
                "  try {\n"
                "    const g = globalThis;\n"
                "    g.chrome = g.chrome || {};\n"
                "    try { (0, eval)(\"if (typeof chrome === 'undefined') { var chrome = globalThis.chrome; }\"); } catch(e) {}\n"
                "    if (typeof window !== 'undefined') window.chrome = g.chrome;\n"
                "    const c = g.chrome;\n"
                "    c.runtime = c.runtime || {};\n"
                "    c.runtime.id = c.runtime.id || '';\n"
                "    c.runtime.lastError = c.runtime.lastError || null;\n"
                "    c.runtime.getURL = c.runtime.getURL || (p => String(p || ''));\n"
                "    c.runtime.onMessage = c.runtime.onMessage || { addListener: function(){} };\n"
                "    c.runtime.sendMessage = c.runtime.sendMessage || function(){ };\n"
                "    c.storage = c.storage || {};\n"
                "    const mkArea = (area) => {\n"
                "      area.get = area.get || function(keys, cb){\n"
                "        try {\n"
                "          if (typeof cb === 'function') {\n"
                "            if (keys && typeof keys === 'object' && !Array.isArray(keys)) cb(keys); else cb({});\n"
                "          }\n"
                "        } catch(e) {}\n"
                "      };\n"
                "      area.set = area.set || function(_obj, cb){ try { if (typeof cb === 'function') cb(); } catch(e) {} };\n"
                "      area.remove = area.remove || function(_keys, cb){ try { if (typeof cb === 'function') cb(); } catch(e) {} };\n"
                "      area.clear = area.clear || function(cb){ try { if (typeof cb === 'function') cb(); } catch(e) {} };\n"
                "      return area;\n"
                "    };\n"
                "    c.storage.sync = mkArea(c.storage.sync || {});\n"
                "    c.storage.local = mkArea(c.storage.local || {});\n"
                "    c.i18n = c.i18n || {};\n"
                "    c.i18n.getMessage = c.i18n.getMessage || function(){ return ''; };\n"
                "    c.app = c.app || { isInstalled: false };\n"
                "  } catch (_) {}\n"
                "})();\n"
            )
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

        # Fallback injection (in case init_script didn't run).
        # We try multiple strategies because some pages have CSP oddities.
        script = self._load_content_script()
        injected = False
        try:
            await page.add_script_tag(content=script)
            injected = True
        except Exception:
            injected = False

        if not injected:
            # As a last resort, run via indirect eval in the page global scope.
            try:
                await page.evaluate(
                    "(code) => { try { (0, eval)(code); return true; } catch(e) { return String(e); } }", script
                )
            except Exception:
                pass

        try:
            await page.wait_for_function(
                "() => !!(window.__imotiExtractor && window.__imotiExtractor.run)",
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            # Capture a small diagnostic snapshot to make failures actionable.
            try:
                diag = await page.evaluate(
                    """() => ({
                      url: location.href,
                      readyState: document.readyState,
                      hasExtractor: !!(window.__imotiExtractor && window.__imotiExtractor.run),
                      injectedFlag: !!window.__realEstateExtractorInjected,
                      chromeType: (typeof chrome),
                      hasWindowChrome: (typeof window !== 'undefined' && !!window.chrome)
                    })"""
                )
            except Exception:
                diag = {"url": getattr(page, "url", None), "note": "failed to eval diag"}
            raise RuntimeError(f"Extractor not initialized (timeout). Diagnostic: {diag}")

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
        # Page-level JS exceptions. Print message + stack when available.
        def _on_pageerror(exc):
            try:
                msg = getattr(exc, "message", None) or str(exc)
                stack = getattr(exc, "stack", None)
                if stack:
                    print(f"[pageerror] {target_name}: {msg}\n{stack}")
                else:
                    print(f"[pageerror] {target_name}: {msg}")
            except Exception:
                print(f"[pageerror] {target_name}: {exc}")

        page.on("pageerror", _on_pageerror)

        def _console_handler(msg):
            if self.log_console_noise and msg.type in ("error", "warning"):
                # Include location when available to pinpoint failing scripts.
                try:
                    loc = msg.location
                    if loc and loc.get("url"):
                        print(
                            f"[console:{msg.type}] {target_name}: {msg.text} "
                            f"({loc.get('url')}:{loc.get('lineNumber')}:{loc.get('columnNumber')})"
                        )
                    else:
                        print(f"[console:{msg.type}] {target_name}: {msg.text}")
                except Exception:
                    print(f"[console:{msg.type}] {target_name}: {msg.text}")

        page.on("console", _console_handler)

        # Best-effort: stub `chrome` in web workers too (context init scripts do NOT run there).
        # This won't save a worker that throws *before* we attach, but it reduces noise on sites
        # where workers reference chrome after startup.
        def _on_worker(worker):
            async def _init_worker():
                try:
                    await worker.evaluate("(() => { try { globalThis.chrome = globalThis.chrome || {}; } catch(e) {} })();")
                except Exception:
                    pass

            try:
                asyncio.create_task(_init_worker())
            except Exception:
                pass

        try:
            page.on("worker", _on_worker)
        except Exception:
            # Some Playwright versions might not support this event.
            pass

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
        seen_item_keys: Optional[set] = None,
    ) -> Tuple[int, int, Optional[int]]:
        posted_signatures = posted_signatures if posted_signatures is not None else set()
        seen_item_keys = seen_item_keys if seen_item_keys is not None else set()

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
                print(f"[{target.name}] page=1 items={len(payload.get('items') or [])} posted={posts_done}")
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
                print(f"[{target.name}] page=1(load_more) items={len(payload.get('items') or [])} posted={posts_done}")
                return pages_visited, posts_done, last_id

            # Pagination mode
            if target.mode != "pagination":
                raise ValueError(f"Unknown mode '{target.mode}' for target '{target.name}'")

            max_pages = max(1, min(20000, int(target.max_pages)))
            delay_ms = max(0, min(20_000, int(target.delay_ms)))

            post_strategy = (target.post_strategy or "per_page").lower().strip()
            if post_strategy not in ("per_page", "per_target"):
                post_strategy = "per_page"

            batch_every = int(target.post_batch_pages or 50)
            batch_every = max(0, min(5000, batch_every))

            # Loop detection in THIS attempt
            seen_this_attempt: set = set()

            # Batch buffers (per_target)
            base_payload: Optional[Dict[str, Any]] = None
            batch_items: Dict[str, Dict[str, Any]] = {}
            batch_page_urls: List[str] = []
            batch_pages = 0
            batch_index = 0

            async def flush_batch(reason: str) -> None:
                nonlocal posts_done, last_id, batch_items, batch_page_urls, batch_pages, batch_index, base_payload

                if post_strategy != "per_target":
                    return

                # If batching disabled, we'll post once at the end.
                if batch_every <= 0:
                    return

                if not batch_items:
                    batch_page_urls = []
                    batch_pages = 0
                    return

                if base_payload is None:
                    base_payload = {"sourceUrl": target.url, "extractedAt": utc_now_iso(), "items": [], "meta": {}}

                payload_to_post = json.loads(json.dumps(base_payload, ensure_ascii=False))
                payload_to_post["sourceUrl"] = target.url
                payload_to_post["extractedAt"] = utc_now_iso()
                payload_to_post["items"] = list(batch_items.values())

                meta = dict(payload_to_post.get("meta") or {})
                meta.update(
                    {
                        "targetName": target.name,
                        "postStrategy": "per_target_batched",
                        "batchIndex": batch_index,
                        "batchPages": batch_pages,
                        "batchPageUrls": list(batch_page_urls),
                        "pagesVisitedSoFar": pages_visited,
                        "uniqueItemsTotalSoFar": len(seen_item_keys),
                        "flushReason": reason,
                    }
                )
                payload_to_post["meta"] = meta

                resp = await api.post_extraction(payload_to_post)
                last_id = resp.get("id") if isinstance(resp, dict) else last_id
                posts_done += 1

                print(
                    f"[{target.name}] BATCH_POST idx={batch_index} pages={batch_pages} "
                    f"items={len(batch_items)} unique_total={len(seen_item_keys)} last_id={last_id} ({reason})"
                )

                batch_index += 1
                batch_items = {}
                batch_page_urls = []
                batch_pages = 0

            for _ in range(max_pages):
                payload = await self._extract(page)
                pages_visited += 1

                items_list = payload.get("items") if isinstance(payload, dict) else None
                item_count = len(items_list) if isinstance(items_list, list) else 0

                sig = self._page_signature(payload)

                if sig in seen_this_attempt:
                    print(f"[{target.name}] STOP loop_detected page={pages_visited} url={page.url}")
                    break
                seen_this_attempt.add(sig)

                new_items_this_page = 0

                if sig not in posted_signatures:
                    if post_strategy == "per_page":
                        resp = await api.post_extraction(payload)
                        last_id = resp.get("id") if isinstance(resp, dict) else last_id
                        posts_done += 1

                    else:  # per_target
                        if base_payload is None:
                            base_payload = payload

                        if isinstance(items_list, list):
                            for it in items_list:
                                if not isinstance(it, dict):
                                    continue
                                k = self._item_key(it)
                                if k in seen_item_keys:
                                    continue
                                seen_item_keys.add(k)
                                new_items_this_page += 1

                                if batch_every > 0:
                                    batch_items[k] = it
                                else:
                                    # no batching: store everything in memory and post once at end
                                    batch_items[k] = it

                        batch_page_urls.append(page.url)
                        batch_pages += 1

                    posted_signatures.add(sig)
                else:
                    print(
                        f"[{target.name}] SKIP already_processed page={pages_visited}/{max_pages} "
                        f"items={item_count} url={page.url}"
                    )

                if post_strategy == "per_target":
                    print(
                        f"[{target.name}] page={pages_visited}/{max_pages} items={item_count} "
                        f"new_unique={new_items_this_page} unique_total={len(seen_item_keys)} "
                        f"batch_pages={batch_pages} batch_items={len(batch_items)} url={page.url}"
                    )
                else:
                    print(
                        f"[{target.name}] page={pages_visited}/{max_pages} items={item_count} "
                        f"posted_pages={posts_done} last_id={last_id} url={page.url}"
                    )

                if post_strategy == "per_target" and batch_every > 0 and batch_pages >= batch_every:
                    await flush_batch(reason=f"reached_{batch_every}_pages")

                did_nav = await self._navigate_next(page)
                if not did_nav:
                    print(f"[{target.name}] STOP no_next page={pages_visited} url={page.url}")
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

            # Final post for per_target
            if post_strategy == "per_target":
                if batch_every > 0:
                    await flush_batch(reason="final")
                else:
                    # One huge POST at end (not recommended for 900 pages, but supported)
                    if base_payload is not None:
                        merged = json.loads(json.dumps(base_payload, ensure_ascii=False))
                        merged["sourceUrl"] = target.url
                        merged["extractedAt"] = utc_now_iso()
                        merged["items"] = list(batch_items.values())
                        meta = dict(merged.get("meta") or {})
                        meta.update(
                            {
                                "targetName": target.name,
                                "postStrategy": "per_target",
                                "pagesVisited": pages_visited,
                                "uniqueItems": len(batch_items),
                            }
                        )
                        merged["meta"] = meta
                        resp = await api.post_extraction(merged)
                        last_id = resp.get("id") if isinstance(resp, dict) else last_id
                        posts_done += 1

            return pages_visited, posts_done, last_id

        finally:
            await ctx.close()


class ScrapeRunner:
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

        # Keep state per target across a retry in the same run
        self._posted_sigs_by_target: Dict[str, set] = {}
        self._seen_item_keys_by_target: Dict[str, set] = {}

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
                    print(f"Failed to launch browser: {e}")
                    return 0

                ok = 0
                try:
                    for t in targets:
                        print(f"\n=== {t.name} ===")
                        print(f"URL: {t.url}")
                        print(
                            f"Mode: {t.mode} | post_strategy: {t.post_strategy} | "
                            f"post_batch_pages: {t.post_batch_pages}"
                        )

                        if browser is None or not browser.is_connected():
                            try:
                                browser = await self._launch_browser(p)
                            except Exception as e:
                                print(f"Browser relaunch failed: {e}")
                                continue

                        posted = self._posted_sigs_by_target.setdefault(t.name, set())
                        seen_keys = self._seen_item_keys_by_target.setdefault(t.name, set())

                        try:
                            pages_visited, posts_done, last_id = await self.extractor.scrape_target(
                                api,
                                t,
                                browser,
                                posted_signatures=posted,
                                seen_item_keys=seen_keys,
                            )
                            ok += 1
                            print(
                                f"DONE target={t.name} pages_visited={pages_visited} posts_done={posts_done} "
                                f"last_extraction_id={last_id}"
                            )
                        except Exception as e:
                            msg = str(e)
                            if "Target page, context or browser has been closed" in msg:
                                print("Browser closed/crashed. Relaunching and retrying once...")
                                try:
                                    try:
                                        await browser.close()
                                    except Exception:
                                        pass
                                    browser = await self._launch_browser(p)

                                    pages_visited, posts_done, last_id = await self.extractor.scrape_target(
                                        api,
                                        t,
                                        browser,
                                        posted_signatures=posted,
                                        seen_item_keys=seen_keys,
                                    )
                                    ok += 1
                                    print(
                                        f"DONE(retry) target={t.name} pages_visited={pages_visited} posts_done={posts_done} "
                                        f"last_extraction_id={last_id}"
                                    )
                                except Exception as e2:
                                    print(f"FAILED target '{t.name}' after retry: {e2}")
                            else:
                                print(f"FAILED target '{t.name}': {e}")

                    return ok
                finally:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass


async def _amain(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Playwright scraper worker (posts to FastAPI /extractions).")
    parser.add_argument("--targets", default=os.getenv("TARGETS_FILE", "./targets.yml"))
    parser.add_argument("--content-script", default=os.getenv("CONTENT_SCRIPT_PATH", "./content.js"))
    parser.add_argument("--endpoint", default=os.getenv("API_ENDPOINT", "http://localhost:8787/api/v1/extractions"))
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

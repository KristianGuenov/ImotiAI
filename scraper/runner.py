import argparse
import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
import time

import httpx
import yaml
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ScrapeTarget:
    """One target to scrape."""

    name: str
    url: str
    mode: str = "extract_once"

    # pagination settings
    max_pages: int = 4000
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

    # If post_strategy == per_target, flush a batch every N pages (0 => one huge POST at end)
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
                    wait_until=str(item.get("wait_until") or "domcontentloaded").strip(),
                    timeout_ms=int(item.get("timeout_ms") or 45_000),
                    post_strategy=str(item.get("post_strategy") or "per_page").strip(),
                    post_batch_pages=int(item.get("post_batch_pages") or 50),
                )
            )

        return targets


class ApiClient:
    def __init__(self, endpoint: str, api_key: str):
        self.endpoint = endpoint
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self._client.aclose()

    async def post_extraction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        r = await self._client.post(self.endpoint, headers=headers, json=payload)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"ok": True}


class PlaywrightExtractor:
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
        self._content_script_cache: Optional[str] = None

    def _load_content_script(self) -> str:
        if self._content_script_cache is not None:
            return self._content_script_cache

        with open(self.content_script_path, "r", encoding="utf-8") as f:
            script = f.read()

        # Mark as injected for diagnostics
        script = "try{window.__realEstateExtractorInjected=true;}catch(_e){}\n" + script
        self._content_script_cache = script
        return script

    def _load_site_profiles_script(self) -> str:
        """
        Load /app/site_profiles.json (written by the profile-sink) and inject overrides so content.js can read them
        via chrome.storage.sync.get({siteOverrides:{}} ...).

        Returns a JS init-script string (or "" if no overrides are available).
        """
        path = os.getenv("SITE_PROFILES_PATH", "/app/site_profiles.json")
        if not os.path.exists(path):
            return ""

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            return ""

        site_overrides = data.get("siteOverrides") if isinstance(data, dict) else None
        if not isinstance(site_overrides, dict):
            return ""

        payload = json.dumps(site_overrides, ensure_ascii=False)

        # Avoid f-string `{}` escaping issues by using a template replacement.
        template = r"""
(function(){
  try {
    var overrides = __PAYLOAD__;
    if (typeof window !== 'undefined') window.__siteOverrides = overrides;

    if (typeof chrome === 'undefined') return;
    chrome.storage = chrome.storage || {};
    chrome.storage.sync = chrome.storage.sync || {};

    // Make chrome.storage.sync.get({siteOverrides:{}} , cb) return our overrides
    chrome.storage.sync.get = function(defaults, cb) {
      try {
        var out = (defaults && typeof defaults === 'object') ? Object.assign({}, defaults) : {};
        out.siteOverrides = overrides;
        if (typeof cb === 'function') cb(out);
      } catch (e) {
        try { if (typeof cb === 'function') cb(defaults || {}); } catch (_) {}
      }
    };
  } catch (_) {}
})();
"""
        return template.replace("__PAYLOAD__", payload)

    async def _new_context(self, browser: Browser) -> BrowserContext:
        ctx = await browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1365, "height": 900},
            java_script_enabled=True,
            bypass_csp=True,
            ignore_https_errors=True,
        )

        # Some portals run scripts that reference extension APIs (chrome.*). Provide a minimal stub.
        await ctx.add_init_script(
            script=(
                "(function(){\n"
                "  try {\n"
                "    if (typeof window === 'undefined') return;\n"
                "    if (typeof chrome !== 'undefined') return;\n"
                "    var c = {};\n"
                "    function mkArea(area){\n"
                "      area.get = area.get || function(defaults,cb){ cb && cb(defaults || {}); };\n"
                "      area.set = area.set || function(_,cb){ cb && cb(); };\n"
                "      area.remove = area.remove || function(_,cb){ cb && cb(); };\n"
                "      return area;\n"
                "    }\n"
                "    c.runtime = c.runtime || {};\n"
                "    c.runtime.getURL = c.runtime.getURL || function(p){ return p; };\n"
                "    c.storage = c.storage || {};\n"
                "    c.storage.sync = mkArea(c.storage.sync || {});\n"
                "    c.storage.local = mkArea(c.storage.local || {});\n"
                "    c.i18n = c.i18n || {};\n"
                "    c.i18n.getMessage = c.i18n.getMessage || function(){ return ''; };\n"
                "    c.app = c.app || { isInstalled: false };\n"
                "    window.chrome = c;\n"
                "    window.__chromeStubbed = true;\n"
                "  } catch (_) {}\n"
                "})();\n"
            )
        )

        # ✅ Inject site overrides FIRST (so content.js can use them in Playwright mode)
        sp = self._load_site_profiles_script()
        if sp:
            await ctx.add_init_script(script=sp)

        # Inject extractor at document-start for every navigation
        await ctx.add_init_script(script=self._load_content_script())
        return ctx

    async def _ensure_extractor(self, page: Page) -> None:
        async def _has() -> bool:
            try:
                return bool(await page.evaluate("() => !!(window.__imotiExtractor && window.__imotiExtractor.run)"))
            except Exception:
                return False

        if await _has():
            return

        # Fallback: try injecting again (rare; e.g. if page replaced context by cross-origin nav)
        try:
            await page.add_init_script(self._load_site_profiles_script() or "")
        except Exception:
            pass
        await page.add_init_script(self._load_content_script())

        t0 = time.time()
        while time.time() - t0 < 12.0:
            if await _has():
                return
            await asyncio.sleep(0.1)

        raise RuntimeError("Extractor not initialized (timeout).")

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

    def _host_of(self, url: str) -> str:
        try:
            return (urlsplit(url).hostname or "").lower()
        except Exception:
            return ""

    def _is_imotbg(self, url: str) -> bool:
        h = self._host_of(url)
        return h.endswith("imot.bg") or h.endswith("www.imot.bg")

    def _is_imotiinfo(self, url: str) -> bool:
        h = self._host_of(url)
        return h.endswith("imoti.info") or h.endswith("www.imoti.info")

    def _strip_choose_prefix(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path or ""
        if path.startswith("/choose/"):
            path = path[len("/choose"):]  # keep leading slash
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    def _manual_page_url(self, base_url: str, page_num: int) -> Optional[str]:
        """Build deterministic page URLs for known sites to avoid unstable Next links."""
        if self._is_imotbg(base_url):
            return self._imotbg_page_url(base_url, page_num)
        if self._is_imotiinfo(base_url):
            return self._imotiinfo_page_url(base_url, page_num)
        return None

    def _imotbg_page_url(self, base_url: str, page_num: int) -> str:
        """imot.bg list paging uses /p-{n}/ as a PATH segment."""
        parts = urlsplit(base_url)
        path = (parts.path or "").rstrip("/")

        # remove trailing /p-<n> or /p-<n>/ if present
        path = re.sub(r"/p-\d+/?$", "", path).rstrip("/")

        if page_num <= 1:
            new_path = path
        else:
            # keep trailing slash to avoid weird canonicalizations
            new_path = f"{path}/p-{page_num}/"

        return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))

    def _imotiinfo_page_url(self, base_url: str, page_num: int) -> str:
        """imoti.info commonly uses /page-N at the end of the path (avoid /choose/)."""
        base_url = self._strip_choose_prefix(base_url)
        parts = urlsplit(base_url)
        path = (parts.path or "").rstrip("/")

        # remove existing /page-N tail
        path = re.sub(r"/page-\d+/?$", "", path).rstrip("/")

        if page_num <= 1:
            new_path = path or "/"
        else:
            new_path = f"{path}/page-{page_num}"

        return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))

    async def _has_next_control_imotbg(self, page: Page) -> bool:
        """Best-effort check: if there is clearly no 'next' control, don't attempt p-2."""
        try:
            return bool(
                await page.evaluate(
                    """() => {
                      const sels = [
                        "a[rel='next']",
                        "link[rel='next']",
                        ".pagination a.next",
                        ".pagination a[rel='next']",
                        "a.next",
                        "a[aria-label*='Следва']",
                        "a[title*='Следва']",
                        "a[title*='Next']",
                        "a[aria-label*='Next']"
                      ];
                      return sels.some(s => document.querySelector(s));
                    }"""
                )
            )
        except Exception:
            return True  # if we can't evaluate, don't block pagination

    async def _ensure_imotiinfo_not_choose(self, page: Page, wait_timeout_ms: int) -> bool:
        """If we land on /choose/, try to get to the real listings page."""
        if not self._is_imotiinfo(page.url or ""):
            return True

        if "/choose/" not in (page.url or ""):
            return True

        # 1) Try direct navigation to stripped URL.
        try:
            fixed = self._strip_choose_prefix(page.url)
            if fixed != page.url:
                await page.goto(fixed, wait_until="domcontentloaded", timeout=wait_timeout_ms)
        except Exception:
            pass

        if "/choose/" not in (page.url or ""):
            return True

        # 2) Try clicking a "continue/show" control on the choose page to set cookie/state.
        try:
            clicked = await page.evaluate(
                """() => {
                  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();

                  // Prefer links that lead to non-/choose/ list pages
                  const links = Array.from(document.querySelectorAll("a[href]"));
                  const preferred = links.find(a => {
                    const h = a.getAttribute("href") || "";
                    return (h.includes("/prodazhbi/") || h.includes("/naemi/")) && !h.includes("/choose/");
                  });
                  if (preferred) { preferred.click(); return true; }

                  // Buttons or inputs with text like "покажи", "продължи", "виж", "търси"
                  const texts = ["покажи", "продължи", "виж", "търси", "готово", "приложи"];
                  const buttons = Array.from(document.querySelectorAll("button, a.btn, input[type='submit'], input[type='button']"));
                  const btn = buttons.find(el => {
                    const t = el.tagName === "INPUT" ? (el.value || "") : (el.textContent || "");
                    const nt = norm(t);
                    return texts.some(k => nt.includes(k));
                  });
                  if (btn) { btn.click(); return true; }

                  return false;
                }"""
            )
            if clicked:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=wait_timeout_ms)
                except Exception:
                    pass
                if "/choose/" in (page.url or ""):
                    fixed = self._strip_choose_prefix(page.url)
                    if fixed != page.url:
                        await page.goto(fixed, wait_until="domcontentloaded", timeout=wait_timeout_ms)
        except Exception:
            pass

        if "/choose/" in (page.url or ""):
            return False

        # 3) Best-effort validation
        try:
            await page.wait_for_selector("a[href*='/obiava']", timeout=min(15000, wait_timeout_ms))
        except Exception:
            pass

        return True

    def _attach_debug_listeners(self, page: Page, target_name: str) -> None:
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
            try:
                if self.log_console_noise:
                    print(f"[console:{msg.type}] {target_name}: {msg.text}")
                else:
                    if msg.type in ("error", "warning"):
                        loc = msg.location or {}
                        url = loc.get("url") if isinstance(loc, dict) else None
                        line = loc.get("lineNumber") if isinstance(loc, dict) else None
                        col = loc.get("columnNumber") if isinstance(loc, dict) else None
                        where = f" {url}:{line}:{col}" if url else ""
                        print(f"[console:{msg.type}] {target_name}:{where} {msg.text}")
            except Exception:
                pass

        page.on("console", _console_handler)

    def _page_signature(self, extracted_payload: Dict[str, Any], max_urls: int = 200) -> str:
        """Signature for loop detection.

        Uses a *sorted* set of many item URLs (not just the first few) to avoid false loop detection
        when a site pins/promotes the same top listings on every page.
        """
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

        if not urls:
            return "no-urls"

        urls = sorted(set(urls))
        return "|".join(urls)

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

            # imoti.info gating: ensure we are not stuck on /choose/ before extracting anything
            if self._is_imotiinfo(page.url or "") and "/choose/" in (page.url or ""):
                ok = await self._ensure_imotiinfo_not_choose(page, target.timeout_ms)
                if not ok:
                    print(f"[{target.name}] STOP imotiinfo_choose_gate url={page.url}")
                    return 1, 0, None

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
                await self._load_more(page, target.load_more or {})
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

            if target.mode != "pagination":
                raise ValueError(f"Unknown mode '{target.mode}' for target '{target.name}'")

            max_pages = max(1, min(20000, int(target.max_pages)))
            delay_ms = max(0, min(20_000, int(target.delay_ms)))

            post_strategy = (target.post_strategy or "per_page").lower().strip()
            if post_strategy not in ("per_page", "per_target"):
                post_strategy = "per_page"

            batch_every = int(target.post_batch_pages or 50)
            batch_every = max(0, min(5000, batch_every))

            seen_this_attempt: set = set()

            base_payload: Optional[Dict[str, Any]] = None
            batch_items: Dict[str, Dict[str, Any]] = {}
            batch_page_urls: List[str] = []
            batch_pages = 0
            batch_index = 0

            async def flush_batch(reason: str) -> None:
                nonlocal posts_done, last_id, batch_items, batch_page_urls, batch_pages, batch_index, base_payload

                if post_strategy != "per_target":
                    return

                if not batch_items:
                    batch_page_urls = []
                    batch_pages = 0
                    return

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
                        "uniqueItemsTotal": len(seen_item_keys),
                        "uniqueItemsInBatch": len(batch_items),
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
                # avoid extracting on imoti.info /choose/ pages
                if self._is_imotiinfo(page.url or "") and "/choose/" in (page.url or ""):
                    ok = await self._ensure_imotiinfo_not_choose(page, target.timeout_ms)
                    if not ok:
                        print(f"[{target.name}] STOP imotiinfo_choose_gate page={pages_visited+1} url={page.url}")
                        break

                payload = await self._extract(page)
                pages_visited += 1

                items_list = payload.get("items") if isinstance(payload, dict) else None
                item_count = len(items_list) if isinstance(items_list, list) else 0

                sig = self._page_signature(payload)

                if sig in seen_this_attempt:
                    reason = "no_next(same_content)" if pages_visited > 1 else "loop_detected"
                    print(f"[{target.name}] STOP {reason} page={pages_visited} url={page.url}")
                    break
                seen_this_attempt.add(sig)

                new_items_this_page = 0

                if sig not in posted_signatures:
                    if post_strategy == "per_page":
                        resp = await api.post_extraction(payload)
                        last_id = resp.get("id") if isinstance(resp, dict) else last_id
                        posts_done += 1
                    else:
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
                                batch_items[k] = it

                        batch_page_urls.append(page.url)
                        batch_pages += 1

                    posted_signatures.add(sig)

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

                if pages_visited >= max_pages:
                    break

                next_page_num = pages_visited + 1
                manual_next = self._manual_page_url(target.url, next_page_num)

                if manual_next:
                    if self._is_imotbg(target.url) and pages_visited == 1:
                        has_next = await self._has_next_control_imotbg(page)
                        if not has_next:
                            print(f"[{target.name}] STOP no_next(no_control) page={pages_visited} url={page.url}")
                            break

                    try:
                        await page.goto(manual_next, wait_until=target.wait_until, timeout=target.timeout_ms)
                    except Exception as e:
                        print(f"[{target.name}] STOP nav_failed page={pages_visited} next={manual_next} err={e}")
                        break

                    if self._is_imotiinfo(page.url or "") and "/choose/" in (page.url or ""):
                        ok = await self._ensure_imotiinfo_not_choose(page, target.timeout_ms)
                        if not ok:
                            print(f"[{target.name}] STOP imotiinfo_choose_gate page={pages_visited} url={page.url}")
                            break
                else:
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

            if post_strategy == "per_target":
                if batch_every > 0:
                    await flush_batch(reason="final")
                else:
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
        self._posted_sigs_by_target: Dict[str, set] = {}
        self._seen_item_keys_by_target: Dict[str, set] = {}

    async def _launch_browser(self, p) -> Browser:
        return await p.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

    async def run(self, only: Optional[str] = None) -> None:
        targets = self.loader.load()
        if only:
            targets = [t for t in targets if t.name == only]
            if not targets:
                raise ValueError(f"No target named '{only}' in {self.targets_file}")

        api = ApiClient(self.api_endpoint, self.api_key)

        async with async_playwright() as p:
            browser = await self._launch_browser(p)
            try:
                ok = 0
                fail = 0

                for t in targets:
                    print(f"=== {t.name} ===")
                    print(f"URL: {t.url}")
                    print(f"Mode: {t.mode} | post_strategy: {t.post_strategy} | post_batch_pages: {t.post_batch_pages}")

                    attempts = 0
                    while attempts < 2:
                        attempts += 1
                        try:
                            posted = self._posted_sigs_by_target.setdefault(t.name, set())
                            seen_keys = self._seen_item_keys_by_target.setdefault(t.name, set())

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
                            break

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
                                except Exception as e2:
                                    print(f"Browser relaunch failed: {e2}")
                                    fail += 1
                                    break
                                continue

                            print(f"FAILED target={t.name} err={e}")
                            fail += 1
                            break

                print(f"SUMMARY ok={ok} fail={fail}")

            finally:
                await api.close()
                try:
                    await browser.close()
                except Exception:
                    pass


def main():
    targets_file = os.getenv("TARGETS_FILE", "targets.yml")
    content_script_path = os.getenv("CONTENT_SCRIPT_PATH", "content.js")
    api_endpoint = os.getenv("API_ENDPOINT", "http://localhost:8787/api/v1/extractions")
    api_key = os.getenv("API_KEY", "")

    parser = argparse.ArgumentParser(description="Run index/list extraction targets.")
    parser.add_argument("--only", help="Run only a single target name", default=None)
    parser.add_argument("--headful", action="store_true", help="Run with a visible browser (headless=false)")
    args = parser.parse_args()

    runner = ScrapeRunner(
        targets_file=targets_file,
        content_script_path=content_script_path,
        api_endpoint=api_endpoint,
        api_key=api_key,
        headless=not args.headful,
    )

    asyncio.run(runner.run(only=args.only))


if __name__ == "__main__":
    main()

import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import time

import httpx
import yaml
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


@dataclass
class ScrapeTarget:
    """One target to scrape."""

    name: str
    url: str
    mode: str = "extract_once"

    # pagination settings
    max_pages: int = 4000
    delay_ms: int = 1500

    # Optional runtime pagination bounds.
    # These default to None for normal targets and are used by the runner to
    # split very large pagination targets (currently en.realestates.bg).
    start_page: Optional[int] = None
    end_page: Optional[int] = None

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
                    start_page=(
                        int(item["start_page"])
                        if item.get("start_page") is not None
                        else None
                    ),
                    end_page=(
                        int(item["end_page"])
                        if item.get("end_page") is not None
                        else None
                    ),
                    load_more=dict(item.get("load_more") or {}),
                    wait_until=str(
                        item.get("wait_until") or "domcontentloaded"
                    ).strip(),
                    timeout_ms=int(item.get("timeout_ms") or 45_000),
                    post_strategy=str(item.get("post_strategy") or "per_page").strip(),
                    post_batch_pages=int(item.get("post_batch_pages") or 50),
                )
            )

        return targets


class ApiClient:
    def __init__(self, endpoint: str, api_key: str):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=60.0)
        self._cycle_by_domain: Dict[str, int] = {}

        # Normal endpoint is .../api/v1/extractions. Keep backwards compatibility
        # with that env var and derive the inventory-cycle endpoint from it.
        marker = "/extractions"
        if marker in self.endpoint:
            self.api_base = self.endpoint.rsplit(marker, 1)[0]
        else:
            self.api_base = self.endpoint.rsplit("/", 1)[0]

    async def close(self):
        await self._client.aclose()

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def set_inventory_cycle(self, domain: str, cycle_id: Optional[int]) -> None:
        d = (domain or "").lower().strip()
        if not d:
            return
        if cycle_id is None:
            self._cycle_by_domain.pop(d, None)
        else:
            self._cycle_by_domain[d] = int(cycle_id)

    async def start_inventory_cycle(
        self, domain: str, targets_expected: int
    ) -> Dict[str, Any]:
        url = f"{self.api_base}/inventory-cycles/start"
        payload = {
            "domain": domain,
            "targetsExpected": int(targets_expected),
            "meta": {"runnerStartedAt": utc_now_iso()},
        }
        r = await self._client.post(url, headers=self._headers(), json=payload)
        r.raise_for_status()
        data = r.json()
        cycle_id = int(data["id"])
        self.set_inventory_cycle(domain, cycle_id)
        return data

    async def complete_inventory_cycle(
        self,
        domain: str,
        cycle_id: int,
        *,
        success: bool,
        targets_succeeded: int,
        targets_failed: int,
        abort_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.api_base}/inventory-cycles/{int(cycle_id)}/complete"
        meta: Dict[str, Any] = {"runnerCompletedAt": utc_now_iso()}
        if abort_reason:
            meta["abortReason"] = str(abort_reason)

        payload = {
            "success": bool(success),
            "targetsSucceeded": int(targets_succeeded),
            "targetsFailed": int(targets_failed),
            "meta": meta,
        }
        r = await self._client.post(url, headers=self._headers(), json=payload)
        r.raise_for_status()
        data = r.json()
        self.set_inventory_cycle(domain, None)
        return data

    async def post_extraction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Attach the domain cycle centrally so scrape modes do not need individual
        # changes at every POST call site.
        source_url = str(payload.get("sourceUrl") or "")
        domain = canonical_domain(source_url)
        cycle_id = self._cycle_by_domain.get(domain)
        if cycle_id is not None:
            meta = dict(payload.get("meta") or {})
            meta["inventoryCycleId"] = cycle_id
            payload = dict(payload)
            payload["meta"] = meta

        r = await self._client.post(self.endpoint, headers=self._headers(), json=payload)
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
        self._site_profiles_cache: Optional[str] = None

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
        """Load site_profiles.js if present.

        This lets the Playwright runner see the same site profile overrides
        that you tune via the extension/sink pipeline.
        """
        if self._site_profiles_cache is not None:
            return self._site_profiles_cache

        candidates: List[str] = []
        env_path = (os.getenv("SITE_PROFILES_JS") or "").strip()
        if env_path:
            candidates.append(env_path)
        # Common relative locations depending on WORKDIR/bind mounts
        candidates.extend(
            [
                "site_profiles.js",
                os.path.join("scraper", "site_profiles.js"),
            ]
        )

        path: Optional[str] = None
        for c in candidates:
            try:
                if c and os.path.exists(c):
                    path = c
                    break
            except Exception:
                continue

        if not path:
            self._site_profiles_cache = ""
            return ""

        try:
            with open(path, "r", encoding="utf-8") as f:
                s = f.read()
            self._site_profiles_cache = s
            return s
        except Exception:
            self._site_profiles_cache = ""
            return ""

    async def _new_context(
        self, browser: Browser, base_url: str = ""
    ) -> BrowserContext:
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
                "      area.get = area.get || function(_,cb){ cb && cb({}); };\n"
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

        # Reduce third-party noise / throttling on some sites (maps, analytics, identity widgets).
        try:
            host = self._host_of(base_url or "")
            if host.endswith("address.bg") or host.endswith("domaza.bg"):

                async def _route(route, request):
                    try:
                        u = (request.url or "").lower()

                        # Block very heavy / irrelevant third-party resources
                        third_party_blocks = (
                            "maps.googleapis.com/maps/api/js",
                            "maps.googleapis.com/maps-api-v3",
                            "static.hotjar.com/",
                            "accounts.google.com/gsi/",
                            "accounts.google.com/gsi/client",
                            "challenges.cloudflare.com/",
                        )
                        if any(s in u for s in third_party_blocks):
                            await route.abort()
                            return

                        # Address.bg map marker/icon requests are often rate-limited (429) and not needed for list scraping
                        if (
                            host.endswith("address.bg")
                            and "/images/map/" in u
                            and u.endswith(
                                (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
                            )
                        ):
                            await route.abort()
                            return

                        await route.continue_()
                    except Exception:
                        try:
                            await route.continue_()
                        except Exception:
                            pass

                await ctx.route("**/*", _route)
        except Exception:
            pass

        # ✅ Option A: Inject built-in site profiles FIRST (so content.js can use them in Playwright mode)
        sp = self._load_site_profiles_script()
        if sp:
            await ctx.add_init_script(script=sp)
        # Inject extractor at document-start for every navigation
        await ctx.add_init_script(script=self._load_content_script())
        return ctx

    async def _ensure_extractor(self, page: Page) -> None:
        async def _has() -> bool:
            try:
                return bool(
                    await page.evaluate(
                        "() => !!(window.__imotiExtractor && window.__imotiExtractor.run)"
                    )
                )
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
        """Run the in-page extractor with retries across navigations.

        ues.bg (and some other sites) may trigger a navigation on 'load more'. If our
        evaluate() happens during that navigation, Playwright throws:
          'Execution context was destroyed...'
        This wrapper waits for the new document and retries once.
        """
        await self._ensure_extractor(page)

        async def _do() -> Dict[str, Any]:
            out = await page.evaluate("() => window.__imotiExtractor.run()")
            if not isinstance(out, dict) or not out.get("ok"):
                err = out.get("error") if isinstance(out, dict) else None
                raise RuntimeError(err or "Extraction failed (no ok:true)")
            result = out.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(
                    "Extraction returned ok:true but missing result object"
                )
            return json.loads(json.dumps(result, ensure_ascii=False))

        try:
            return await _do()
        except Exception as e:
            msg = str(e)
            if (
                "Execution context was destroyed" in msg
                or "Cannot find context" in msg
                or "Target closed" in msg
            ):
                # Likely in-flight navigation; wait for the new document then retry once.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=45_000)
                except Exception:
                    pass
                await page.wait_for_timeout(250)
                await self._ensure_extractor(page)
                return await _do()
            raise

    async def _navigate_next(self, page: Page) -> bool:
        await self._ensure_extractor(page)
        did = await page.evaluate("() => window.__imotiExtractor.navigateNext()")
        return bool(did)

    async def _load_more(self, page: Page, options: Dict[str, Any]) -> Dict[str, Any]:
        """Trigger in-page loadMore in a navigation-safe way.

        Some sites (notably ues.bg) change the URL / navigate on 'load more'. When that
        happens, the JS execution context can be destroyed while evaluate() is running.
        In that case we *do not* re-click; we wait for the navigation to settle and return
        a marker so the caller can proceed to extract.
        """
        await self._ensure_extractor(page)
        options = options or {}

        url_before = page.url
        try:
            out = await page.evaluate(
                "(opts) => window.__imotiExtractor.loadMore(opts || {})", options
            )
            if isinstance(out, dict):
                # attach URL info if navigation happened
                if (page.url or "") != (url_before or ""):
                    out = dict(out)
                    out["urlBefore"] = url_before
                    out["urlAfter"] = page.url
                    out["navigated"] = True
                return out
            return {
                "ok": True,
                "urlBefore": url_before,
                "urlAfter": page.url,
                "navigated": (page.url != url_before),
            }
        except Exception as e:
            msg = str(e)
            if "Execution context was destroyed" in msg or "Cannot find context" in msg:
                # Navigation likely started; wait for it to finish, then continue without re-clicking.
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=int(options.get("navTimeoutMs", 45_000)),
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(350)
                # Ensure extractor is present in the new document
                try:
                    await self._ensure_extractor(page)
                except Exception:
                    pass
                return {
                    "ok": True,
                    "stoppedReason": "navigation_context_destroyed",
                    "urlBefore": url_before,
                    "urlAfter": page.url,
                    "navigated": True,
                }
            raise

    def _load_site_overrides_json_script(self) -> str:
        """Load site overrides JSON written by profile_sink and expose it to the page context."""
        candidates = [
            os.getenv("PROFILE_SINK_OUT"),
            "/data/site_profiles.json",
            "/app/site_profiles.json",
            "/app/scraper/site_profiles.json",
            str((Path(__file__).resolve().parent / "site_profiles.json")),
            "site_profiles.json",
        ]
        for path in candidates:
            if not path:
                continue
            try:
                p = Path(path)
                if not (p.exists() and p.is_file()):
                    continue
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(
                    data.get("siteOverrides"), dict
                ):
                    overrides = data["siteOverrides"]
                elif isinstance(data, dict):
                    # Some files may already be the overrides dict
                    overrides = {k: v for k, v in data.items() if isinstance(v, dict)}
                else:
                    continue
                payload = json.dumps(overrides, ensure_ascii=False)
                return f"window.__imotiRunnerSiteOverrides = {payload};"
            except Exception:
                continue
        return "window.__imotiRunnerSiteOverrides = {};"

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

    def _is_addressbg(self, url: str) -> bool:
        h = self._host_of(url)
        return h.endswith("address.bg") or h.endswith("www.address.bg")

    def _is_propertybg(self, url: str) -> bool:
        h = self._host_of(url)
        return h.endswith("property.bg") or h.endswith("www.property.bg")

    def _is_realestatesbg(self, url: str) -> bool:
        h = self._host_of(url)
        return h.endswith("realestates.bg") or h.endswith("www.realestates.bg")

    def _strip_choose_prefix(self, url: str) -> str:
        parts = urlsplit(url)
        path = parts.path or ""
        if path.startswith("/choose/"):
            path = path[len("/choose") :]  # keep leading slash
        return urlunsplit(
            (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
        )

    def _manual_page_url(self, base_url: str, page_num: int) -> Optional[str]:
        """Build deterministic page URLs for known sites to avoid unstable Next links."""
        if self._is_imotbg(base_url):
            return self._imotbg_page_url(base_url, page_num)
        if self._is_imotiinfo(base_url):
            return self._imotiinfo_page_url(base_url, page_num)
        if self._is_propertybg(base_url):
            return self._propertybg_page_url(base_url, page_num)
        if self._is_realestatesbg(base_url):
            return self._realestatesbg_page_url(base_url, page_num)
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

        return urlunsplit(
            (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
        )

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

        return urlunsplit(
            (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
        )

    def _propertybg_page_url(self, base_url: str, page_num: int) -> str:
        """property.bg search paging uses `page=N` query param."""
        parts = urlsplit(base_url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q["page"] = str(max(1, int(page_num)))
        new_query = urlencode(q, doseq=True)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )

    def _realestatesbg_page_url(self, base_url: str, page_num: int) -> str:
        """en.realestates.bg paging uses the `page=N` query parameter."""
        parts = urlsplit(base_url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q["page"] = str(max(1, int(page_num)))
        new_query = urlencode(q, doseq=True)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )

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

    async def _ensure_imotiinfo_not_choose(
        self, page: Page, wait_timeout_ms: int
    ) -> bool:
        """If we land on /choose/, try to get to the real listings page."""
        if not self._is_imotiinfo(page.url or ""):
            return True

        if "/choose/" not in (page.url or ""):
            return True

        # 1) Try direct navigation to stripped URL.
        try:
            fixed = self._strip_choose_prefix(page.url)
            if fixed != page.url:
                await page.goto(
                    fixed, wait_until="domcontentloaded", timeout=wait_timeout_ms
                )
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
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=wait_timeout_ms
                    )
                except Exception:
                    pass
                if "/choose/" in (page.url or ""):
                    fixed = self._strip_choose_prefix(page.url)
                    if fixed != page.url:
                        await page.goto(
                            fixed,
                            wait_until="domcontentloaded",
                            timeout=wait_timeout_ms,
                        )
        except Exception:
            pass

        if "/choose/" in (page.url or ""):
            return False

        # 3) Best-effort validation
        try:
            await page.wait_for_selector(
                "a[href*='/obiava']", timeout=min(15000, wait_timeout_ms)
            )
        except Exception:
            pass

        return True

    def _attach_debug_listeners(self, page: Page, target_name: str) -> None:
        def _on_pageerror(exc):
            try:
                msg = getattr(exc, "message", None) or str(exc)
                stack = getattr(exc, "stack", None)
                # Suppress noisy site JS errors that don't affect list scraping (maps/widgets/etc.)
                try:
                    noisy = (
                        "OverlayView",
                        "Cannot read properties of undefined (reading 'id')",
                        "Cannot read properties of undefined (reading 'OverlayView')",
                        "Permissions policy violation: Geolocation",
                        "hideLuxSelection is not defined",
                        "Unexpected string",
                    )
                    if any(s in (msg or "") for s in noisy) or any(
                        s in (stack or "") for s in noisy
                    ):
                        return
                except Exception:
                    pass
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
                    if msg.type in ("error",):
                        # Suppress known noisy console errors that don't affect list scraping
                        try:
                            t = msg.text or ""
                            loc0 = msg.location or {}
                            lu = (
                                (loc0.get("url") or "")
                                if isinstance(loc0, dict)
                                else ""
                            )

                            noise_substrings = (
                                "Permissions policy violation: Geolocation",
                                "Failed to load resource: the server responded with a status of 429",
                                "Provider's accounts list is empty",
                                "Not signed in with the identity provider",
                                "FedCM get() rejects",
                                "[GSI_LOGGER]",
                                "GSI_LOGGER",
                                "Hotjar not launching due to suspicious userAgent",
                                "Failed to create WebGPU Context Provider",
                                "Automatic fallback to software WebGL has been deprecated",
                                "OverlayView",
                                "Cannot read properties of undefined (reading 'id')",
                                "Cannot read properties of undefined (reading 'OverlayView')",
                            )
                            noise_url_substrings = (
                                "maps.googleapis.com/",
                                "static.hotjar.com/",
                                "accounts.google.com/gsi",
                                "challenges.cloudflare.com/",
                            )

                            if any(s in t for s in noise_substrings) or any(
                                s in lu for s in noise_url_substrings
                            ):
                                return
                        except Exception:
                            pass
                        loc = msg.location or {}
                        url = loc.get("url") if isinstance(loc, dict) else None
                        line = loc.get("lineNumber") if isinstance(loc, dict) else None
                        col = loc.get("columnNumber") if isinstance(loc, dict) else None
                        where = f" {url}:{line}:{col}" if url else ""
                        print(f"[console:{msg.type}] {target_name}:{where} {msg.text}")
            except Exception:
                pass

        page.on("console", _console_handler)

    def _page_signature(
        self, extracted_payload: Dict[str, Any], max_urls: int = 200
    ) -> str:
        """Signature for loop detection.

        Uses a *sorted* set of many item URLs (not just the first few) to avoid false loop detection
        when a site pins/promotes the same top listings on every page.
        """
        items = (
            extracted_payload.get("items")
            if isinstance(extracted_payload, dict)
            else None
        )
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

    def _is_holmesbg(self, url: str) -> bool:
        try:
            return "holmes.bg" in (url or "")
        except Exception:
            return False

    async def _wait_holmes_results_ready(self, page: Page, timeout_ms: int) -> None:
        """Holmes often hydrates listings after initial DOM load.

        Without a short readiness gate, extraction can capture only the first row of cards.
        This waits until the listing link count stabilizes or reaches a typical page size.
        """
        selector = "a[href^='/obiava/']"

        # Cap the gate time so it never blocks the whole run.
        deadline = time.monotonic() + (min(int(timeout_ms or 0), 15_000) / 1000.0)
        last_count = -1
        stable_since: Optional[float] = None

        while True:
            try:
                count = await page.locator(selector).count()
            except Exception:
                count = 0

            # Typical Holmes pages show up to 20 results ("1-20").
            if count >= 20:
                return

            if count > 0 and count == last_count:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif (time.monotonic() - stable_since) >= 1.0:
                    return
            else:
                stable_since = None
                last_count = count

            if time.monotonic() >= deadline:
                return

            await page.wait_for_timeout(250)

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
        posted_signatures = (
            posted_signatures if posted_signatures is not None else set()
        )
        seen_item_keys = seen_item_keys if seen_item_keys is not None else set()

        ctx = await self._new_context(browser, target.url)
        page = await ctx.new_page()
        self._attach_debug_listeners(page, target.name)

        pages_visited = 0
        posts_done = 0
        last_id: Optional[int] = None

        try:
            initial_url = target.url
            if target.start_page is not None and int(target.start_page) > 1:
                partition_start_url = self._manual_page_url(
                    target.url, int(target.start_page)
                )
                if not partition_start_url:
                    raise RuntimeError(
                        f"pagination partitioning is not supported for {target.url}"
                    )
                initial_url = partition_start_url

            await page.goto(
                initial_url, wait_until=target.wait_until, timeout=target.timeout_ms
            )
            await page.wait_for_timeout(700)
            await self._ensure_extractor(page)

            # holmes.bg often hydrates listings after initial DOM load; wait until results are ready
            if self._is_holmesbg(target.url):
                await self._wait_holmes_results_ready(page, target.timeout_ms)

            # imoti.info gating: ensure we are not stuck on /choose/ before extracting anything
            if self._is_imotiinfo(page.url or "") and "/choose/" in (page.url or ""):
                ok = await self._ensure_imotiinfo_not_choose(page, target.timeout_ms)
                if not ok:
                    raise RuntimeError(
                        f"incomplete_target: imotiinfo_choose_gate url={page.url}"
                    )

            if target.mode == "extract_once":
                payload = await self._extract(page)
                try:
                    items_list = (
                        payload.get("items") if isinstance(payload, dict) else None
                    )
                    item_count = len(items_list) if isinstance(items_list, list) else 0
                    sample_links = []
                    if isinstance(items_list, list):
                        for it in items_list:
                            if isinstance(it, dict) and it.get("url"):
                                sample_links.append(it.get("url"))
                            if len(sample_links) >= 5:
                                break
                    print(
                        f"[{target.name}] AFTER_LOAD_MORE items={item_count} sample_links={sample_links}"
                    )
                except Exception:
                    pass
                sig = self._page_signature(payload)
                if sig not in posted_signatures:
                    resp = await api.post_extraction(payload)
                    last_id = resp.get("id") if isinstance(resp, dict) else last_id
                    posts_done += 1
                    posted_signatures.add(sig)
                pages_visited = 1
                print(
                    f"[{target.name}] page=1 items={len(payload.get('items') or [])} posted={posts_done}"
                )
                return pages_visited, posts_done, last_id

            if target.mode == "load_more_then_extract":
                lm_stats = await self._load_more(page, target.load_more or {})
                print(f"[{target.name}] LOAD_MORE stats={lm_stats}")
                payload = await self._extract(page)
                try:
                    items_list = (
                        payload.get("items") if isinstance(payload, dict) else None
                    )
                    item_count = len(items_list) if isinstance(items_list, list) else 0
                    sample_links = []
                    if isinstance(items_list, list):
                        for it in items_list:
                            if isinstance(it, dict) and it.get("url"):
                                sample_links.append(it.get("url"))
                            if len(sample_links) >= 5:
                                break
                    print(
                        f"[{target.name}] AFTER_LOAD_MORE items={item_count} sample_links={sample_links}"
                    )
                except Exception:
                    pass
                sig = self._page_signature(payload)
                if sig not in posted_signatures:
                    resp = await api.post_extraction(payload)
                    last_id = resp.get("id") if isinstance(resp, dict) else last_id
                    posts_done += 1
                    posted_signatures.add(sig)
                pages_visited = 1
                print(
                    f"[{target.name}] page=1(load_more) items={len(payload.get('items') or [])} posted={posts_done}"
                )
                return pages_visited, posts_done, last_id

            if target.mode == "load_more_pagination":
                max_pages = max(1, min(20000, int(target.max_pages)))
                post_strategy = (target.post_strategy or "per_page").lower().strip()
                if post_strategy not in ("per_page", "per_target"):
                    post_strategy = "per_page"

                batch_every = int(target.post_batch_pages or 50)
                batch_every = max(0, min(5000, batch_every))

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

                    payload_to_post = json.loads(
                        json.dumps(base_payload or {}, ensure_ascii=False)
                    )
                    payload_to_post["sourceUrl"] = target.url
                    payload_to_post["extractedAt"] = utc_now_iso()
                    payload_to_post["items"] = list(batch_items.values())

                    meta = dict(payload_to_post.get("meta") or {})
                    meta.update(
                        {
                            "targetName": target.name,
                            "mode": "load_more_pagination",
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
                    batch_index += 1
                    batch_items = {}
                    batch_page_urls = []
                    batch_pages = 0

                no_new_streak = 0
                load_more_opts = target.load_more or {}

                for page_no in range(1, max_pages + 1):
                    payload = await self._extract(page)
                    try:
                        items_list = (
                            payload.get("items") if isinstance(payload, dict) else None
                        )
                        item_count = (
                            len(items_list) if isinstance(items_list, list) else 0
                        )
                        sample_links = []
                        if isinstance(items_list, list):
                            for it in items_list:
                                if isinstance(it, dict) and it.get("url"):
                                    sample_links.append(it.get("url"))
                                if len(sample_links) >= 5:
                                    break
                        print(
                            f"[{target.name}] AFTER_LOAD_MORE items={item_count} sample_links={sample_links}"
                        )
                    except Exception:
                        pass
                    if base_payload is None:
                        # Keep a stable envelope for batching; don't carry massive 'items' across posts.
                        base_payload = json.loads(
                            json.dumps(payload, ensure_ascii=False)
                        )
                        base_payload["items"] = []

                    items = payload.get("items") or []
                    new_items: List[Dict[str, Any]] = []
                    new_unique = 0
                    for it in items:
                        try:
                            key = self._item_key(it)
                        except Exception:
                            continue
                        if key in seen_item_keys:
                            continue
                        seen_item_keys.add(key)
                        new_unique += 1
                        new_items.append(it)
                        if post_strategy == "per_target":
                            batch_items[key] = it

                    pages_visited = page_no
                    print(
                        f"[{target.name}] page={page_no}/{max_pages} items={len(items)} new_unique={new_unique} "
                        f"unique_total={len(seen_item_keys)} url={page.url}"
                    )

                    # Per-page posting (optional): post only newly discovered items
                    if post_strategy == "per_page" and new_items:
                        payload_to_post = json.loads(
                            json.dumps(payload, ensure_ascii=False)
                        )
                        payload_to_post["items"] = new_items
                        meta = dict(payload_to_post.get("meta") or {})
                        meta.update(
                            {
                                "targetName": target.name,
                                "mode": "load_more_pagination",
                                "postStrategy": "per_page_new_items",
                                "pagesVisitedSoFar": pages_visited,
                                "uniqueItemsTotal": len(seen_item_keys),
                                "newUniqueThisPage": new_unique,
                            }
                        )
                        payload_to_post["meta"] = meta
                        resp = await api.post_extraction(payload_to_post)
                        last_id = resp.get("id") if isinstance(resp, dict) else last_id
                        posts_done += 1

                    # batch book-keeping
                    if post_strategy == "per_target":
                        batch_pages += 1
                        batch_page_urls.append(page.url)

                        if batch_every > 0 and (page_no % batch_every == 0):
                            await flush_batch(reason=f"reached_{batch_every}_pages")

                    if new_unique == 0:
                        no_new_streak += 1
                    else:
                        no_new_streak = 0

                    # Stop when we stop discovering new listings (primary stop condition).
                    if page_no > 1 and no_new_streak >= 1:
                        await flush_batch(reason="no_new_unique")
                        print(
                            f"[{target.name}] STOP no_new_unique page={page_no} url={page.url}"
                        )
                        break

                    if page_no >= max_pages:
                        raise RuntimeError(
                            f"incomplete_target: reached max_pages={max_pages} before natural end"
                        )

                    # Trigger one 'load more' step. ues.bg may navigate; _load_more() is navigation-safe.
                    url_before = page.url
                    status = await self._load_more(page, load_more_opts)

                    # If navigation happened, wait for the new document before the next extraction.
                    if (page.url or "") != (url_before or "") or bool(
                        status.get("navigated")
                    ):
                        try:
                            await page.wait_for_load_state(
                                target.wait_until, timeout=target.timeout_ms
                            )
                        except Exception:
                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded", timeout=target.timeout_ms
                                )
                            except Exception:
                                pass
                        await page.wait_for_timeout(350)
                        await self._ensure_extractor(page)

                    # Honor per-step waits (helps sites that hydrate cards after DOM load).
                    wait_after = int(load_more_opts.get("waitAfterClickMs", 0) or 0)
                    if wait_after > 0:
                        await page.wait_for_timeout(min(wait_after, 20_000))

                # Final flush for per-target batching
                await flush_batch(reason="end")
                print(
                    f"[{target.name}] DONE(load_more_pagination) pages_visited={pages_visited} posts_done={posts_done} last_extraction_id={last_id}"
                )
                return pages_visited, posts_done, last_id
            if target.mode != "pagination":
                raise ValueError(
                    f"Unknown mode '{target.mode}' for target '{target.name}'"
                )

            max_pages = max(1, min(20000, int(target.max_pages)))
            delay_ms = max(0, min(20_000, int(target.delay_ms)))

            current_page_num = max(1, int(target.start_page or 1))
            partition_end_page = (
                max(current_page_num, int(target.end_page))
                if target.end_page is not None
                else None
            )
            display_max_page = partition_end_page or max_pages

            post_strategy = (target.post_strategy or "per_page").lower().strip()
            if post_strategy not in ("per_page", "per_target"):
                post_strategy = "per_page"

            batch_every = int(target.post_batch_pages or 50)
            batch_every = max(0, min(5000, batch_every))

            seen_this_attempt: set = set()
            repeat_sig_hits = 0  # address.bg sometimes repeats pages under throttling; allow a few repeats

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

                payload_to_post = json.loads(
                    json.dumps(base_payload, ensure_ascii=False)
                )
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
                if self._is_imotiinfo(page.url or "") and "/choose/" in (
                    page.url or ""
                ):
                    ok = await self._ensure_imotiinfo_not_choose(
                        page, target.timeout_ms
                    )
                    if not ok:
                        raise RuntimeError(
                            f"incomplete_target: imotiinfo_choose_gate page={pages_visited+1} url={page.url}"
                        )

                payload = await self._extract(page)
                try:
                    items_list = (
                        payload.get("items") if isinstance(payload, dict) else None
                    )
                    item_count = len(items_list) if isinstance(items_list, list) else 0
                    sample_links = []
                    if isinstance(items_list, list):
                        for it in items_list:
                            if isinstance(it, dict) and it.get("url"):
                                sample_links.append(it.get("url"))
                            if len(sample_links) >= 5:
                                break
                    print(
                        f"[{target.name}] AFTER_LOAD_MORE items={item_count} sample_links={sample_links}"
                    )
                except Exception:
                    pass
                pages_visited += 1

                items_list = payload.get("items") if isinstance(payload, dict) else None
                item_count = len(items_list) if isinstance(items_list, list) else 0

                sig = self._page_signature(payload)

                if sig in seen_this_attempt:
                    if self._is_addressbg(target.url) and repeat_sig_hits < 5:
                        repeat_sig_hits += 1
                        print(
                            f"[{target.name}] WARN repeated_content allow_next hit={repeat_sig_hits} page={pages_visited} url={page.url}"
                        )
                    else:
                        reason = (
                            "no_next(same_content)"
                            if pages_visited > 1
                            else "loop_detected"
                        )
                        print(
                            f"[{target.name}] STOP {reason} page={pages_visited} url={page.url}"
                        )
                        break
                else:
                    seen_this_attempt.add(sig)
                    repeat_sig_hits = 0

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
                        f"[{target.name}] page={current_page_num}/{display_max_page} items={item_count} "
                        f"new_unique={new_items_this_page} unique_total={len(seen_item_keys)} "
                        f"batch_pages={batch_pages} batch_items={len(batch_items)} url={page.url}"
                    )
                else:
                    print(
                        f"[{target.name}] page={current_page_num}/{display_max_page} items={item_count} "
                        f"posted_pages={posts_done} last_id={last_id} url={page.url}"
                    )

                if (
                    post_strategy == "per_target"
                    and batch_every > 0
                    and batch_pages >= batch_every
                ):
                    await flush_batch(reason=f"reached_{batch_every}_pages")

                # A runtime page partition deliberately stops at its assigned
                # end page. This is a successful partition boundary, not a site end.
                if (
                    partition_end_page is not None
                    and current_page_num >= partition_end_page
                ):
                    print(
                        f"[{target.name}] STOP partition_end "
                        f"page={current_page_num} url={page.url}"
                    )
                    break

                if pages_visited >= max_pages:
                    raise RuntimeError(
                        f"incomplete_target: reached max_pages={max_pages} before natural end"
                    )

                next_page_num = current_page_num + 1
                manual_next = self._manual_page_url(target.url, next_page_num)

                if manual_next:
                    if self._is_imotbg(target.url) and current_page_num == 1:
                        has_next = await self._has_next_control_imotbg(page)
                        if not has_next:
                            print(
                                f"[{target.name}] STOP no_next(no_control) page={pages_visited} url={page.url}"
                            )
                            break

                    try:
                        await page.goto(
                            manual_next,
                            wait_until=target.wait_until,
                            timeout=target.timeout_ms,
                        )
                    except Exception as e:
                        raise RuntimeError(
                            f"incomplete_target: nav_failed page={pages_visited} next={manual_next} err={e}"
                        ) from e

                    if self._is_imotiinfo(page.url or "") and "/choose/" in (
                        page.url or ""
                    ):
                        ok = await self._ensure_imotiinfo_not_choose(
                            page, target.timeout_ms
                        )
                        if not ok:
                            raise RuntimeError(
                                f"incomplete_target: imotiinfo_choose_gate page={pages_visited} url={page.url}"
                            )
                else:
                    did_nav = await self._navigate_next(page)
                    if not did_nav:
                        print(
                            f"[{target.name}] STOP no_next page={pages_visited} url={page.url}"
                        )
                        break

                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=target.timeout_ms
                    )
                except Exception:
                    pass

                await page.wait_for_timeout(500)
                await self._ensure_extractor(page)
                current_page_num = next_page_num

                # holmes.bg: wait for hydrated results after navigation
                if self._is_holmesbg(target.url):
                    await self._wait_holmes_results_ready(page, target.timeout_ms)

                if delay_ms:
                    jitter = random.randint(0, min(600, delay_ms))
                    await page.wait_for_timeout(delay_ms + jitter)

            if post_strategy == "per_target":
                if batch_every > 0:
                    await flush_batch(reason="final")
                else:
                    if base_payload is not None:
                        merged = json.loads(
                            json.dumps(base_payload, ensure_ascii=False)
                        )
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
    # Extra target-level concurrency only for the domains that dominate runtime.
    # All other domains stay at one active target at a time.
    DOMAIN_TARGET_CONCURRENCY: Dict[str, int] = {
        "imoti.com": 2,
        "holmes.bg": 2,
        "imoti.info": 2,
        "en.realestates.bg": 4,
    }

    # en.realestates.bg has one very large pagination target, so target-level
    # concurrency alone cannot help it. The runner creates four non-overlapping
    # runtime page partitions for that one logical target.
    PAGE_PARTITION_CONCURRENCY: Dict[str, int] = {
        "en.realestates.bg": 4,
    }

    def __init__(
        self,
        targets_file: str,
        content_script_path: str,
        api_endpoint: str,
        api_key: str,
        headless: bool = True,
        domain_concurrency: int = 1,
    ):
        self.targets_file = targets_file
        self.content_script_path = content_script_path
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.headless = headless
        self.domain_concurrency = max(1, int(domain_concurrency))

        self.loader = TargetsLoader(targets_file)
        self.extractor = PlaywrightExtractor(
            content_script_path=content_script_path, headless=headless
        )
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

    def _target_worker_count(self, domain: str, items: List[ScrapeTarget]) -> int:
        configured = int(self.DOMAIN_TARGET_CONCURRENCY.get(domain, 1))
        if domain in self.PAGE_PARTITION_CONCURRENCY:
            return max(1, configured)
        return max(1, min(configured, len(items)))

    def _split_page_target(
        self,
        target: ScrapeTarget,
        parts: int,
    ) -> List[ScrapeTarget]:
        """Split one pagination target into non-overlapping runtime page ranges."""
        parts = max(1, int(parts))
        if parts == 1:
            return [target]
        if target.mode != "pagination":
            raise ValueError(
                f"Cannot page-partition non-pagination target '{target.name}' "
                f"(mode={target.mode})"
            )

        first_page = max(1, int(target.start_page or 1))
        last_page = max(first_page, int(target.end_page or target.max_pages))
        total_pages = last_page - first_page + 1
        parts = min(parts, total_pages)

        base_size = total_pages // parts
        remainder = total_pages % parts

        out: List[ScrapeTarget] = []
        cursor = first_page

        for index in range(parts):
            size = base_size + (1 if index < remainder else 0)
            start_page = cursor
            end_page = cursor + size - 1
            cursor = end_page + 1

            out.append(
                replace(
                    target,
                    name=f"{target.name}__part{index + 1}",
                    start_page=start_page,
                    end_page=end_page,
                )
            )

        return out

    def _domain_work_items(
        self,
        domain: str,
        items: List[ScrapeTarget],
    ) -> Tuple[List[ScrapeTarget], bool]:
        """Return runtime work items and whether they represent one logical target."""
        partition_count = int(self.PAGE_PARTITION_CONCURRENCY.get(domain, 1))
        if partition_count > 1:
            if len(items) != 1:
                raise ValueError(
                    f"Page-partitioned domain '{domain}' must currently have exactly "
                    f"one configured target; found {len(items)}"
                )

            parts = self._split_page_target(items[0], partition_count)
            ranges = ", ".join(
                f"{p.start_page}-{p.end_page}" for p in parts
            )
            print(f"[{domain}] PAGE PARTITIONS: {ranges}")
            return parts, True

        return list(items), False

    async def _run_target(
        self,
        api: ApiClient,
        target: ScrapeTarget,
        browser: Browser,
    ) -> bool:
        print(f"=== {target.name} ===")
        print(f"URL: {target.url}")
        print(
            f"Mode: {target.mode} | post_strategy: {target.post_strategy} | "
            f"post_batch_pages: {target.post_batch_pages}"
        )
        if target.start_page is not None or target.end_page is not None:
            print(
                f"[{target.name}] RANGE "
                f"{target.start_page or 1}-{target.end_page or target.max_pages}"
            )

        try:
            posted = self._posted_sigs_by_target.setdefault(target.name, set())
            seen_keys = self._seen_item_keys_by_target.setdefault(target.name, set())

            pages_visited, posts_done, last_id = await self.extractor.scrape_target(
                api,
                target,
                browser,
                posted_signatures=posted,
                seen_item_keys=seen_keys,
            )
            print(
                f"DONE target={target.name} pages_visited={pages_visited} "
                f"posts_done={posts_done} last_extraction_id={last_id}"
            )
            return True

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Under concurrency a domain browser may be shared by multiple target
            # contexts, so one target must never close/relaunch that browser while
            # sibling targets are still using it. A browser-level failure therefore
            # fails this target/domain safely; the lifecycle cycle will not reconcile.
            print(f"FAILED target={target.name} err={exc}")
            return False

    async def _run_targets_bounded(
        self,
        api: ApiClient,
        browser: Browser,
        work_items: List[ScrapeTarget],
        worker_count: int,
    ) -> List[bool]:
        """Run each work item exactly once with bounded same-domain concurrency."""
        semaphore = asyncio.Semaphore(max(1, int(worker_count)))

        async def guarded(target: ScrapeTarget) -> bool:
            async with semaphore:
                return await self._run_target(api, target, browser)

        # Each target object appears exactly once in this gather. The semaphore
        # dynamically keeps up to worker_count targets active, so faster workers
        # naturally take the next waiting target without manual 50/50 splitting.
        return await asyncio.gather(
            *(guarded(target) for target in work_items)
        )

    async def _finalize_failed_cycle(
        self,
        api: ApiClient,
        domain: str,
        cycle_id: Optional[int],
        *,
        targets_expected: int,
        targets_succeeded: int,
        targets_failed: int,
        abort_reason: str,
    ) -> None:
        if cycle_id is None:
            return

        remaining = max(
            0,
            int(targets_expected) - int(targets_succeeded) - int(targets_failed),
        )
        failed_total = max(int(targets_failed), 0) + remaining

        try:
            # Shield cycle finalization during Ctrl+C cancellation so a partially
            # scraped domain cannot be left eligible for reconciliation.
            result = await asyncio.shield(
                api.complete_inventory_cycle(
                    domain,
                    cycle_id,
                    success=False,
                    targets_succeeded=int(targets_succeeded),
                    targets_failed=int(failed_total),
                    abort_reason=abort_reason,
                )
            )
            print(
                f"INVENTORY_CYCLE ABORTED domain={domain} "
                f"cycle_id={cycle_id} status={result.get('status')}"
            )
        except Exception as exc:
            print(
                f"INVENTORY_CYCLE ABORT_FINALIZE_FAILED domain={domain} "
                f"cycle_id={cycle_id} err={exc}"
            )

    async def _run_domain(
        self,
        p,
        api: ApiClient,
        domain: str,
        items: List[ScrapeTarget],
        domain_semaphore: asyncio.Semaphore,
    ) -> Tuple[int, int]:
        """Run one independent domain lifecycle inside the global domain semaphore."""
        async with domain_semaphore:
            cycle_id: Optional[int] = None
            browser: Optional[Browser] = None

            logical_expected = len(items)
            logical_succeeded = 0
            logical_failed = 0

            try:
                data = await api.start_inventory_cycle(domain, logical_expected)
                cycle_id = int(data["id"])

                work_items, partitioned_single_target = self._domain_work_items(
                    domain, items
                )
                worker_count = self._target_worker_count(domain, work_items)

                print(
                    f"INVENTORY_CYCLE START domain={domain} "
                    f"cycle_id={cycle_id} targets={logical_expected} "
                    f"work_items={len(work_items)} target_workers={worker_count}"
                )

                # One browser process per active domain. Internal target workers use
                # separate BrowserContexts in that browser.
                browser = await self._launch_browser(p)

                results = await self._run_targets_bounded(
                    api,
                    browser,
                    work_items,
                    worker_count,
                )

                if partitioned_single_target:
                    # Four realestates page partitions are still one logical target.
                    if all(results):
                        logical_succeeded = 1
                        logical_failed = 0
                    else:
                        logical_succeeded = 0
                        logical_failed = 1
                else:
                    logical_succeeded = sum(1 for result in results if result)
                    logical_failed = logical_expected - logical_succeeded

                success = (
                    logical_succeeded == logical_expected
                    and logical_failed == 0
                )

                result = await api.complete_inventory_cycle(
                    domain,
                    cycle_id,
                    success=success,
                    targets_succeeded=logical_succeeded,
                    targets_failed=logical_failed,
                )
                print(
                    "INVENTORY_CYCLE END "
                    f"domain={domain} cycle_id={cycle_id} "
                    f"status={result.get('status')} "
                    f"seen={result.get('listingsSeen')} "
                    f"missing={result.get('listingsMissing')} "
                    f"deactivated={result.get('listingsDeactivated')} "
                    f"reconciled={result.get('reconciliationApplied')}"
                )
                if result.get("message"):
                    print(f"  {result.get('message')}")

                return logical_succeeded, logical_failed

            except asyncio.CancelledError:
                await self._finalize_failed_cycle(
                    api,
                    domain,
                    cycle_id,
                    targets_expected=logical_expected,
                    targets_succeeded=logical_succeeded,
                    targets_failed=logical_failed,
                    abort_reason="runner_interrupted",
                )
                raise

            except Exception as exc:
                print(f"INVENTORY_CYCLE DOMAIN_ERROR domain={domain} err={exc}")
                await self._finalize_failed_cycle(
                    api,
                    domain,
                    cycle_id,
                    targets_expected=logical_expected,
                    targets_succeeded=logical_succeeded,
                    targets_failed=logical_failed,
                    abort_reason="domain_runner_exception",
                )
                return logical_succeeded, max(
                    logical_failed,
                    logical_expected - logical_succeeded,
                )

            finally:
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass

    async def run(self, only: Optional[str] = None) -> None:
        all_targets = self.loader.load()

        if only:
            targets = [t for t in all_targets if t.name == only]
            if not targets:
                raise ValueError(f"No target named '{only}' in {self.targets_file}")
        else:
            targets = all_targets

        api = ApiClient(self.api_endpoint, self.api_key)

        domain_targets: Dict[str, List[ScrapeTarget]] = {}
        for target in targets:
            domain = canonical_domain(target.url)
            if not domain:
                raise ValueError(
                    f"Could not determine domain for target "
                    f"'{target.name}': {target.url}"
                )
            domain_targets.setdefault(domain, []).append(target)

        async with async_playwright() as p:
            try:
                # --only remains a smoke/debug run and never reconciles inventory.
                if only:
                    print(
                        "PARTIAL RUN (--only): inventory reconciliation is "
                        "disabled for safety."
                    )
                    browser = await self._launch_browser(p)
                    try:
                        ok = await self._run_target(api, targets[0], browser)
                    finally:
                        try:
                            await browser.close()
                        except Exception:
                            pass

                    print(f"SUMMARY ok={1 if ok else 0} fail={0 if ok else 1}")
                    return

                configured_target_workers = sum(
                    self._target_worker_count(domain, items)
                    for domain, items in domain_targets.items()
                )
                domain_limit = min(
                    self.domain_concurrency,
                    max(1, len(domain_targets)),
                )

                print(
                    f"DOMAIN CONCURRENCY: {domain_limit} | "
                    f"domains={len(domain_targets)} | targets={len(targets)} | "
                    f"configured_peak_target_workers={configured_target_workers}"
                )

                domain_semaphore = asyncio.Semaphore(domain_limit)

                tasks = [
                    asyncio.create_task(
                        self._run_domain(
                            p,
                            api,
                            domain,
                            items,
                            domain_semaphore,
                        ),
                        name=f"domain:{domain}",
                    )
                    for domain, items in domain_targets.items()
                ]

                try:
                    domain_results = await asyncio.gather(*tasks)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

                total_ok = sum(ok for ok, _fail in domain_results)
                total_fail = sum(fail for _ok, fail in domain_results)
                print(f"SUMMARY ok={total_ok} fail={total_fail}")

            finally:
                await api.close()


def main():
    targets_file = os.getenv("TARGETS_FILE", "targets.yml")
    content_script_path = os.getenv("CONTENT_SCRIPT_PATH", "content.js")
    api_endpoint = os.getenv(
        "API_ENDPOINT",
        "http://localhost:8787/api/v1/extractions",
    )
    api_key = os.getenv("API_KEY", "")

    env_domain_concurrency = int(
        os.getenv("SCRAPER_DOMAIN_CONCURRENCY", "1") or "1"
    )

    parser = argparse.ArgumentParser(
        description="Run index/list extraction targets."
    )
    parser.add_argument(
        "--only",
        help="Run only a single target name",
        default=None,
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run with a visible browser (headless=false)",
    )
    parser.add_argument(
        "--domain-concurrency",
        type=int,
        default=env_domain_concurrency,
        help=(
            "Maximum number of domains to scrape concurrently "
            "(default: SCRAPER_DOMAIN_CONCURRENCY or 1)"
        ),
    )
    args = parser.parse_args()

    runner = ScrapeRunner(
        targets_file=targets_file,
        content_script_path=content_script_path,
        api_endpoint=api_endpoint,
        api_key=api_key,
        headless=not args.headful,
        domain_concurrency=args.domain_concurrency,
    )

    asyncio.run(runner.run(only=args.only))


if __name__ == "__main__":
    main()
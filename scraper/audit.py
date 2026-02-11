#!/usr/bin/env python3
"""
audit.py - Domain audit automation for detail scraping.

Commands:
  - init: builds/updates domain_audit.yml from targets.yml (unique domains)
          optionally auto-fills 3 sample URLs per domain from the detail queue API.
  - run:  fetches the 3 sample URLs per domain, writes raw audit dumps to audits/<domain>/,
          computes structural signals, and writes them back into domain_audit.yml.

Design goals:
  - Minimal dependencies: reuses Playwright (already used by detail_runner.py).
  - No normalization: this is structure discovery only.
  - Safe caps: avoids storing unbounded HTML/text.

Autofill model (IMPORTANT):
  - Does NOT crawl the websites.
  - Pulls a pool of candidate detail URLs from the backend detail queue API (no domain filter),
    then assigns up to 3 sample URLs per audit domain by suffix/base-domain matching.
"""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import yaml
from playwright.async_api import async_playwright, Page


# ---------------------------
# Helpers
# ---------------------------

PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
LATLON_RE = re.compile(r"(-?\d{1,3}\.\d{3,})")

STATE_MARKERS = (
    "__NEXT_DATA__",
    "__NUXT__",
    "window.__INITIAL_STATE__",
    "INITIAL_STATE",
    "apolloState",
    "preloadedState",
    "reduxState",
)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def safe_slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9.-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def dump_yaml(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)


def cap_text(s: Optional[str], max_len: int) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"\n\n[TRUNCATED {len(s) - max_len} chars]"


def base_domain(host: str) -> str:
    """
    Normalize hostnames for matching:
    - lowercase
    - strip common subdomain prefixes: www, www2, m, mobile
    This is intentionally simple (no public-suffix parsing) and works well for BG portals.
    """
    h = (host or "").strip().lower().strip(".")
    if not h:
        return ""
    # strip repeated known prefixes
    while True:
        if h.startswith("www."):
            h = h[4:]
            continue
        if re.match(r"^www\d+\.", h):
            h = re.sub(r"^www\d+\.", "", h)
            continue
        if h.startswith("m."):
            h = h[2:]
            continue
        if h.startswith("mobile."):
            h = h[7:]
            continue
        break
    return h


# ---------------------------
# API client (read-only for audit init)
# ---------------------------

class ApiClient:
    def __init__(self, api_base: str, api_key: str, timeout_s: float = 30.0):
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
        domain: Optional[str] = None,
        url_contains: Optional[str] = None,
        limit: int = 200,
    ) -> List[str]:
        """
        Mirrors detail_runner's approach:
        - If domain is None, backend returns URLs across all domains.
        """
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
        out = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        return out


# ---------------------------
# DOM/HTML heuristics for detection
# ---------------------------

async def _extract_jsonld(page: Page) -> List[Any]:
    js = """
    () => {
      const out = [];
      for (const s of Array.from(document.querySelectorAll('script[type="application/ld+json"]'))) {
        const t = (s.textContent || '').trim();
        if (!t) continue;
        try {
          out.push(JSON.parse(t));
        } catch (e) {
          // ignore parse errors
        }
      }
      return out;
    }
    """
    res = await page.evaluate(js)
    return res if isinstance(res, list) else []


async def _extract_state_blobs(page: Page) -> List[Dict[str, Any]]:
    js = f"""
    () => {{
      const markers = {json.dumps(list(STATE_MARKERS))};
      const blobs = [];
      const scripts = Array.from(document.querySelectorAll('script'));
      for (const s of scripts) {{
        const id = (s.id || '').trim();
        const txt = (s.textContent || '');
        const len = txt.length || 0;
        if (!txt || len < 2000) continue;

        let marker = null;
        if (id && markers.includes(id)) marker = id;
        if (!marker) {{
          for (const m of markers) {{
            if (txt.includes(m)) {{ marker = m; break; }}
          }}
        }}
        // heuristic: either marker present or very large JSON-ish script
        const looksJson = (txt.trim().startsWith('{{') || txt.trim().startsWith('['));
        if (marker || (looksJson && len >= 50000)) {{
          blobs.push({{
            marker: marker,
            length: len,
            snippet: txt.slice(0, 20000),
          }});
        }}
      }}
      return blobs.slice(0, 8);
    }}
    """
    res = await page.evaluate(js)
    return res if isinstance(res, list) else []


async def _extract_kv_pairs(page: Page) -> Tuple[str, List[Dict[str, str]]]:
    # try to detect markup family + extract candidate pairs
    js = """
    () => {
      const pairs = [];
      let markup = "none";

      // 1) <dl><dt>/<dd>
      const dls = Array.from(document.querySelectorAll("dl"));
      for (const dl of dls) {
        const dts = Array.from(dl.querySelectorAll("dt"));
        const dds = Array.from(dl.querySelectorAll("dd"));
        if (dts.length && dds.length && Math.min(dts.length, dds.length) >= 2) {
          markup = "dl";
          const n = Math.min(dts.length, dds.length);
          for (let i=0;i<n;i++) {
            const k = (dts[i].innerText || "").trim();
            const v = (dds[i].innerText || "").trim();
            if (k && v) pairs.push({k, v});
          }
        }
      }

      // 2) tables with th/td or td/td
      const tables = Array.from(document.querySelectorAll("table"));
      for (const t of tables) {
        const rows = Array.from(t.querySelectorAll("tr"));
        for (const r of rows) {
          const th = r.querySelector("th");
          const tds = Array.from(r.querySelectorAll("td"));
          if (th && tds.length) {
            if (markup === "none") markup = "table";
            const k = (th.innerText || "").trim();
            const v = (tds[0].innerText || "").trim();
            if (k && v) pairs.push({k, v});
          } else if (tds.length >= 2) {
            if (markup === "none") markup = "table";
            const k = (tds[0].innerText || "").trim();
            const v = (tds[1].innerText || "").trim();
            if (k && v) pairs.push({k, v});
          }
        }
      }

      // 3) label:value patterns in text blocks
      const candidates = Array.from(document.querySelectorAll("li, p, div, span"))
        .slice(0, 4000)
        .map(x => (x.innerText || "").trim())
        .filter(t => t && t.length <= 120 && t.includes(":"));
      for (const t of candidates) {
        const parts = t.split(":");
        if (parts.length !== 2) continue;
        const k = parts[0].trim();
        const v = parts[1].trim();
        if (k && v && k.length <= 40 && v.length <= 70) {
          if (markup === "none") markup = "label_value";
          pairs.push({k, v});
        }
      }

      // de-dupe
      const seen = new Set();
      const out = [];
      for (const p of pairs) {
        const key = (p.k||"") + "||" + (p.v||"");
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(p);
        if (out.length >= 250) break;
      }

      // if mixed sources were found, label as mixed
      if (out.length > 0 && markup !== "none") {
        // If we found both dl and table-ish patterns, treat as mixed.
        const hasDl = dls.some(dl => dl.querySelectorAll("dt").length >= 2);
        const hasTable = tables.some(t => t.querySelectorAll("tr").length >= 2);
        if (hasDl && hasTable) markup = "mixed";
      }

      return { markup, pairs: out };
    }
    """
    out = await page.evaluate(js)
    if not isinstance(out, dict):
        return ("none", [])
    markup = out.get("markup") if isinstance(out.get("markup"), str) else "none"
    pairs = out.get("pairs") if isinstance(out.get("pairs"), list) else []
    kv_pairs: List[Dict[str, str]] = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        k = p.get("k")
        v = p.get("v")
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            kv_pairs.append({"k": k.strip(), "v": v.strip()})
    return (markup, kv_pairs)


async def _extract_images(page: Page) -> Tuple[str, List[str]]:
    js = """
    () => {
      const urls = new Set();

      // <img src> and common lazy attributes
      const imgs = Array.from(document.querySelectorAll("img"));
      for (const im of imgs) {
        const src = im.getAttribute("src") || "";
        const ds = im.getAttribute("data-src") || "";
        const dsl = im.getAttribute("data-srcset") || "";
        if (src.startsWith("http")) urls.add(src);
        if (ds.startsWith("http")) urls.add(ds);
        if (dsl.includes("http")) {
          for (const part of dsl.split(",")) {
            const u = part.trim().split(" ")[0];
            if (u && u.startsWith("http")) urls.add(u);
          }
        }
      }

      // CSS background images
      const els = Array.from(document.querySelectorAll("*")).slice(0, 6000);
      for (const el of els) {
        const bg = window.getComputedStyle(el).getPropertyValue("background-image") || "";
        if (bg.includes("url(")) {
          const m = bg.match(/url\\(["']?(.*?)["']?\\)/i);
          if (m && m[1] && m[1].startsWith("http")) urls.add(m[1]);
        }
      }

      // heuristic gallery type
      let type = "mixed";
      let hasDataSrc = false, hasBg = false, hasImgSrc = false;
      for (const im of imgs) {
        const src = (im.getAttribute("src")||"");
        const ds = (im.getAttribute("data-src")||"");
        if (src.startsWith("http")) hasImgSrc = true;
        if (ds.startsWith("http")) hasDataSrc = true;
      }
      if (hasDataSrc && !hasImgSrc) type = "data_src";
      else if (hasImgSrc && !hasDataSrc) type = "img_src";
      else if (hasImgSrc && hasDataSrc) type = "mixed";

      // background image presence
      // (if there are many bg images and few img tags, call it background_image)
      if (!hasImgSrc && urls.size > 0) type = "background_image";

      return { gallery_type: type, images: Array.from(urls).slice(0, 80) };
    }
    """
    out = await page.evaluate(js)
    if not isinstance(out, dict):
        return ("mixed", [])
    gt = out.get("gallery_type") if isinstance(out.get("gallery_type"), str) else "mixed"
    imgs = out.get("images") if isinstance(out.get("images"), list) else []
    images = [str(x) for x in imgs if isinstance(x, str)]
    return (gt, images)


async def _visible_text(page: Page) -> str:
    try:
        txt = await page.inner_text("body")
        return txt if isinstance(txt, str) else ""
    except Exception:
        return ""


async def analyze_url(page: Page, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> Dict[str, Any]:
    await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    await page.wait_for_timeout(750)

    # Basic signals
    title = ""
    try:
        title = await page.title()
    except Exception:
        title = ""

    html = await page.content()
    text = await _visible_text(page)

    jsonld = await _extract_jsonld(page)
    state_blobs = await _extract_state_blobs(page)
    kv_markup, kv_pairs = await _extract_kv_pairs(page)
    gallery_type, images = await _extract_images(page)

    # Contacts + coords heuristic
    phones = list({m.group(1).strip() for m in PHONE_RE.finditer(text)})[:20]
    emails = list({m.group(0).strip() for m in EMAIL_RE.finditer(text)})[:20]

    # coords: search in HTML + state snippets + jsonld raw
    coord_candidates: List[str] = []
    coord_candidates += [m.group(1) for m in LATLON_RE.finditer(html)][:40]
    for b in state_blobs:
        sn = b.get("snippet") if isinstance(b, dict) else None
        if isinstance(sn, str):
            coord_candidates += [m.group(1) for m in LATLON_RE.finditer(sn)][:20]
    for j in jsonld:
        try:
            coord_candidates += [m.group(1) for m in LATLON_RE.finditer(json.dumps(j, ensure_ascii=False))][:20]
        except Exception:
            pass
    has_map_coords = len(coord_candidates) >= 2  # loose

    # price heuristic: common currency symbols/keywords in text
    t_lower = (text or "").lower()
    has_price = ("€" in (text or "")) or ("eur" in t_lower) or ("лв" in t_lower) or ("bgn" in t_lower) or ("цена" in t_lower)

    # Decide primary payload source (rough)
    primary = "description_only"
    if jsonld and len(json.dumps(jsonld, ensure_ascii=False)) > 800:
        primary = "jsonld"
    if state_blobs and any(((b.get("marker") is not None) or (b.get("length", 0) >= 100_000)) for b in state_blobs if isinstance(b, dict)):
        # state tends to be richer than jsonld; override
        primary = "state_blob"
    if kv_pairs and len(kv_pairs) >= 8 and primary == "description_only":
        primary = "kv"
    if jsonld and kv_pairs:
        primary = "mixed"

    sample = {
        "url": url,
        "domain": domain_of(url),
        "scraped_at": now_iso(),
        "page_title": title,
        "raw": {
            "html": cap_text(html, 500_000),
            "text": cap_text(text, 250_000),
            "jsonld": jsonld,
            "state_blobs": state_blobs,
            "kv_markup": kv_markup,
            "kv_pairs": kv_pairs,
            "gallery_type": gallery_type,
            "images": images,
            "phones": phones,
            "emails": emails,
        },
        "signals": {
            "has_jsonld": bool(jsonld),
            "has_state_blob": bool(state_blobs),
            "has_kv_pairs": len(kv_pairs) > 0,
            "kv_markup": kv_markup,
            "has_contacts": bool(phones or emails),
            "has_price": bool(has_price),
            "gallery_type": gallery_type,
            "has_map_coords": bool(has_map_coords),
            "primary_payload_source": primary,
            "text_length": len(text or ""),
            "jsonld_block_count": len(jsonld),
            "state_blob_count": len(state_blobs),
            "kv_pair_count": len(kv_pairs),
        },
    }
    return sample


def aggregate_domain_signals(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Majority vote / averages across samples
    if not samples:
        return {}

    def majority_bool(key: str) -> Optional[bool]:
        vals = []
        for s in samples:
            v = (s.get("signals") or {}).get(key)
            if isinstance(v, bool):
                vals.append(v)
        if not vals:
            return None
        return vals.count(True) >= max(1, (len(vals) // 2 + 1))

    def majority_str(key: str) -> Optional[str]:
        vals = []
        for s in samples:
            v = (s.get("signals") or {}).get(key)
            if isinstance(v, str) and v:
                vals.append(v)
        if not vals:
            return None
        # mode
        return max(set(vals), key=vals.count)

    def avg_int(key: str) -> Optional[int]:
        vals = []
        for s in samples:
            v = (s.get("signals") or {}).get(key)
            if isinstance(v, int):
                vals.append(v)
        if not vals:
            return None
        return int(sum(vals) / len(vals))

    detected = {
        "has_jsonld": majority_bool("has_jsonld"),
        "has_state_blob": majority_bool("has_state_blob"),
        "has_kv_pairs": majority_bool("has_kv_pairs"),
        "kv_markup": majority_str("kv_markup"),
        "has_amenity_chips": None,  # reserved for future when we add chip detection
        "has_contacts": majority_bool("has_contacts"),
        "has_price": majority_bool("has_price"),
        "gallery_type": majority_str("gallery_type"),
        "has_map_coords": majority_bool("has_map_coords"),
        "primary_payload_source": majority_str("primary_payload_source"),
        "kv_pair_count_avg": avg_int("kv_pair_count"),
        "text_length_avg": avg_int("text_length"),
        "jsonld_block_count_avg": avg_int("jsonld_block_count"),
        "state_blob_count_avg": avg_int("state_blob_count"),
    }
    return detected


# ---------------------------
# Commands
# ---------------------------

def cmd_init(args: argparse.Namespace) -> int:
    targets_path = Path(args.targets)
    out_path = Path(args.out)

    targets = load_yaml(targets_path)
    if not isinstance(targets, list):
        raise SystemExit(f"targets.yml must be a list: {targets_path}")

    # unique domains from target URLs
    domains = sorted({domain_of(t.get("url", "")) for t in targets if isinstance(t, dict) and t.get("url")})
    domains = [d for d in domains if d]

    # Load existing audit file if present to preserve manual edits
    existing = load_yaml(out_path) if out_path.exists() else None
    existing_domains: Dict[str, Any] = {}
    if isinstance(existing, dict):
        for ent in existing.get("domains") or []:
            if isinstance(ent, dict) and ent.get("domain"):
                existing_domains[str(ent["domain"]).lower()] = ent

    doc: Dict[str, Any] = {
        "version": 1,
        "generated_at": now_iso(),
        "domains": [],
    }

    # Optionally auto-fill sample URLs from API
    api_base = args.api_base or os.getenv("API_BASE")
    api_key = args.api_key or os.getenv("API_KEY")
    auto_fill = bool(args.autofill_samples) and bool(api_base) and bool(api_key)

    sample_urls_by_domain: Dict[str, List[str]] = {}

    if auto_fill:
        pool_limit = max(50, int(args.queue_pool_limit))
        per_domain_cap = max(20, int(args.queue_limit))
        # Fetch a pool across ALL domains once, then match by (base)domain.
        async def _fill():
            async with ApiClient(api_base, api_key) as api:
                pool = await api.get_detail_queue(domain=None, url_contains=None, limit=pool_limit)

                # Index pool by base domain and by exact host (helps for weird portals)
                by_base: Dict[str, List[str]] = {}
                by_host: Dict[str, List[str]] = {}
                for u in pool:
                    h = domain_of(u)
                    if not h:
                        continue
                    bh = base_domain(h)
                    by_host.setdefault(h, []).append(u)
                    if bh:
                        by_base.setdefault(bh, []).append(u)

                # For each audit domain, pick URLs:
                for d in domains:
                    d_host = d
                    d_base = base_domain(d_host)
                    candidates: List[str] = []
                    # Try exact host bucket first (rarely matches but cheap)
                    candidates += by_host.get(d_host, [])
                    # Then base-domain bucket (main path)
                    if d_base:
                        candidates += by_base.get(d_base, [])

                    # Also allow suffix-match in case base-domain heuristic isn't enough
                    # e.g., audit domain = "imot.bg", host = "www.imot.bg"
                    if not candidates:
                        for h, urls in by_host.items():
                            if h == d_host or h.endswith("." + d_host) or d_host.endswith("." + h):
                                candidates += urls

                    # De-dupe while preserving order, cap
                    uniq: List[str] = []
                    seen: set = set()
                    for u in candidates:
                        if u in seen:
                            continue
                        seen.add(u)
                        uniq.append(u)
                        if len(uniq) >= 3:
                            break
                    sample_urls_by_domain[d_host] = uniq

        asyncio.run(_fill())

    for d in domains:
        prev = existing_domains.get(d)
        entry: Dict[str, Any] = prev if isinstance(prev, dict) else {}

        entry.setdefault("domain", d)
        entry.setdefault("enabled", True)

        # samples: preserve existing if present and non-empty AND at least one URL is set
        keep_existing = False
        if isinstance(entry.get("samples"), list) and entry.get("samples"):
            for s in entry["samples"]:
                if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("http"):
                    keep_existing = True
                    break

        if not keep_existing:
            urls = sample_urls_by_domain.get(d, []) if auto_fill else []
            entry["samples"] = [
                {"url": urls[0] if len(urls) > 0 else "", "label": "standard"},
                {"url": urls[1] if len(urls) > 1 else "", "label": "rich_features"},
                {"url": urls[2] if len(urls) > 2 else "", "label": "different_type_or_edge"},
            ]

        entry.setdefault("detected", {
            "has_jsonld": None,
            "has_state_blob": None,
            "has_kv_pairs": None,
            "kv_markup": None,
            "has_amenity_chips": None,
            "has_contacts": None,
            "has_price": None,
            "gallery_type": None,
            "has_map_coords": None,
            "primary_payload_source": None,
            "kv_pair_count_avg": None,
            "text_length_avg": None,
            "jsonld_block_count_avg": None,
            "state_blob_count_avg": None,
        })

        entry.setdefault("classification", {
            "template_family": None,
            "confidence": None,
            "notes": "",
        })

        entry.setdefault("extraction_plan", {
            "priority": "medium",
            "next_actions": [
                "Ensure detail_rules include/exclude patterns are correct",
                "Enable raw harvester: jsonld + kv extraction",
                "Add optional network json capture if SPA-backed",
            ],
        })

        doc["domains"].append(entry)

    dump_yaml(out_path, doc)
    print(f"✅ wrote: {out_path} (domains={len(doc['domains'])})")
    if auto_fill:
        filled = 0
        for ent in doc["domains"]:
            samples = ent.get("samples") or []
            if any(isinstance(x, dict) and isinstance(x.get("url"), str) and x["url"].startswith("http") for x in samples):
                filled += 1
        print(f"✅ auto-filled samples for {filled}/{len(doc['domains'])} domains (from detail queue API)")
        if filled == 0:
            print("ℹ️ Note: pool matching found no domains. Increase --queue-pool-limit or ensure the queue contains URLs for your target domains.")
    else:
        print("ℹ️ samples not auto-filled (provide --autofill-samples with API_BASE/API_KEY, or paste URLs manually)")
    return 0


async def cmd_run_async(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit)
    audits_dir = Path(args.audits_dir)

    doc = load_yaml(audit_path)
    if not isinstance(doc, dict) or not isinstance(doc.get("domains"), list):
        raise SystemExit(f"Invalid audit file: {audit_path}")

    domains = [d for d in (doc.get("domains") or []) if isinstance(d, dict) and d.get("enabled", True)]
    if args.domain:
        want = args.domain.lower()
        domains = [d for d in domains if str(d.get("domain","")).lower() == want]

    if not domains:
        print("⚠️ no domains to audit")
        return 2

    audits_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not bool(args.headed),
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent=DEFAULT_UA,
            viewport={"width": 1365, "height": 900},
            java_script_enabled=True,
            locale="bg-BG",
            ignore_https_errors=True,
            bypass_csp=True,
        )

        for ent in domains:
            dom = str(ent.get("domain") or "").lower()
            dom_dir = audits_dir / safe_slug(dom)
            dom_dir.mkdir(parents=True, exist_ok=True)

            samples = ent.get("samples") or []
            urls: List[str] = []
            for s in samples:
                if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("http"):
                    urls.append(s["url"])

            print(f"\n▶️ auditing {dom} (samples={len(urls)})")

            page = await ctx.new_page()
            try:
                sample_results: List[Dict[str, Any]] = []
                for idx, u in enumerate(urls[:3]):
                    label = None
                    if idx < len(samples) and isinstance(samples[idx], dict):
                        label = samples[idx].get("label")
                    label = safe_slug(str(label or f"sample_{idx+1}"))

                    try:
                        res = await analyze_url(page, u, wait_until=args.wait_until, timeout_ms=args.timeout_ms)
                        sample_results.append(res)

                        out_file = dom_dir / f"{label}.json"
                        with out_file.open("w", encoding="utf-8") as f:
                            json.dump(res, f, ensure_ascii=False, indent=2)
                        print(f"  ✅ saved {out_file.name} | kv_pairs={res['signals']['kv_pair_count']} jsonld={res['signals']['jsonld_block_count']} state={res['signals']['state_blob_count']}")
                    except Exception as e:
                        print(f"  ❌ sample failed: {u} -> {e}")

                detected = aggregate_domain_signals(sample_results)
                ent["detected"] = {**(ent.get("detected") or {}), **detected}

                det_file = dom_dir / "domain_detection.json"
                with det_file.open("w", encoding="utf-8") as f:
                    json.dump({"domain": dom, "detected": ent["detected"], "sample_count": len(sample_results)}, f, ensure_ascii=False, indent=2)
                print(f"  📌 wrote domain_detection.json")

            finally:
                await page.close()

        await ctx.close()
        await browser.close()

    # Write back updated audit file
    doc["generated_at"] = now_iso()
    dump_yaml(audit_path, doc)
    print(f"\n✅ updated: {audit_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(cmd_run_async(args))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit automation: init and run domain structural audits.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create/update domain_audit.yml from targets.yml")
    p_init.add_argument("--targets", default="targets.yml", help="Path to targets.yml")
    p_init.add_argument("--out", default="domain_audit.yml", help="Output path for domain_audit.yml")
    p_init.add_argument("--autofill-samples", action="store_true", help="Auto-fill 3 sample URLs per domain from detail queue API")
    p_init.add_argument("--api-base", default=None, help="API base (or set API_BASE env)")
    p_init.add_argument("--api-key", default=None, help="API key (or set API_KEY env)")
    p_init.add_argument("--queue-limit", type=int, default=60, help="(Legacy) per-domain desired pool size; used as fallback cap")
    p_init.add_argument("--queue-pool-limit", type=int, default=int(os.getenv("AUDIT_QUEUE_POOL_LIMIT", "2000")),
                        help="How many URLs to fetch from detail queue (across all domains) for auto-fill")

    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Run audits for sample URLs and write detected signals back to domain_audit.yml")
    p_run.add_argument("--audit", default="domain_audit.yml", help="Path to domain_audit.yml")
    p_run.add_argument("--audits-dir", default="audits", help="Directory to write audit dumps")
    p_run.add_argument("--domain", default=None, help="Audit only this domain")
    p_run.add_argument("--headed", action="store_true", help="Run browser headed (debug)")
    p_run.add_argument("--wait-until", default="domcontentloaded", choices=["load", "domcontentloaded", "networkidle"], help="Playwright waitUntil strategy")
    p_run.add_argument("--timeout-ms", type=int, default=45000, help="Navigation timeout per sample")

    p_run.set_defaults(func=cmd_run)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(args)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

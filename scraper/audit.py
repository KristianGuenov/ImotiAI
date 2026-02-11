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
"""

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
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


def base_domain(host: str) -> str:
    """
    Normalize hostnames for matching:
    - lowercase
    - strip common subdomain prefixes: www, www2, m, mobile
    This is intentionally simple (no public-suffix parsing).
    """
    h = (host or "").strip().lower().strip(".")
    if not h:
        return ""
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

    async def get_detail_queue(self, domain: str, limit: int = 50) -> List[str]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")
        params: Dict[str, Any] = {"limit": int(limit), "domain": domain}
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
# Detection via Playwright
# ---------------------------

async def _extract_jsonld(page: Page) -> List[Any]:
    """Return parsed JSON-LD blocks (best-effort)."""
    js = r"""
    () => {
      const blocks = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
        .map(s => (s.textContent || '').trim())
        .filter(Boolean);
      return blocks;
    }
    """
    blocks = await page.evaluate(js)
    out: List[Any] = []
    if isinstance(blocks, list):
        for raw in blocks[:20]:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                out.append(json.loads(raw))
            except Exception:
                # Some sites have multiple JSON objects concatenated; store raw snippet
                out.append({"_raw": raw[:2000]})
    return out


async def _extract_state_blobs(page: Page) -> List[Dict[str, Any]]:
    """Return script blobs that look like app state (best-effort)."""
    js = r"""
    () => {
      const scripts = Array.from(document.querySelectorAll('script'))
        .map(s => (s.id ? `#${s.id}` : '') + (s.type ? `:${s.type}` : '') + '::' + ((s.textContent || '').length))
      return scripts.slice(0, 200);
    }
    """
    # Above is cheap; for deeper detection we pull a few biggest scripts + known markers
    scripts_meta = await page.evaluate(js)
    # Pull actual content for scripts containing known markers or very large size
    js2 = r"""
    (markers) => {
      const out = [];
      const scripts = Array.from(document.querySelectorAll('script'));
      for (const s of scripts) {
        const t = (s.textContent || '');
        if (!t) continue;
        const len = t.length;
        let hit = null;
        for (const m of markers) {
          if (t.includes(m)) { hit = m; break; }
        }
        if (hit || len >= 50_000) {
          out.push({
            marker: hit,
            length: len,
            id: s.id || null,
            type: s.type || null,
            // store only a capped snippet to avoid huge dumps
            snippet: t.slice(0, 2000),
          });
        }
        if (out.length >= 10) break;
      }
      return out;
    }
    """
    blobs = await page.evaluate(js2, list(STATE_MARKERS))
    return blobs if isinstance(blobs, list) else []


async def _extract_kv_pairs(page: Page) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Try common patterns and return (kv_markup, pairs).
    kv_markup: dl | table | label_value | mixed | none
    """
    js = r"""
    () => {
      const pairs = [];
      let dlCount = 0, tblCount = 0, lvCount = 0;

      // DL pattern
      for (const dl of document.querySelectorAll('dl')) {
        const dts = Array.from(dl.querySelectorAll('dt'));
        for (const dt of dts) {
          const dd = dt.nextElementSibling;
          if (!dd || dd.tagName.toLowerCase() !== 'dd') continue;
          const k = (dt.textContent || '').trim();
          const v = (dd.textContent || '').trim();
          if (k && v && k.length <= 80 && v.length <= 200) {
            pairs.push([k, v, "dl"]);
            dlCount++;
            if (pairs.length >= 80) break;
          }
        }
        if (pairs.length >= 80) break;
      }

      // Table pattern
      if (pairs.length < 80) {
        for (const tr of document.querySelectorAll('table tr')) {
          const th = tr.querySelector('th');
          const td = tr.querySelector('td');
          if (!th || !td) continue;
          const k = (th.textContent || '').trim();
          const v = (td.textContent || '').trim();
          if (k && v && k.length <= 80 && v.length <= 200) {
            pairs.push([k, v, "table"]);
            tblCount++;
            if (pairs.length >= 80) break;
          }
        }
      }

      // Label: Value blocks (very heuristic)
      if (pairs.length < 80) {
        const candidates = Array.from(document.querySelectorAll('li, div, p, span'))
          .map(el => (el.textContent || '').trim())
          .filter(t => t && t.includes(':') && t.length <= 220);
        for (const t of candidates) {
          const idx = t.indexOf(':');
          const k = t.slice(0, idx).trim();
          const v = t.slice(idx+1).trim();
          if (k && v && k.length <= 60 && v.length <= 160) {
            pairs.push([k, v, "label_value"]);
            lvCount++;
            if (pairs.length >= 80) break;
          }
        }
      }

      // Decide markup
      const counts = {dl: dlCount, table: tblCount, label_value: lvCount};
      let best = "none";
      let bestCount = 0;
      for (const [k, c] of Object.entries(counts)) {
        if (c > bestCount) { bestCount = c; best = k; }
      }
      let mixed = 0;
      for (const c of Object.values(counts)) if (c > 0) mixed++;
      if (mixed >= 2) best = "mixed";
      if (bestCount === 0) best = "none";

      const outPairs = pairs.slice(0, 50).map(([k,v,_t]) => [k,v]);
      return { kv_markup: best, pairs: outPairs };
    }
    """
    out = await page.evaluate(js)
    if not isinstance(out, dict):
        return ("none", [])
    kv_markup = out.get("kv_markup") if isinstance(out.get("kv_markup"), str) else "none"
    pairs_raw = out.get("pairs")
    pairs: List[Tuple[str, str]] = []
    if isinstance(pairs_raw, list):
        for p in pairs_raw:
            if isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p):
                pairs.append((p[0].strip(), p[1].strip()))
    return (kv_markup, pairs)


async def _extract_images(page: Page) -> Tuple[str, List[str]]:
    js = r"""
    () => {
      const urls = new Set();
      const imgs = Array.from(document.images || []);
      let srcCount = 0, dataCount = 0, bgCount = 0;

      for (const img of imgs) {
        const src = (img.getAttribute('src') || '').trim();
        const ds = (img.getAttribute('data-src') || img.getAttribute('data-original') || '').trim();
        if (src && src.startsWith('http')) { urls.add(src); srcCount++; }
        if (ds && ds.startsWith('http')) { urls.add(ds); dataCount++; }
      }

      // background-image urls
      const els = Array.from(document.querySelectorAll('*')).slice(0, 2500);
      const re = /background-image\s*:\s*url\(["']?([^"')]+)["']?\)/i;
      for (const el of els) {
        const cs = window.getComputedStyle(el);
        const bg = cs && cs.backgroundImage ? cs.backgroundImage : '';
        const m = re.exec(bg || '');
        if (m && m[1] && (m[1].startsWith('http') || m[1].startsWith('//'))) {
          const u = m[1].startsWith('//') ? (location.protocol + m[1]) : m[1];
          urls.add(u);
          bgCount++;
        }
      }

      const counts = {img_src: srcCount, data_src: dataCount, background_image: bgCount};
      let best = "mixed";
      let nonzero = Object.values(counts).filter(x => x > 0).length;
      if (nonzero === 0) best = "mixed";
      else if (nonzero === 1) {
        best = Object.entries(counts).sort((a,b) => b[1]-a[1])[0][0];
      } else {
        best = "mixed";
      }

      return { gallery_type: best, images: Array.from(urls).slice(0, 80) };
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
    has_price = ("€" in text) or ("eur" in text.lower()) or ("лв" in text.lower()) or ("bgn" in text.lower()) or ("цена" in text.lower())

    # Decide primary payload source (rough)
    primary = "description_only"
    if jsonld and len(json.dumps(jsonld, ensure_ascii=False)) > 800:
        primary = "jsonld"
    if state_blobs and any((b.get("marker") or b.get("length", 0) >= 100_000) for b in state_blobs if isinstance(b, dict)):
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
        async def _fill():
            async with ApiClient(api_base, api_key) as api:
                for d in domains:
                    try:
                        q = await api.get_detail_queue(d, limit=max(20, args.queue_limit))
                        # Keep first 3 unique
                        uniq = []
                        seen = set()
                        for u in q:
                            if u in seen:
                                continue
                            seen.add(u)
                            uniq.append(u)
                            if len(uniq) >= 3:
                                break
                        sample_urls_by_domain[d] = uniq
                    except Exception:
                        sample_urls_by_domain[d] = []
        asyncio.run(_fill())

    for d in domains:
        prev = existing_domains.get(d)
        entry: Dict[str, Any] = prev if isinstance(prev, dict) else {}

        entry.setdefault("domain", d)
        entry.setdefault("enabled", True)

        # samples: preserve existing if present and non-empty
        if not isinstance(entry.get("samples"), list) or not entry.get("samples"):
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
        filled = sum(1 for d in doc["domains"] if any((x.get("url") or "").startswith("http") for x in (d.get("samples") or [])))
        print(f"✅ auto-filled samples for {filled}/{len(doc['domains'])} domains (from detail queue API)")
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
            samples = ent.get("samples") or []
            urls = [s.get("url") for s in samples if isinstance(s, dict) and isinstance(s.get("url"), str)]
            urls = [u for u in urls if u.startswith("http")]

            if len(urls) < 1:
                print(f"⏭️ {dom}: no sample URLs")
                continue

            dom_dir = audits_dir / safe_slug(dom)
            dom_dir.mkdir(parents=True, exist_ok=True)

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




# ---------------------------
# Classification + rules generation
# ---------------------------

_FAMILY_STOP_SEGMENTS = {
    # very common non-detail segments
    "search", "filter", "category", "login", "register", "account", "profile",
    # very common locale/type segments that are not stable detail markers
    "bg", "en", "sale", "rent", "prodazhbi", "prodazhba", "naem", "naemi", "offers", "offer",
    "imoti", "imot", "properties", "property",
}

def classify_family(detected: Dict[str, Any]) -> Tuple[str, str]:
    """
    Deterministic family assignment based on detected structural signals.
    Returns: (template_family, confidence)
    """
    if not isinstance(detected, dict):
        return ("mixed", "low")

    has_state = bool(detected.get("has_state_blob"))
    has_jsonld = bool(detected.get("has_jsonld"))
    has_kv = bool(detected.get("has_kv_pairs"))
    kv_avg = detected.get("kv_pair_count_avg")
    primary = (detected.get("primary_payload_source") or "").strip().lower()

    # Primary labels from detection (preferred)
    if primary in {"state_blob", "network_json"} or has_state:
        # many SPAs expose listing data via inlined state
        conf = "high" if primary == "state_blob" or has_state else "medium"
        return ("api_backed_spa", conf)

    if primary in {"jsonld"} and has_jsonld:
        return ("structured_jsonld", "high")

    # Mixed: both structured and kv
    if has_jsonld and has_kv and primary == "mixed":
        # choose which is more "dominant"
        if isinstance(kv_avg, int) and kv_avg >= 10:
            return ("kv_table_heavy", "medium")
        return ("structured_jsonld", "medium")

    if has_jsonld:
        return ("structured_jsonld", "medium")

    if has_kv:
        if isinstance(kv_avg, int) and kv_avg >= 8:
            return ("kv_table_heavy", "high")
        return ("kv_table_light", "medium")

    if not has_state and not has_jsonld and not has_kv:
        return ("description_only", "high")

    return ("mixed", "low")


def _derive_include_any_from_samples(sample_urls: List[str]) -> List[str]:
    """
    Generate a conservative include_any pattern list from 2-3 sample detail URLs.
    Goal: avoid list/search pages by requiring a stable marker present in all samples.
    """
    if not sample_urls:
        return []

    paths = []
    for u in sample_urls:
        try:
            paths.append(urlparse(u).path or "")
        except Exception:
            continue
    paths = [p for p in paths if p]
    if not paths:
        return []

    # Hard-coded high-signal patterns (BG portals)
    joined = " || ".join(paths).lower()
    if "/obiava" in joined:
        return ["/obiava"]  # imot.bg
    if "/offer/" in joined:
        return ["/offer/"]  # homes.bg and similar
    if "/offers/" in joined:
        return ["/offers/"]

    # Common segment intersection (preserve order from first path)
    seg_lists = []
    for p in paths:
        segs = [s for s in p.split("/") if s]
        seg_lists.append(segs)

    first = seg_lists[0]
    common = []
    for seg in first:
        s = seg.strip().lower()
        if not s or s in _FAMILY_STOP_SEGMENTS:
            continue
        if re.fullmatch(r"\d+", s):
            continue
        if all(seg in other for other in seg_lists[1:]):
            common.append(seg)

    if common:
        seg = common[0]
        # Prefer slash-bounded marker for stability
        if seg.endswith(".html"):
            return [f"/{seg}"]
        return [f"/{seg}/", f"/{seg}"]

    # Fallback: use first non-empty segment from first path
    for seg in first:
        s = seg.strip().lower()
        if not s or s in _FAMILY_STOP_SEGMENTS or re.fullmatch(r"\d+", s):
            continue
        return [f"/{seg}/", f"/{seg}"]

    return []


def cmd_classify(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit)
    doc = load_yaml(audit_path)
    if not isinstance(doc, dict) or not isinstance(doc.get("domains"), list):
        raise SystemExit(f"Invalid audit file: {audit_path}")

    changed = 0
    for ent in doc.get("domains") or []:
        if not isinstance(ent, dict):
            continue
        if not ent.get("enabled", True):
            continue
        detected = ent.get("detected") or {}
        fam, conf = classify_family(detected)
        cls = ent.get("classification")
        if not isinstance(cls, dict):
            cls = {}
            ent["classification"] = cls
        prev_fam = cls.get("template_family")
        prev_conf = cls.get("confidence")
        if prev_fam != fam or prev_conf != conf:
            cls["template_family"] = fam
            cls["confidence"] = conf
            changed += 1

    doc["generated_at"] = now_iso()
    dump_yaml(audit_path, doc)
    print(f"✅ updated classifications for {changed} domains: {audit_path}")
    return 0


def cmd_write_rules(args: argparse.Namespace) -> int:
    audit_path = Path(args.audit)
    rules_path = Path(args.rules)

    audit = load_yaml(audit_path)
    if not isinstance(audit, dict) or not isinstance(audit.get("domains"), list):
        raise SystemExit(f"Invalid audit file: {audit_path}")

    rules = load_yaml(rules_path)
    if not isinstance(rules, dict):
        raise SystemExit(f"Invalid rules file: {rules_path}")

    defaults = rules.get("defaults")
    if not isinstance(defaults, dict):
        raise SystemExit("detail_rules.yml missing 'defaults' dict")

    existing_domains = rules.get("domains") or []
    existing_by_domain: Dict[str, Dict[str, Any]] = {}
    if isinstance(existing_domains, list):
        for r in existing_domains:
            if isinstance(r, dict) and r.get("domain"):
                existing_by_domain[str(r["domain"]).lower()] = r

    # Build complete domain rule list
    out_domains: List[Dict[str, Any]] = []
    seen: set = set()

    def family_concurrency(fam: str) -> int:
        fam = (fam or "").lower()
        if fam in {"api_backed_spa"}:
            return 1
        if fam in {"structured_jsonld"}:
            return 2
        if fam in {"kv_table_heavy"}:
            return 2
        if fam in {"kv_table_light"}:
            return 2
        if fam in {"description_only"}:
            return 2
        return 2

    for ent in audit.get("domains") or []:
        if not isinstance(ent, dict):
            continue
        if not ent.get("enabled", True):
            continue

        audit_dom = str(ent.get("domain") or "").lower().strip()
        if not audit_dom:
            continue

        dom = base_domain(audit_dom) or audit_dom
        dom = dom.lower()
        if dom in seen:
            continue
        seen.add(dom)

        # Preserve existing rule if present; otherwise create one.
        rule = dict(existing_by_domain.get(dom, {}))
        rule.setdefault("domain", dom)

        # Apply/refresh concurrency from family unless user wants to keep existing explicitly
        fam = ((ent.get("classification") or {}).get("template_family") or "").strip()
        if args.force or "concurrency" not in rule:
            rule["concurrency"] = family_concurrency(fam) if fam else int(defaults.get("concurrency", 2))

        # min_desc_len: keep existing if set; else default
        rule.setdefault("min_desc_len", defaults.get("min_desc_len", 60))

        # include/exclude: fill if missing or if forced
        samples = ent.get("samples") or []
        sample_urls = [s.get("url") for s in samples if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("http")]
        if args.force or ("include_any" not in rule and "include_regex" not in rule):
            inc = _derive_include_any_from_samples(sample_urls[:3])
            rule["include_any"] = inc
            rule.setdefault("include_regex", [])
        else:
            # normalize
            rule.setdefault("include_any", [])
            rule.setdefault("include_regex", [])

        # Ensure exclude_any exists; don't delete existing exclusions.
        if "exclude_any" not in rule:
            rule["exclude_any"] = []
        if not isinstance(rule["exclude_any"], list):
            rule["exclude_any"] = []

        # Domain-specific hygiene tweaks (lightweight)
        j = " ".join(sample_urls).lower()
        if "/obiava" in j and "/obiavi/" not in rule["exclude_any"]:
            rule["exclude_any"].append("/obiavi/")

        out_domains.append(rule)

    # Preserve any existing rules for domains not in audit (rare but safe)
    for dom_key, rule in existing_by_domain.items():
        if dom_key not in seen:
            out_domains.append(rule)

    # Write back into the same file (user requested no second file)
    rules["domains"] = sorted(out_domains, key=lambda x: str(x.get("domain","")))

    dump_yaml(rules_path, rules)
    print(f"✅ wrote updated rules: {rules_path} (domains={len(rules['domains'])})")
    return 0



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit automation: init and run domain structural audits.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create/update domain_audit.yml from targets.yml")
    p_init.add_argument("--targets", default="targets.yml", help="Path to targets.yml")
    p_init.add_argument("--out", default="domain_audit.yml", help="Output path for domain_audit.yml")
    p_init.add_argument("--autofill-samples", action="store_true", help="Auto-fill 3 sample URLs per domain from detail queue API")
    p_init.add_argument("--api-base", default=None, help="API base (or set API_BASE env)")
    p_init.add_argument("--api-key", default=None, help="API key (or set API_KEY env)")
    p_init.add_argument("--queue-limit", type=int, default=60, help="How many queue URLs to fetch per domain when auto-filling")

    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Run audits for sample URLs and write detected signals back to domain_audit.yml")
    p_run.add_argument("--audit", default="domain_audit.yml", help="Path to domain_audit.yml")
    p_run.add_argument("--audits-dir", default="audits", help="Directory to write audit dumps")
    p_run.add_argument("--domain", default=None, help="Audit only this domain")
    p_run.add_argument("--headed", action="store_true", help="Run browser headed (debug)")
    p_run.add_argument("--wait-until", default="domcontentloaded", choices=["load", "domcontentloaded", "networkidle"], help="Playwright waitUntil strategy")
    p_run.add_argument("--timeout-ms", type=int, default=45000, help="Navigation timeout per sample")

    p_run.set_defaults(func=cmd_run)

    p_classify = sub.add_parser("classify", help="Auto-assign template families based on detected signals")
    p_classify.add_argument("--audit", default="domain_audit.yml", help="Path to domain_audit.yml")
    p_classify.set_defaults(func=cmd_classify)

    p_rules = sub.add_parser("write-rules", help="Fill/update detail_rules.yml domains from domain_audit.yml samples + classifications")
    p_rules.add_argument("--audit", default="domain_audit.yml", help="Path to domain_audit.yml")
    p_rules.add_argument("--rules", default="detail_rules.yml", help="Path to detail_rules.yml to update IN PLACE")
    p_rules.add_argument("--force", action="store_true", help="Overwrite include_any/include_regex and concurrency for all domains")
    p_rules.set_defaults(func=cmd_write_rules)



    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(args)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

import argparse
import asyncio
from io import BytesIO
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import httpx
import yaml
try:
    from scraper.listing_health import canonical_domain, classify_response
except ImportError:  # direct execution: python /app/scraper/detail_runner.py
    from listing_health import canonical_domain, classify_response
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


async def _stable_page_content(page: Page, attempts: int = 6) -> bytes:
    """Read rendered HTML across one-time SPA hydration navigations."""
    last_error: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            return (await page.content()).encode("utf-8", "replace")[:131072]
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await page.wait_for_timeout(400)
    raise RuntimeError(f"detail page never became stable: {last_error}")


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


def _next_queue_offset(current: int, seen: int, posted: int) -> int:
    """Skip this process's failed rows without hiding them from tomorrow's run."""
    return max(0, int(current)) + max(0, int(seen) - int(posted))


def _nonempty_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


_BOILERPLATE_MARKERS = (
    "използваме бисквитки",
    "настройки на бисквитките",
    "отговорно използване на вашите данни",
    "we use cookies",
    "cookie preferences",
    "privacy preferences",
    "verify you are human",
    "human verification",
    "enable javascript and cookies",
    "checking your browser",
    "access denied",
)


def _is_boilerplate_text(value: Any) -> bool:
    text = _nonempty_text(value).lower()
    return bool(text) and any(marker in text for marker in _BOILERPLATE_MARKERS)


def _structured_signal_count(value: Any) -> int:
    """Count non-empty structured evidence without judging whether values are plausible."""
    if isinstance(value, dict):
        return 1 if any(v not in (None, "", [], {}) for v in value.values()) else 0
    if isinstance(value, list):
        return sum(1 for item in value if item not in (None, "", [], {}))
    return 1 if value not in (None, "", [], {}) else 0


def _document_extension(url: str) -> str:
    path = urlsplit(url or "").path.lower()
    for extension in (".pdf", ".docx"):
        if path.endswith(extension):
            return extension
    return ""


def clean_extracted_media(listing_url: str, extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Remove known publisher chrome from canonical listing media."""
    host = (urlsplit(listing_url or "").hostname or "").lower().removeprefix("www.")
    deny: Tuple[str, ...] = ()
    if host == "sofia.bg":
        deny = ("/image/layout_set_logo", "/o/epsof-0601-theme/")
    elif host == "estates.ubb.bg":
        deny = ("/images/og_image.png", "/images/arrow-top.png")
    elif host == "bbr.bg":
        deny = ("/static/dist/assets/images/default-card-img.png",)
    elif host == "homes.bg":
        deny = ("/logo_homes.",)

    imot_listing_token: Optional[str] = None
    if host == "imot.bg":
        match = re.search(r"/obiava-[^-]*?(\d{12,})-", urlsplit(listing_url).path, re.I)
        if match:
            imot_listing_token = match.group(1)

    address_listing_token: Optional[str] = None
    if host == "address.bg":
        match = re.search(r"offer(\d+)", urlsplit(listing_url).path, re.I)
        if match:
            address_listing_token = match.group(1)

    imoti_net_listing_token: Optional[str] = None
    if host == "imoti.net":
        match = re.search(r"/obiava/(\d+)", urlsplit(listing_url).path, re.I)
        if match:
            imoti_net_listing_token = match.group(1)

    property_image_token: Optional[str] = None
    token_patterns = {
        "suprimmo.bg": r"/imot-(\d+)",
        "property.bg": r"/property-(\d+)",
        "luximmo.com": r"luxury-property-(\d+)",
        "bulgarianproperties.com": r"/AD(\d+)BG_",
    }
    if host in token_patterns:
        match = re.search(token_patterns[host], urlsplit(listing_url).path, re.I)
        if match:
            property_image_token = match.group(1)

    anchored_path_prefix: Optional[str] = None
    if host in {"era.bg", "revolution-estate.bg"}:
        cover_value = extracted.get("image")
        if isinstance(cover_value, str):
            cover_path = urlsplit(cover_value).path.lower()
            anchor_pattern = r"(/offer/\d+/)" if host == "era.bg" else r"(/estate/\d+/)"
            match = re.search(anchor_pattern, cover_path)
            if match:
                anchored_path_prefix = match.group(1)
    elif host == "homes.bg":
        cover_value = extracted.get("image")
        if isinstance(cover_value, str):
            cover_path = urlsplit(cover_value).path.lower()
            if "/" in cover_path.rstrip("/"):
                anchored_path_prefix = cover_path.rsplit("/", 1)[0] + "/"

    focus_listing_token: Optional[str] = None
    if host in {"imot.bg", "imoti.info", "holmes.bg", "bazar.bg"}:
        if imot_listing_token:
            focus_listing_token = imot_listing_token
        else:
            candidates = [extracted.get("image"), *(extracted.get("images") or [])]
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                match = re.search(r"/[0-9][a-z](\d{12,})_[^/]+$", urlsplit(candidate).path, re.I)
                if match and "/photosimotbg/" in candidate.lower():
                    focus_listing_token = match.group(1)
                    break

    path = urlsplit(listing_url).path
    numeric_listing_token: Optional[str] = None
    if host == "mirela.bg":
        match = re.search(r"-(\d+)/?$", path)
        numeric_listing_token = match.group(1) if match else None
    elif host == "sales.bcpea.org":
        match = re.search(r"/properties/(\d+)/?$", path, re.I)
        numeric_listing_token = match.group(1) if match else None
    elif host == "domaza.bg":
        match = re.search(r"-(\d+)-p/?$", path, re.I)
        numeric_listing_token = match.group(1) if match else None
    elif host == "estates.ubb.bg":
        match = re.search(r"/sales/(\d+)/?$", path, re.I)
        numeric_listing_token = match.group(1) if match else None

    imotno_token: Optional[str] = None
    if host == "imotno.bg":
        match = re.search(r"/property/([0-9a-f-]{36})/?$", path, re.I)
        imotno_token = match.group(1).lower() if match else None

    ues_image_prefix: Optional[str] = None
    if host == "ues.bg":
        cover_value = extracted.get("image")
        if isinstance(cover_value, str):
            cover_candidate = unquote(urlsplit(cover_value).query)
            if "url=" in cover_candidate:
                cover_candidate = unquote(
                    cover_candidate.split("url=", 1)[1].split("&", 1)[0]
                )
            else:
                cover_candidate = cover_value
            match = re.search(r"/offers/(offer_[^/?]*?_\d+_)", cover_candidate, re.I)
            if match:
                ues_image_prefix = match.group(1).lower()

    def allowed(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        normalized = value.strip()
        if host == "bbr.bg" and normalized.rstrip("/") == "https://bbr.bg":
            return False
        lowered = normalized.lower()
        if host == "olx.bg":
            # OLX listing galleries use immutable Apollo file URLs. Generic
            # img-resizer URLs on the same page are recommendation cards and
            # advertising banners belonging to other offers.
            media_host = (urlsplit(normalized).hostname or "").lower()
            return (
                (
                    media_host.endswith("apollo.olxcdn.com")
                    or media_host.endswith(".olx.com")
                )
                and "/v1/files/" in urlsplit(normalized).path.lower()
                and "/image" in urlsplit(normalized).path.lower()
            )
        if host == "arcoreal.bg":
            parsed_media = urlsplit(normalized)
            return (
                (parsed_media.hostname or "").lower().removeprefix("www.")
                == "arcoreal.bg"
                and parsed_media.path.rstrip("/").lower() == "/image"
                and bool(re.search(r"(?:^|&)id=\d+(?:&|$)", parsed_media.query))
            )
        if host == "buildingbox.bg":
            parsed_media = urlsplit(normalized)
            media_host = (parsed_media.hostname or "").lower().removeprefix("www.")
            media_path = parsed_media.path.lower()
            return (
                media_host == "buildingbox.bg"
                and "/wp-content/uploads/" in media_path
                and not re.search(r"(?:^|[-_/])logo(?:[-_.\/]|$)", media_path)
            )
        if host == "homes.bg":
            media_host = (urlsplit(normalized).hostname or "").lower()
            return bool(
                re.fullmatch(r"g\d+\.homes\.bg", media_host)
                and anchored_path_prefix
                and anchored_path_prefix in urlsplit(normalized).path.lower()
            )
        if host == "ues.bg":
            parsed_media = urlsplit(normalized)
            candidate = unquote(parsed_media.query)
            if "url=" in candidate:
                candidate = unquote(candidate.split("url=", 1)[1].split("&", 1)[0])
            else:
                candidate = normalized
            return bool(
                ues_image_prefix
                and f"/offers/{ues_image_prefix}" in candidate.lower()
            )
        if host == "novitesgradi.bg":
            parsed_media = urlsplit(normalized)
            media_host = (parsed_media.hostname or "").lower().removeprefix("www.")
            media_path = parsed_media.path.lower()
            return (
                media_host == "novitesgradi.bg"
                and "/wp-content/uploads/" in media_path
                and "/novite_" not in media_path
                and "lazy_placeholder" not in media_path
            )
        if host == "imot.bg" and imot_listing_token:
            # The page contains agency logos and photos from recommended offers.
            # Its own gallery files carry the numeric id from the listing URL.
            return (
                "/photosimotbg/" in lowered
                and imot_listing_token in lowered
            )
        if host in {"imoti.info", "holmes.bg", "bazar.bg"} and focus_listing_token:
            return "/photosimotbg/" in lowered and focus_listing_token in lowered
        if host == "address.bg" and address_listing_token:
            return bool(
                re.search(
                    rf"/offers/\d+/{re.escape(address_listing_token)}/",
                    urlsplit(normalized).path,
                    re.I,
                )
            )
        if host == "imoti.net" and imoti_net_listing_token:
            return f"/obiavi/{imoti_net_listing_token}/" in lowered
        if property_image_token:
            return (
                "property-images" in lowered
                and property_image_token in urlsplit(normalized).path
            )
        if anchored_path_prefix:
            return anchored_path_prefix in urlsplit(normalized).path.lower()
        if host == "home2u.bg":
            return (urlsplit(normalized).hostname or "").lower().endswith(
                "skyholding.media"
            )
        media_path = urlsplit(normalized).path.lower()
        if host == "mirela.bg" and numeric_listing_token:
            return bool(
                re.search(
                    rf"/offers/php/\d+/{re.escape(numeric_listing_token)}/",
                    media_path,
                    re.I,
                )
            )
        if host == "sales.bcpea.org" and numeric_listing_token:
            return f"/upload/{numeric_listing_token}/" in media_path
        if host == "domaza.bg" and numeric_listing_token:
            return f"/{numeric_listing_token}/" in media_path
        if host == "estates.ubb.bg" and numeric_listing_token:
            return f"/attachments/listing/{numeric_listing_token}/" in media_path
        if host == "imotno.bg" and imotno_token:
            # Supabase gallery links on this site are short-lived signed URLs.
            # The per-listing share image is stable and safe to persist.
            return media_path.rstrip("/") == f"/property/{imotno_token}/share-image"
        return not any(part in lowered for part in deny)

    def identity(value: str) -> str:
        parsed = urlsplit(value)
        path = parsed.path.lower()
        if host == "olx.bg":
            match = re.search(r"/v1/files/([^/]+)/image", path)
            if match:
                return f"olx:{match.group(1)}"
        if host == "homes.bg":
            match = re.search(r"/(\d+)[a-z]?\.(?:jpe?g|png|webp)$", path, re.I)
            if match:
                return f"homes:{match.group(1)}"
        if host == "home2u.bg":
            filename = path.rsplit("/", 1)[-1]
            filename = re.sub(
                r"-\d+x\d+(?=\.(?:jpe?g|png|webp)$)", "", filename, flags=re.I
            )
            return f"home2u:{filename}"
        if host == "novitesgradi.bg":
            filename = path.rsplit("/", 1)[-1]
            filename = re.sub(
                r"-\d+x\d+(?=\.(?:jpe?g|png|webp)(?:\.webp)?$)",
                "",
                filename,
                flags=re.I,
            )
            filename = re.sub(
                r"\.(jpe?g|png|webp)\.webp$", r".\1", filename, flags=re.I
            )
            return f"novitesgradi:{filename}"
        if host == "imoti.net" and imoti_net_listing_token:
            parent = path.rsplit("/", 2)[-2] if "/" in path else ""
            filename = path.rsplit("/", 1)[-1]
            filename = re.sub(
                r"^thumb_\d+x\d+_(?:wm_)?", "", filename, flags=re.I
            )
            return f"imoti-net:{parent}:{filename}"
        if host == "address.bg" and address_listing_token:
            return f"address:{path.rsplit('/', 1)[-1].rsplit('.', 1)[0]}"
        if host in {"imot.bg", "imoti.info", "holmes.bg", "bazar.bg"} and "/photosimotbg/" in path:
            return f"focus:{path.rsplit('/', 1)[-1].rsplit('.', 1)[0]}"
        if host == "yavlena.com":
            decoded = unquote(parsed.query)
            match = re.search(r"/([^/?]+\.(?:jpe?g|png|webp))", decoded, re.I)
            if match:
                return f"yavlena:{match.group(1).lower()}"
        if property_image_token:
            match = re.search(
                rf"{re.escape(property_image_token)}_(\d+)\.(?:jpe?g|png|webp)$",
                path,
                re.I,
            )
            if match:
                return f"property-template:{property_image_token}:{match.group(1)}"
        if host == "revolution-estate.bg":
            match = re.search(r"image_(\d+)\.(?:jpe?g|png|webp)$", path, re.I)
            if match:
                return f"revolution:{match.group(1)}"
        if host == "ues.bg":
            decoded = unquote(parsed.query)
            match = re.search(r"(?:^|&)url=([^&]+)", decoded, re.I)
            if match:
                embedded = urlsplit(unquote(match.group(1)))
                return f"ues:{embedded.path.lower()}"
            if path.startswith("/offers/"):
                return f"ues:{path}"
        return value

    images: List[str] = []
    identities: Set[str] = set()
    for value in extracted.get("images") or []:
        if allowed(value):
            normalized = value.strip()
            key = identity(normalized)
            if key not in identities:
                identities.add(key)
                images.append(normalized)
    cover = extracted.get("image")
    extracted["images"] = images
    extracted["image"] = cover.strip() if allowed(cover) else (images[0] if images else None)
    return extracted


def _docx_text(content: bytes) -> str:
    """Extract readable text from a DOCX without needing a full office suite."""
    with zipfile.ZipFile(BytesIO(content)) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    paragraphs: List[str] = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        pieces = [
            node.text or ""
            for node in paragraph.iter()
            if node.tag.endswith("}t") and node.text
        ]
        text = "".join(pieces).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


async def extract_document_detail(url: str, rule: "DomainRule") -> Dict[str, Any]:
    """Download and extract municipal PDF/DOCX auction notices."""
    extension = _document_extension(url)
    if not extension:
        raise RuntimeError("unsupported document type")

    max_bytes = 30 * 1024 * 1024
    headers = {
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(
        timeout=max(15.0, rule.timeout_ms / 1000.0),
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > max_bytes:
            raise RuntimeError(f"document exceeds {max_bytes} bytes")
        content = response.content

    metadata: Dict[str, Any] = {
        "document_type": extension.lstrip("."),
        "content_bytes": len(content),
    }
    if extension == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        parts = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join(part for part in parts if part)
        metadata["page_count"] = len(reader.pages)
        pdf_meta = reader.metadata or {}
        title = str(pdf_meta.get("/Title") or "").strip()
    else:
        text = _docx_text(content)
        title = ""

    text = text.strip()
    if not text:
        raise RuntimeError("document contains no extractable text")

    if not title:
        title = unquote(urlsplit(url).path.rsplit("/", 1)[-1])

    # Keep a compact legacy description and a substantially larger raw evidence
    # block. This avoids unbounded API payloads while retaining the actual notice.
    return {
        "ok": True,
        "title": title[:512],
        "description": text[:50_000],
        "image": None,
        "images": [],
        "raw_kv": [{"label": key, "value": value} for key, value in metadata.items()],
        "raw_text_blocks": [
            {"type": "auction_document", "text": text[:500_000]}
        ],
        "signals": metadata,
    }


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
    if len(desc) >= desc_threshold and not _is_boilerplate_text(desc):
        return True, f"description:{len(desc)}"

    # Some sites expose the useful body as text blocks rather than description.
    if isinstance(text_blocks, list):
        block_text_len = 0
        for block in text_blocks:
            if isinstance(block, str):
                if not _is_boilerplate_text(block):
                    block_text_len += len(block.strip())
            elif isinstance(block, dict):
                for key in ("text", "value", "content", "label"):
                    value = block.get(key)
                    if isinstance(value, str) and not _is_boilerplate_text(value):
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
    request_delay_ms: int
    min_desc_len: int
    wait_until: str
    timeout_ms: int
    extractor_script: str
    description_selectors: List[str]
    image_selectors: List[str]
    inactive_selectors: List[str]
    inactive_regex: List[re.Pattern]
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
            request_delay_ms=max(
                0, min(60_000, _safe_int(dct.get("request_delay_ms"), 250))
            ),
            min_desc_len=max(0, _safe_int(dct.get("min_desc_len"), 60)),
            wait_until=str(dct.get("wait_until") or "domcontentloaded"),
            timeout_ms=max(
                5_000, min(180_000, _safe_int(dct.get("timeout_ms"), 45_000))
            ),
            extractor_script=str(
                dct.get("extractor_script") or "/app/scraper/detail_extractor.js"
            ),
            description_selectors=[
                str(value).strip()
                for value in (dct.get("description_selectors") or [])
                if isinstance(value, str) and value.strip()
            ],
            image_selectors=[
                str(value).strip()
                for value in (dct.get("image_selectors") or [])
                if isinstance(value, str) and value.strip()
            ],
            inactive_selectors=[
                str(value).strip()
                for value in (dct.get("inactive_selectors") or [])
                if isinstance(value, str) and value.strip()
            ],
            inactive_regex=[
                re.compile(value, re.I)
                for value in (dct.get("inactive_regex") or [])
                if isinstance(value, str) and value.strip()
            ],
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
        offset: int = 0,
    ) -> List[str]:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")

        params: Dict[str, Any] = {
            "limit": int(limit),
            "offset": max(0, int(offset)),
        }
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

    async def deactivate_listings(self, urls: List[str], reason: str) -> int:
        if self._client is None:
            raise RuntimeError("ApiClient not initialized")
        if not urls:
            return 0
        response = await self._client.post(
            f"{self.api_base}/api/v1/listings/deactivate",
            headers=self._auth_headers(),
            json={"urls": urls, "reason": reason[:200]},
        )
        response.raise_for_status()
        data = response.json()
        return int(data.get("deactivated") or 0)


class DeadListingError(RuntimeError):
    """A definitive listing-level absence, safe to deactivate in the catalogue."""


class PublisherBlockedError(RuntimeError):
    """Publisher-level throttling/challenge; never evidence of an inactive ad."""


class IncompleteDetailError(RuntimeError):
    """The page loaded, but extraction produced no listing-specific evidence."""


class DomainCircuitBreaker:
    """Stop a batch when repeated failures show a publisher-wide problem.

    This is deliberately scoped to one ``run_once`` call. A later scheduled run
    gets a clean probe, while the current run cannot hammer a blocked publisher.
    """

    def __init__(self, blocked_threshold: int = 2, incomplete_threshold: int = 5):
        self.blocked_threshold = max(1, int(blocked_threshold))
        self.incomplete_threshold = max(1, int(incomplete_threshold))
        self._failures: Dict[str, Dict[str, int]] = {}
        self._open: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def reason(self, domain: str) -> Optional[str]:
        async with self._lock:
            return self._open.get(domain)

    async def success(self, domain: str) -> None:
        async with self._lock:
            self._failures.pop(domain, None)

    async def failure(self, domain: str, category: str, detail: str) -> bool:
        async with self._lock:
            counts = self._failures.setdefault(domain, {})
            counts[category] = counts.get(category, 0) + 1
            threshold = (
                self.blocked_threshold
                if category == "publisher_blocked"
                else self.incomplete_threshold
            )
            if counts[category] >= threshold:
                self._open[domain] = f"{category}: {detail}"
                return True
            return False

    async def opened(self) -> Dict[str, str]:
        async with self._lock:
            return dict(self._open)


class DomainPacer:
    """Guarantee a minimum gap between navigation starts for each publisher."""

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._next_start: Dict[str, float] = {}

    async def wait(self, domain: str, delay_ms: int) -> None:
        delay_s = max(0, int(delay_ms)) / 1000.0
        if delay_s <= 0:
            return
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            remaining = self._next_start.get(domain, 0.0) - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._next_start[domain] = loop.time() + delay_s


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

    async def extract_detail(
        self,
        page: Page,
        script_path: str,
        description_selectors: Optional[List[str]] = None,
        image_selectors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Executes the in-page extractor and returns the full extraction dict.
        The extractor should return at minimum: {ok, title, description, image, images}
        and may additionally return v2 raw harvest fields.
        """
        await self.ensure(page, script_path)
        out = await page.evaluate(
            "options => window.__listingDetailExtractor.extract(options)",
            {
                "descriptionSelectors": description_selectors or [],
                "imageSelectors": image_selectors or [],
            },
        )
        if not isinstance(out, dict) or not out.get("ok"):
            raise RuntimeError("Detail extraction failed")

        # Basic normalization for core fields
        if isinstance(out.get("description"), str):
            out["description"] = out["description"].strip()
            if _is_boilerplate_text(out["description"]):
                out["description"] = ""
                signals = out.get("signals")
                if not isinstance(signals, dict):
                    signals = {}
                    out["signals"] = signals
                signals["description_rejected"] = "boilerplate"
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
    raw_jsonld_input = extracted.get("raw_jsonld") or []
    raw_jsonld = []
    pending_jsonld = list(raw_jsonld_input) if isinstance(raw_jsonld_input, list) else []
    while pending_jsonld:
        block = pending_jsonld.pop(0)
        if isinstance(block, dict):
            raw_jsonld.append(block)
        elif isinstance(block, list):
            pending_jsonld[0:0] = block
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
        response = await page.goto(
            url, wait_until=rule.wait_until, timeout=rule.timeout_ms
        )
        if response is None:
            raise RuntimeError("detail navigation returned no response")
        await page.wait_for_timeout(350)
        await _auto_scroll(page)
        await page.wait_for_timeout(250)

        final_url = page.url or url
        if canonical_domain(final_url) != canonical_domain(url):
            # External bot-manager/consent challenges are common and are not
            # proof that the publisher removed the listing. Keep them retryable.
            raise PublisherBlockedError(
                f"detail redirected across domains: {final_url}"
            )
        if not rule.allows_url(final_url):
            raise DeadListingError(f"detail redirected off listing path: {final_url}")
        rendered = await _stable_page_content(page)
        health = classify_response(
            original_url=url,
            final_url=final_url,
            status_code=response.status,
            body=rendered,
            content_type=(await response.all_headers()).get("content-type", ""),
            body_expected=True,
        )
        if health.state == "dead":
            raise DeadListingError(
                f"detail page is dead: {health.reason} status={health.status_code}"
            )
        if not health.valid:
            message = (
                f"detail page is {health.state}: {health.reason} "
                f"status={health.status_code}"
            )
            if health.status_code in {403, 429, 520} or health.reason == "block_page":
                raise PublisherBlockedError(message)
            raise RuntimeError(message)

        if rule.inactive_selectors and rule.inactive_regex:
            for selector in rule.inactive_selectors:
                try:
                    values = await page.locator(selector).all_inner_texts()
                except Exception:
                    continue
                marker_text = " ".join(value.strip() for value in values if value.strip())
                if any(pattern.search(marker_text) for pattern in rule.inactive_regex):
                    raise DeadListingError(
                        f"listing has publisher inactive marker in {selector}: "
                        f"{marker_text[:160]}"
                    )

        extracted = await extractor.extract_detail(
            page,
            rule.extractor_script,
            rule.description_selectors,
            rule.image_selectors,
        )
        extracted = clean_extracted_media(url, extracted)
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
            raise IncompleteDetailError(reason)

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
        confirmed: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        try:
            results = (
                await api.post_extractions_batch(chunk)
                if batch_size > 1
                else [await api.post_extraction(chunk[0])]
            )
            confirmed = list(zip(chunk, results))
        except Exception as e:
            # Fallback: try individual posts so one bad payload doesn't nuke the whole batch.
            print(f"⚠️ batch post failed ({len(chunk)} items): {e} -> trying individual")
            for p in chunk:
                try:
                    confirmed.append((p, await api.post_extraction(p)))
                except Exception as e2:
                    print(f"❌ post failed: {p.get('sourceUrl')} -> {e2}")

        for payload, r in confirmed:
            rid = r.get("id") if isinstance(r, dict) else None
            if isinstance(rid, int):
                ids.append(rid)
                # A detail write updates canonical listing state without adding
                # another historical index run, so the API receipt id may point
                # to that row's original index run. Log the submitted listing,
                # which is the object actually updated.
                src = payload.get("sourceUrl")
                if isinstance(src, str) and src:
                    print(f"📌 posted: id={rid} url={src}")
                else:
                    print(f"📌 posted: id={rid}")

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
    queue_offset: int = 0,
    circuit: Optional[DomainCircuitBreaker] = None,
    pacer: Optional[DomainPacer] = None,
) -> Tuple[int, int, int, Optional[int]]:
    """Returns (urls_seen, urls_scraped, urls_deactivated, last_id)."""

    default_rule, domain_rules = load_rules(rules_file)

    async with ApiClient(api_base, api_key) as api:
        urls = await api.get_detail_queue(
            domain=queue_domain,
            url_contains=queue_url_contains,
            limit=min(queue_limit, max_urls),
            offset=queue_offset,
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
            return (len(urls), 0, 0, None)

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
            dead_urls: List[str] = []
            # Drain mode passes process-lifetime instances so a domain blocked
            # in one queue page cannot be probed again in the next page.
            circuit = circuit or DomainCircuitBreaker()
            pacer = pacer or DomainPacer()

            async def worker(u: str, rule: DomainRule) -> Optional[Dict[str, Any]]:
                async with global_sem:
                    async with domain_sems[rule.domain]:
                        open_reason = await circuit.reason(rule.domain)
                        if open_reason:
                            # The batch summary reports the open circuit once.
                            # Per-URL messages would generate millions of noisy
                            # lines while a large publisher is being skipped.
                            return None
                        attempts = max(1, min(5, int(retry_attempts)))
                        last_error: Optional[str] = None

                        for attempt in range(1, attempts + 1):
                            try:
                                await pacer.wait(rule.domain, rule.request_delay_ms)
                                if _document_extension(u):
                                    extracted = await extract_document_detail(u, rule)
                                    payload = make_detail_payload(u, extracted)
                                else:
                                    payload = await scrape_one(pool, extractor, rule, u)
                                if isinstance(payload, dict):
                                    await circuit.success(rule.domain)
                                    items = payload.get("items") or []
                                    raw = ""
                                    if items and isinstance(items[0], dict):
                                        raw = items[0].get("rawText") or ""
                                    raw_len = len(raw) if isinstance(raw, str) else 0
                                    print(f"✅ scraped: {u} (rawText_len={raw_len}, attempt={attempt})")
                                    return payload

                                last_error = "no meaningful detail content"
                            except DeadListingError as e:
                                await circuit.success(rule.domain)
                                dead_urls.append(u)
                                print(f"🗑️ confirmed inactive: {u} -> {e}")
                                return None
                            except PublisherBlockedError as e:
                                last_error = str(e)
                                opened = await circuit.failure(
                                    rule.domain, "publisher_blocked", last_error
                                )
                                print(
                                    f"🛡️ publisher response deferred without retry: "
                                    f"{u} -> {last_error}"
                                )
                                if opened:
                                    print(
                                        f"⏸️ opened domain circuit for {rule.domain}: "
                                        f"{last_error}"
                                    )
                                return None
                            except Exception as e:
                                last_error = str(e)

                            if attempt < attempts:
                                delay = max(0.0, float(retry_delay_s)) * attempt
                                print(
                                    f"↻ detail retry {attempt + 1}/{attempts}: {u} "
                                    f"after {delay:.1f}s ({last_error})"
                                )
                                await asyncio.sleep(delay)

                        category = (
                            "incomplete_detail"
                            if isinstance(last_error, str)
                            and last_error.startswith("insufficient_content:")
                            else "runtime_failure"
                        )
                        opened = await circuit.failure(
                            rule.domain, category, last_error or "unknown error"
                        )
                        print(f"❌ detail failed after {attempts} attempts: {u} -> {last_error}")
                        if opened:
                            print(
                                f"⏸️ opened domain circuit for {rule.domain}: "
                                f"{category}: {last_error}"
                            )
                        return None

            payloads = await asyncio.gather(
                *[asyncio.create_task(worker(u, rule)) for (u, rule) in final]
            )
            opened_circuits = await circuit.opened()
            for domain, reason in sorted(opened_circuits.items()):
                print(f"🛡️ domain deferred for next run: {domain} -> {reason}")
            await pool.close()
            await browser.close()

        good_payloads = [p for p in payloads if isinstance(p, dict)]
        ids = await post_payloads(api, good_payloads, post_batch_size)
        deactivated = await api.deactivate_listings(
            list(dict.fromkeys(dead_urls)), "detail_confirmed_dead"
        )
        if deactivated:
            print(f"🗑️ deactivated confirmed-dead listings: {deactivated}")

    last_id = ids[-1] if ids else None
    return (len(urls), len(ids), deactivated, last_id)


async def main_async(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Detail runner (multi-domain): pulls listing URLs from the DB queue "
            "and stores the latest raw detail snapshot on the canonical listing "
            "without rewriting historical index extraction rows."
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

    parser.add_argument(
        "--balanced-per-domain",
        type=int,
        default=int(os.getenv("DETAIL_BALANCED_PER_DOMAIN", "0")),
        help=(
            "Audit up to this many pending rows independently for every configured "
            "domain. Zero disables balanced mode."
        ),
    )

    args = parser.parse_args(argv)

    queue_limit = max(1, min(500, int(args.queue_limit)))
    max_urls = max(1, min(500, int(args.max_urls)))

    total_seen = 0
    total_done = 0
    total_deactivated = 0
    last_id: Optional[int] = None
    queue_offset = 0

    print(
        f"DETAIL CONCURRENCY: {max(1, min(20, int(args.global_concurrency)))} | "
        f"retry_attempts={max(1, min(5, int(args.retry_attempts)))}"
    )

    balanced_limit = max(0, min(500, int(args.balanced_per_domain)))
    if balanced_limit:
        _default_rule, configured_rules = load_rules(args.rules)
        domains = (
            [args.queue_domain]
            if args.queue_domain
            else list(dict.fromkeys(rule.domain for rule in configured_rules))
        )
        results: List[Tuple[str, int, int, int]] = []
        for domain in domains:
            print(f"\n🔬 balanced domain audit: {domain} (limit={balanced_limit})")
            seen, done, deactivated, lid = await run_once(
                api_base=args.api_base,
                api_key=args.api_key,
                queue_domain=domain,
                queue_url_contains=args.queue_url_contains,
                queue_limit=balanced_limit,
                max_urls=balanced_limit,
                rules_file=args.rules,
                global_concurrency=args.global_concurrency,
                post_batch_size=args.post_batch_size,
                retry_attempts=args.retry_attempts,
                retry_delay_s=args.retry_delay_s,
                queue_offset=0,
            )
            results.append((domain, seen, done, deactivated))
            total_seen += seen
            total_done += done
            total_deactivated += deactivated
            last_id = lid or last_id
            print(
                f"AUDIT_RESULT domain={domain} sampled={seen} "
                f"posted={done} deactivated={deactivated} "
                f"deferred={max(0, seen - done - deactivated)}"
            )

        print("\nBALANCED_AUDIT_SUMMARY")
        for domain, seen, done, deactivated in results:
            print(
                f"{domain}\tsampled={seen}\tposted={done}\t"
                f"deactivated={deactivated}\t"
                f"deferred={max(0, seen - done - deactivated)}"
            )
        print(
            f"✅ detail total: posted={total_done}, "
            f"deactivated={total_deactivated}, sampled={total_seen}"
        )
        if last_id is not None:
            print(f"last_extraction_id={last_id}")
        return 0 if (total_done + total_deactivated) > 0 else 2

    drain_circuit = DomainCircuitBreaker()
    drain_pacer = DomainPacer()

    while True:
        print(
            f"▶️ queue request: domain={args.queue_domain!r}, "
            f"contains={args.queue_url_contains!r}, limit={min(queue_limit, max_urls)}, "
            f"offset={queue_offset}"
        )

        seen, done, deactivated, lid = await run_once(
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
            queue_offset=queue_offset,
            circuit=drain_circuit,
            pacer=drain_pacer,
        )

        total_seen += seen
        total_done += done
        total_deactivated += deactivated
        last_id = lid or last_id

        print(
            f"✅ batch done: scraped={done}, deactivated={deactivated}, "
            f"queue_returned={seen}"
        )

        if not args.drain:
            break

        # drain mode: stop when API returns 0 candidates
        if seen == 0:
            break

        # Successful rows leave the pending queue. Failed/rule-rejected rows do
        # not, so advance past exactly those rows for the remainder of this run.
        # The next scheduled process starts at zero and retries them.
        handled = done + deactivated
        failed_this_batch = max(0, seen - handled)
        queue_offset = _next_queue_offset(queue_offset, seen, handled)
        print(
            f"➡️ drain progress: posted={done}, deferred={failed_this_batch}, "
            f"next_offset={queue_offset}"
        )
        await asyncio.sleep(max(0.0, float(args.batch_sleep_s)))

    print(
        f"✅ detail total: posted={total_done}, "
        f"deactivated={total_deactivated}"
    )
    if last_id is not None:
        print(f"last_extraction_id={last_id}")

    return 0 if (total_done + total_deactivated) > 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(main_async(os.sys.argv[1:])))


if __name__ == "__main__":
    main()

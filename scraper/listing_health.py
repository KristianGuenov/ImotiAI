from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
import html
import os
import time
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession


_DEFINITIVE_DEAD_STATUSES = {404, 410}
_TRANSIENT_STATUSES = {401, 403, 408, 425, 429}

_DEAD_TITLE_TEXT = re.compile(
    r"(?:"
    r"page\s+not\s+found|not\s+found|error\s*404|"
    r"страницата\s+(?:не\s+е\s+намерена|не\s+съществува)|"
    r"обявата\s+(?:не\s+е\s+намерена|не\s+съществува|е\s+изтрита|е\s+неактивна|вече\s+не\s+е\s+активна)|"
    r"офертата\s+(?:не\s+съществува|е\s+изтрита|е\s+неактивна|вече\s+не\s+е\s+активна)|"
    r"имотът\s+(?:не\s+съществува|е\s+неактивен)|"
    r"няма\s+такава\s+(?:обява|оферта)"
    r")",
    re.I,
)

_DEAD_BODY_TEXT = re.compile(
    r"(?:"
    r"страницата\s+(?:не\s+е\s+намерена|не\s+съществува)|"
    r"обявата\s+(?:не\s+е\s+намерена|не\s+съществува|е\s+изтрита|е\s+неактивна|вече\s+не\s+е\s+активна)|"
    r"офертата\s+(?:не\s+съществува|е\s+изтрита|е\s+неактивна|вече\s+не\s+е\s+активна)|"
    r"имотът\s+(?:не\s+съществува|е\s+неактивен)|"
    r"няма\s+такава\s+(?:обява|оферта)"
    r")",
    re.I,
)

_BLOCK_TEXT = re.compile(
    r"(?:just\s+a\s+moment|attention\s+required|access\s+denied|"
    r"radware\s+page|checking\s+your\s+browser|verify\s+you\s+are\s+human|"
    r"captcha|temporarily\s+unavailable|service\s+unavailable)",
    re.I,
)


def canonical_domain(url: str) -> str:
    host = (urlsplit(url or "").hostname or "").lower().strip()
    return host.removeprefix("www.")


@dataclass(frozen=True)
class HealthVerdict:
    state: str  # valid | dead | unverifiable
    reason: str
    status_code: Optional[int] = None
    final_url: Optional[str] = None

    @property
    def valid(self) -> bool:
        return self.state == "valid"


@dataclass
class ValidationStats:
    checked: int = 0
    accepted: int = 0
    dead: int = 0
    unverifiable: int = 0
    reasons: Counter = None

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = Counter()

    def add(self, verdict: HealthVerdict) -> None:
        self.checked += 1
        if verdict.state == "valid":
            self.accepted += 1
        elif verdict.state == "dead":
            self.dead += 1
        else:
            self.unverifiable += 1
        self.reasons[verdict.reason] += 1


def _decode_html(body: bytes, content_type: str = "") -> str:
    charset_match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings.extend(["utf-8", "windows-1251"])
    best = ""
    for encoding in dict.fromkeys(encodings):
        try:
            decoded = body.decode(encoding, "replace")
        except LookupError:
            continue
        if not best or decoded.count("\ufffd") < best.count("\ufffd"):
            best = decoded
        if "\ufffd" not in decoded:
            break
    return html.unescape(best)


def _extract_title(body: bytes, content_type: str = "") -> str:
    text = _decode_html(body, content_type)
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return value[:1000]


def _matches_final_listing_url(
    original_url: str,
    final_url: str,
    listing_url_regex: Optional[str],
) -> bool:
    if not listing_url_regex:
        return True
    matcher = re.compile(listing_url_regex, re.I)
    if matcher.search(final_url):
        return True

    # A number of Bulgarian portals canonicalize www/non-www while preserving
    # the listing path. Rebuild the final path on the original authority so a
    # host-only canonical redirect does not become a false rejection.
    original = urlsplit(original_url)
    final = urlsplit(final_url)
    comparable = urlunsplit(
        (original.scheme, original.netloc, final.path, final.query, "")
    )
    return bool(matcher.search(comparable))


def classify_response(
    *,
    original_url: str,
    final_url: str,
    status_code: int,
    body: bytes = b"",
    content_type: str = "",
    listing_url_regex: Optional[str] = None,
    body_expected: bool = True,
) -> HealthVerdict:
    """Classify a listing response without confusing transient blocks with death."""
    if status_code in _DEFINITIVE_DEAD_STATUSES:
        return HealthVerdict("dead", f"http_{status_code}", status_code, final_url)
    if status_code in _TRANSIENT_STATUSES or status_code >= 500:
        return HealthVerdict(
            "unverifiable", f"http_{status_code}", status_code, final_url
        )
    if status_code < 200 or status_code >= 400:
        return HealthVerdict("dead", f"http_{status_code}", status_code, final_url)

    if canonical_domain(original_url) != canonical_domain(final_url):
        return HealthVerdict("dead", "cross_domain_redirect", status_code, final_url)
    if not _matches_final_listing_url(
        original_url, final_url, listing_url_regex
    ):
        return HealthVerdict("dead", "redirected_off_listing", status_code, final_url)

    if not body_expected:
        return HealthVerdict("valid", "http_head", status_code, final_url)

    if len(body) < 64:
        return HealthVerdict("unverifiable", "empty_response", status_code, final_url)

    lowered_type = (content_type or "").lower()
    if "pdf" in lowered_type or urlsplit(final_url).path.lower().endswith(".pdf"):
        if body.lstrip().startswith(b"%PDF"):
            return HealthVerdict("valid", "pdf", status_code, final_url)
        return HealthVerdict("unverifiable", "invalid_pdf", status_code, final_url)

    title = _extract_title(body, content_type)
    if title and _DEAD_TITLE_TEXT.search(title):
        return HealthVerdict("dead", "soft_404_title", status_code, final_url)
    if title and _BLOCK_TEXT.search(title):
        return HealthVerdict("unverifiable", "block_page", status_code, final_url)

    # Strong inactive phrases are useful in the document body, but generic words
    # such as "sold" are deliberately not used because they occur in live copy.
    sample = _decode_html(body[:131072], content_type)
    if _DEAD_BODY_TEXT.search(sample):
        return HealthVerdict("dead", "soft_404_body", status_code, final_url)

    return HealthVerdict("valid", "http_get", status_code, final_url)


class ListingHealthValidator:
    """Bounded pre-insert validation shared by every index target."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        global_concurrency: Optional[int] = None,
        per_domain_concurrency: Optional[int] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        read_bytes: int = 131072,
    ):
        self.enabled = bool(enabled)
        self.global_concurrency = max(
            1,
            int(
                global_concurrency
                if global_concurrency is not None
                else os.getenv("LISTING_VALIDATION_CONCURRENCY", "24")
            ),
        )
        self.per_domain_concurrency = max(
            1,
            int(
                per_domain_concurrency
                if per_domain_concurrency is not None
                else os.getenv("LISTING_VALIDATION_DOMAIN_CONCURRENCY", "4")
            ),
        )
        self.timeout_s = max(
            3.0,
            float(
                timeout_s
                if timeout_s is not None
                else os.getenv("LISTING_VALIDATION_TIMEOUT_S", "20")
            ),
        )
        self.retries = max(
            1,
            min(
                4,
                int(
                    retries
                    if retries is not None
                    else os.getenv("LISTING_VALIDATION_RETRIES", "3")
                ),
            ),
        )
        self.read_bytes = max(4096, min(int(read_bytes), 524288))
        self.head_delay_s = max(
            0.0,
            float(os.getenv("LISTING_VALIDATION_HEAD_DELAY_MS", "20"))
            / 1000.0,
        )
        self.get_delay_s = max(
            0.0,
            float(os.getenv("LISTING_VALIDATION_GET_DELAY_MS", "100"))
            / 1000.0,
        )
        self._global_sem = asyncio.Semaphore(self.global_concurrency)
        self._domain_sems: Dict[str, asyncio.Semaphore] = {}
        self._domain_rate_locks: Dict[str, asyncio.Lock] = {}
        self._domain_next_start: Dict[str, float] = {}
        self._stats: Dict[str, ValidationStats] = defaultdict(ValidationStats)
        self._curl_fallback_domains = {
            value.strip().lower().removeprefix("www.")
            for value in os.getenv(
                "LISTING_VALIDATION_CURL_DOMAINS", "address.bg"
            ).split(",")
            if value.strip()
        }
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(
                max_connections=self.global_concurrency,
                max_keepalive_connections=self.global_concurrency,
            ),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/124.0.0.0 Safari/537.36 ImotiAI/1.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.7",
            },
        )
        curl_options = dict(
            impersonate="chrome",
            max_clients=self.global_concurrency,
        )
        proxy_url = (os.getenv("SCRAPER_PROXY_URL") or "").strip()
        if proxy_url:
            curl_options["proxy"] = proxy_url
        self._curl_client = CurlAsyncSession(**curl_options)

    async def close(self) -> None:
        await self._client.aclose()
        await self._curl_client.close()

    def stats_for(self, target_name: str) -> ValidationStats:
        return self._stats[target_name]

    def _domain_sem(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._domain_sems:
            self._domain_sems[domain] = asyncio.Semaphore(
                self.per_domain_concurrency
            )
        return self._domain_sems[domain]

    async def _pace_domain(self, domain: str, method: str) -> None:
        delay_s = self.get_delay_s if method == "GET" else self.head_delay_s
        if delay_s <= 0:
            return
        lock = self._domain_rate_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_s = self._domain_next_start.get(domain, now) - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            self._domain_next_start[domain] = (
                time.monotonic() + delay_s
            )

    async def _read_response(
        self,
        method: str,
        url: str,
        listing_url_regex: Optional[str],
    ) -> HealthVerdict:
        if canonical_domain(url) in self._curl_fallback_domains:
            return await self._read_response_curl(
                method, url, listing_url_regex
            )
        headers = {"Range": f"bytes=0-{self.read_bytes - 1}"} if method == "GET" else {}
        try:
            await self._pace_domain(canonical_domain(url), method)
            async with self._client.stream(method, url, headers=headers) as response:
                body = b""
                if method == "GET" and 200 <= response.status_code < 400:
                    pieces = []
                    total = 0
                    async for piece in response.aiter_bytes():
                        if not piece:
                            continue
                        remaining = self.read_bytes - total
                        pieces.append(piece[:remaining])
                        total += min(len(piece), remaining)
                        if total >= self.read_bytes:
                            break
                    body = b"".join(pieces)
                verdict = classify_response(
                    original_url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    body=body,
                    content_type=response.headers.get("content-type", ""),
                    listing_url_regex=listing_url_regex,
                    body_expected=method == "GET",
                )
                if (
                    verdict.state == "unverifiable"
                    and verdict.status_code in {403, 429}
                    and canonical_domain(url) in self._curl_fallback_domains
                ):
                    return await self._read_response_curl(
                        method, url, listing_url_regex
                    )
                return verdict
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return HealthVerdict(
                "unverifiable", type(exc).__name__.lower(), None, url
            )
        except Exception as exc:
            return HealthVerdict(
                "unverifiable", f"request_{type(exc).__name__.lower()}", None, url
            )

    async def _read_response_curl(
        self,
        method: str,
        url: str,
        listing_url_regex: Optional[str],
    ) -> HealthVerdict:
        """Fallback for a small allowlist of CDNs that reject httpx TLS.

        This does not bypass a challenge: a remaining 403/429 is still
        unverifiable. It only repeats the public request using the curl TLS
        stack that the same publisher accepts for ordinary browser traffic.
        """
        try:
            headers = (
                {"Range": f"bytes=0-{self.read_bytes - 1}"}
                if method == "GET"
                else None
            )
            response = await self._curl_client.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout_s,
                allow_redirects=True,
            )
            return classify_response(
                original_url=url,
                final_url=str(response.url),
                status_code=int(response.status_code),
                body=(response.content or b"")[: self.read_bytes],
                content_type=response.headers.get("content-type", ""),
                listing_url_regex=listing_url_regex,
                body_expected=method == "GET",
            )
        except Exception as exc:
            return HealthVerdict(
                "unverifiable", f"curl_{type(exc).__name__.lower()}", None, url
            )

    async def check(
        self,
        url: str,
        *,
        mode: str,
        listing_url_regex: Optional[str],
    ) -> HealthVerdict:
        if mode == "source":
            return HealthVerdict("valid", "authoritative_source_feed", None, url)

        domain = canonical_domain(url)
        # Take the narrow per-domain slot first. Acquiring the global slot first
        # lets a large batch from one host occupy every global permit while most
        # tasks merely wait for that host's semaphore, starving unrelated sites.
        async with self._domain_sem(domain):
            async with self._global_sem:
                last = HealthVerdict("unverifiable", "not_checked", None, url)
                for attempt in range(1, self.retries + 1):
                    last = await self._read_response(
                        "HEAD" if mode == "head" else "GET",
                        url,
                        listing_url_regex,
                    )
                    # Some servers do not implement HEAD correctly. Use GET as the
                    # final fallback for an ambiguous HEAD result.
                    if mode == "head" and last.state == "unverifiable":
                        last = await self._read_response(
                            "GET", url, listing_url_regex
                        )
                    if last.state != "unverifiable":
                        return last
                    if attempt < self.retries:
                        await asyncio.sleep(1.5 * attempt)
                return last

    async def filter_payload(
        self,
        payload: Dict[str, Any],
        target: Any,
    ) -> Dict[str, Any]:
        items = payload.get("items") if isinstance(payload, dict) else None
        if not self.enabled or not isinstance(items, list) or not items:
            return payload

        configured_mode = str(getattr(target, "validation_mode", "auto") or "auto")
        mode = configured_mode.lower().strip()
        if mode == "auto":
            mode = "head"
        if mode not in {"head", "get", "source"}:
            raise ValueError(f"Unsupported listing validation mode: {mode}")

        listing_url_regex = getattr(target, "listing_url_regex", None)
        unique: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url and url not in unique:
                unique[url] = item

        verdicts = await asyncio.gather(
            *(
                self.check(
                    url,
                    mode=mode,
                    listing_url_regex=listing_url_regex,
                )
                for url in unique
            )
        )

        accepted = []
        batch_reasons: Counter = Counter()
        stats = self._stats[str(getattr(target, "name", "unknown"))]
        for (url, item), verdict in zip(unique.items(), verdicts):
            stats.add(verdict)
            batch_reasons[verdict.reason] += 1
            if verdict.valid:
                accepted.append(item)

        out = dict(payload)
        out["items"] = accepted
        meta = dict(out.get("meta") or {})
        meta["listingValidation"] = {
            "mode": mode,
            "checked": len(unique),
            "accepted": len(accepted),
            "dead": sum(1 for v in verdicts if v.state == "dead"),
            "unverifiable": sum(
                1 for v in verdicts if v.state == "unverifiable"
            ),
            "reasons": dict(batch_reasons.most_common()),
            # The API uses these only to heartbeat already-known active rows.
            # They are never inserted as new listings.
            "unverifiableUrls": [
                url
                for url, verdict in zip(unique, verdicts)
                if verdict.state == "unverifiable"
            ],
        }
        out["meta"] = meta
        print(
            f"[{getattr(target, 'name', 'unknown')}] VALIDATE mode={mode} "
            f"checked={len(unique)} accepted={len(accepted)} "
            f"dead={meta['listingValidation']['dead']} "
            f"unverifiable={meta['listingValidation']['unverifiable']} "
            f"reasons={dict(batch_reasons.most_common(6))}"
        )
        return out

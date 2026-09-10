from __future__ import annotations

import csv
import hashlib
import io
import json
from sqlalchemy import text
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import desc, exists, func, select, update
from sqlalchemy.orm import Session, aliased

from .models import ExtractionItem, ExtractionRun, InventoryCycle, Listing
from .schemas import (
    ExtractedItem,
    ExtractionIn,
    ExtractionBatchOut,
    ExtractionListItem,
    ExtractionListOut,
    ExtractionOut,
    InventoryCycleCompleteIn,
    InventoryCycleCompleteOut,
    InventoryCycleStartIn,
    InventoryCycleStartOut,
)


def _sanitize(value):
    """Replace invalid Unicode (e.g., unpaired surrogates) before DB insert."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    return value


def _detail_value_has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_detail_value_has_content(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_detail_value_has_content(v) for v in value)
    return True


def _detail_payload_has_meaningful_content(first: Any) -> bool:
    """
    Server-side safety check for detail writes.

    Extraction is deliberately permissive: missing images and implausible business
    values do not make a detail scrape invalid. We only reject payloads that contain
    no meaningful text/structured evidence at all.
    """
    if first is None:
        return False

    for field in (
        "rawText",
        "description",
        "raw_text_blocks",
        "raw_kv",
        "raw_jsonld",
        "raw_state_blobs",
        "raw_contacts",
        "signals",
    ):
        if _detail_value_has_content(getattr(first, field, None)):
            return True
    return False


def _normalize_url_list(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, (tuple, set)):
        value = list(value)
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for v in value:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        out.append(s)
    # de-dupe while preserving order
    seen: Set[str] = set()
    deduped: List[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def _apply_images_to_listing(session: Session, *, item_url: str, cover: Optional[str], images: List[str]) -> None:
    """Persist cover + images to listings, even if the ORM model lacks the images column."""
    # cover is optional; don't overwrite with NULL
    session.execute(
        text(
            """
            UPDATE listings
            SET
              image = COALESCE(:cover, image),
              images = CAST(:images AS text[])
            WHERE item_url = :item_url
            """
        ),
        {"cover": cover, "images": images, "item_url": item_url},
    )


def _apply_raw_payload_to_listing(session: Session, *, item_url: str, raw_payload: dict) -> None:
    """Persist raw extractor output to listings.raw_payload (JSONB)."""
    session.execute(
        text(
            """
            UPDATE listings
            SET raw_payload = CAST(:raw_payload AS jsonb)
            WHERE item_url = :item_url
            """
        ),
        {"raw_payload": json.dumps(raw_payload, ensure_ascii=False), "item_url": item_url},
    )
def _jsonable(obj: Any) -> Any:
    """Best-effort conversion to JSON-serializable structures."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    # pydantic models
    if hasattr(obj, "dict"):
        try:
            return _jsonable(obj.dict())
        except Exception:
            pass
    return str(obj)


def _hash_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()


def _index_fingerprint(item: Any) -> Optional[str]:
    """Hash stable list/sitemap evidence used to decide on detail refreshes.

    Sitemap entries often have no title, but do publish ``lastmod`` in ``texts``.
    Hashing only the title therefore missed real source updates. Keep the existing
    ``title_hash`` column for compatibility while storing this broader fingerprint.
    """
    evidence = {
        "title": _sanitize(getattr(item, "title", None)),
        "image": _sanitize(getattr(item, "image", None)),
        "texts": _sanitize(list(getattr(item, "texts", None) or [])),
    }
    if not any(_detail_value_has_content(value) for value in evidence.values()):
        return None
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _hash_text(encoded)


def _url_domain(url: str) -> Optional[str]:
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


def _normalize_domain(domain: Optional[str]) -> Optional[str]:
    if not domain:
        return None
    d = str(domain).strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d or None


def _domain_variants(domain: str) -> List[str]:
    d = _normalize_domain(domain) or domain.lower()
    return [d, f"www.{d}"]


def _chunks(seq: List[str], size: int) -> Iterable[List[str]]:
    if size <= 0:
        yield seq
        return
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _unverifiable_urls_from_meta(
    meta: Any,
    *,
    source_url: str,
    limit: int = 5000,
) -> List[str]:
    """Return same-domain transient failures supplied by the trusted index runner."""
    if not isinstance(meta, dict):
        return []
    validation = meta.get("listingValidation")
    if not isinstance(validation, dict):
        return []
    values = validation.get("unverifiableUrls")
    if not isinstance(values, list):
        return []

    source_domain = _url_domain(source_url)
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        if len(out) >= max(0, int(limit)):
            break
        if not isinstance(value, str):
            continue
        url = _sanitize(value.strip())
        if (
            not url
            or not url.startswith(("http://", "https://"))
            or _url_domain(url) != source_domain
            or url in seen
        ):
            continue
        seen.add(url)
        out.append(url)
    return out


def _lock_listing_urls(session: Session, urls: List[str]) -> None:
    """Serialize overlapping PostgreSQL upserts without locking unrelated URLs.

    The previous read-then-insert path allowed concurrent scraper batches to both
    observe a missing URL and then race on listings.item_url. Locks are acquired
    in hash order so overlapping batches cannot deadlock each other.
    """
    bind = session.get_bind()
    if not urls or bind is None or bind.dialect.name != "postgresql":
        return
    session.execute(
        text(
            """
            SELECT pg_advisory_xact_lock(lock_key)
            FROM (
                SELECT hashtextextended(locked_url, 0) AS lock_key
                FROM unnest(CAST(:urls AS text[])) AS locked_urls(locked_url)
                ORDER BY lock_key
            ) AS ordered_locks
            """
        ),
        {"urls": sorted(set(urls))},
    ).all()


class ExtractionService:
    """Persistence + queue + de-duplication rules.

    **Index runs** (regular scraper):
    - Update/insert into `listings` (canonical state by item_url).
    - Only create new ExtractionRun/ExtractionItem rows when something is **new** or **changed**.
      This prevents daily list runs from flooding the DB with duplicates.

    **Detail runs** (detail scraper):
    - Do NOT create new ExtractionRun/ExtractionItem rows.
    - Store raw detail evidence on the canonical Listing without rewriting an
      historical ExtractionItem row.

    Change detection used (fast):
    - ``title_hash`` stores a compatible index fingerprint of title, cover image,
      and text evidence (including sitemap ``lastmod`` values).
    - if it changes, ``Listing.detail_done`` is reset so the detail is refreshed.
    """

    def __init__(self, session: Session):
        self.session = session

    # ---------- API ----------

    def create(self, payload: ExtractionIn, *, commit: bool = True) -> ExtractionOut:
        meta_in: Dict = dict(payload.meta or {})
        mode = str(meta_in.get("mode") or "").strip().lower()
        if mode == "detail":
            return self._apply_detail_state(payload, commit=commit)
        return self._create_index_run(payload, commit=commit)

    def create_batch(self, payloads: List[ExtractionIn]) -> "ExtractionBatchOut":
        """Create many payloads in one DB transaction.

        Used by /api/v1/extractions/batch to reduce HTTP+DB overhead.
        """
        from .schemas import ExtractionBatchOut, ExtractionBatchResult

        results: List[ExtractionBatchResult] = []
        for p in payloads or []:
            out = self.create(p, commit=False)
            results.append(ExtractionBatchResult(id=out.id, sourceUrl=out.sourceUrl))

        self.session.commit()
        return ExtractionBatchOut(results=results)

    def list(self, limit: int = 50) -> ExtractionListOut:
        stmt = select(ExtractionRun).order_by(desc(ExtractionRun.id)).limit(limit)
        runs = self.session.execute(stmt).scalars().all()
        items = [
            ExtractionListItem(
                id=r.id,
                sourceUrl=r.source_url,
                pageTitle=r.page_title,
                extractedAt=r.extracted_at,
                receivedAt=r.received_at,
                itemCount=r.item_count,
            )
            for r in runs
        ]
        return ExtractionListOut(items=items)

    def get(self, extraction_id: int) -> ExtractionOut | None:
        run = self.session.get(ExtractionRun, extraction_id)
        return self._to_out(run) if run else None

    def export_items_csv(self, extraction_id: int) -> bytes | None:
        run = self.session.get(ExtractionRun, extraction_id)
        if not run:
            return None

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["title", "url", "image", "images", "texts", "rawText"])

        for it in run.items:
            writer.writerow(
                [
                    it.title or "",
                    it.item_url or "",
                    it.image or "",
                    " | ".join(it.images or []),
                    " | ".join(it.texts or []),
                    it.raw_text or "",
                ]
            )

        return buf.getvalue().encode("utf-8")

    # ---------- inventory lifecycle ----------

    def start_inventory_cycle(self, payload: InventoryCycleStartIn) -> InventoryCycleStartOut:
        domain = _normalize_domain(payload.domain)
        if not domain:
            raise ValueError("Inventory cycle requires a valid domain")

        cycle = InventoryCycle(
            domain=domain,
            started_at=datetime.now(timezone.utc),
            status="running",
            targets_expected=int(payload.targetsExpected or 0),
            targets_succeeded=0,
            targets_failed=0,
            listings_seen=0,
            listings_missing=0,
            listings_deactivated=0,
            meta_json=_sanitize(payload.meta or {}),
        )
        self.session.add(cycle)
        self.session.commit()
        self.session.refresh(cycle)

        return InventoryCycleStartOut(
            id=cycle.id,
            domain=cycle.domain,
            status=cycle.status,
            startedAt=cycle.started_at,
            targetsExpected=cycle.targets_expected,
        )

    def complete_inventory_cycle(
        self,
        cycle_id: int,
        payload: InventoryCycleCompleteIn,
    ) -> InventoryCycleCompleteOut:
        cycle = self.session.get(InventoryCycle, cycle_id)
        if cycle is None:
            raise ValueError(f"Inventory cycle {cycle_id} not found")
        if cycle.status != "running":
            raise ValueError(f"Inventory cycle {cycle_id} is already {cycle.status}")

        now = datetime.now(timezone.utc)
        cycle.completed_at = now
        cycle.targets_succeeded = int(payload.targetsSucceeded or 0)
        cycle.targets_failed = int(payload.targetsFailed or 0)

        merged_meta = dict(cycle.meta_json or {})
        merged_meta.update(_sanitize(payload.meta or {}))
        cycle.meta_json = merged_meta

        variants = _domain_variants(cycle.domain)
        seen_count = int(
            self.session.execute(
                select(func.count(Listing.id)).where(
                    func.lower(Listing.domain).in_(variants),
                    Listing.last_seen_cycle_id == cycle.id,
                )
            ).scalar_one()
            or 0
        )
        cycle.listings_seen = seen_count

        expected_ok = (
            cycle.targets_expected <= 0
            or cycle.targets_succeeded == cycle.targets_expected
        )
        run_ok = bool(payload.success) and cycle.targets_failed == 0 and expected_ok

        if not run_ok:
            cycle.status = "failed"
            self.session.commit()
            return InventoryCycleCompleteOut(
                id=cycle.id,
                domain=cycle.domain,
                status=cycle.status,
                completedAt=cycle.completed_at,
                targetsExpected=cycle.targets_expected,
                targetsSucceeded=cycle.targets_succeeded,
                targetsFailed=cycle.targets_failed,
                listingsSeen=cycle.listings_seen,
                listingsMissing=0,
                listingsDeactivated=0,
                reconciliationApplied=False,
                message="Cycle failed or was incomplete; no listings were marked missing.",
            )

        active_before = int(
            self.session.execute(
                select(func.count(Listing.id)).where(
                    func.lower(Listing.domain).in_(variants),
                    Listing.active.is_(True),
                )
            ).scalar_one()
            or 0
        )

        # Completeness guard: if a previously populated domain suddenly returns less
        # than half of its active inventory, preserve the DB and reject reconciliation.
        # The newly seen listings remain stored; only missing/deactivation is skipped.
        if active_before >= 100 and seen_count < int(active_before * 0.50):
            cycle.status = "rejected"
            merged_meta = dict(cycle.meta_json or {})
            merged_meta["rejectionReason"] = "seen_count_below_50_percent_of_active_inventory"
            merged_meta["activeBefore"] = active_before
            cycle.meta_json = merged_meta
            self.session.commit()
            return InventoryCycleCompleteOut(
                id=cycle.id,
                domain=cycle.domain,
                status=cycle.status,
                completedAt=cycle.completed_at,
                targetsExpected=cycle.targets_expected,
                targetsSucceeded=cycle.targets_succeeded,
                targetsFailed=cycle.targets_failed,
                listingsSeen=cycle.listings_seen,
                listingsMissing=0,
                listingsDeactivated=0,
                reconciliationApplied=False,
                message=(
                    f"Reconciliation rejected for safety: saw {seen_count} listings versus "
                    f"{active_before} currently active listings."
                ),
            )

        # Anything active for this domain that was not seen in this complete cycle
        # gets one missing strike. Seen rows were already reset to zero by the heartbeat.
        missing_filter = (
            func.lower(Listing.domain).in_(variants),
            Listing.active.is_(True),
            func.coalesce(Listing.last_seen_cycle_id, 0) != cycle.id,
        )

        missing_count = int(
            self.session.execute(
                select(func.count(Listing.id)).where(*missing_filter)
            ).scalar_one()
            or 0
        )

        if missing_count:
            self.session.execute(
                update(Listing)
                .where(*missing_filter)
                .values(missing_cycles=Listing.missing_cycles + 1)
            )

        deactivate_filter = (
            func.lower(Listing.domain).in_(variants),
            Listing.active.is_(True),
            Listing.missing_cycles >= 2,
        )
        deactivated_count = int(
            self.session.execute(
                select(func.count(Listing.id)).where(*deactivate_filter)
            ).scalar_one()
            or 0
        )
        if deactivated_count:
            self.session.execute(
                update(Listing)
                .where(*deactivate_filter)
                .values(active=False, inactive_at=now)
            )

        cycle.listings_missing = missing_count
        cycle.listings_deactivated = deactivated_count
        cycle.status = "completed"
        self.session.commit()

        return InventoryCycleCompleteOut(
            id=cycle.id,
            domain=cycle.domain,
            status=cycle.status,
            completedAt=cycle.completed_at,
            targetsExpected=cycle.targets_expected,
            targetsSucceeded=cycle.targets_succeeded,
            targetsFailed=cycle.targets_failed,
            listingsSeen=cycle.listings_seen,
            listingsMissing=cycle.listings_missing,
            listingsDeactivated=cycle.listings_deactivated,
            reconciliationApplied=True,
            message="Cycle reconciled successfully.",
        )

    def _cycle_id_for_payload(self, payload: ExtractionIn) -> Optional[int]:
        meta = dict(payload.meta or {})
        raw = meta.get("inventoryCycleId")
        if raw in (None, ""):
            return None
        try:
            cycle_id = int(raw)
        except Exception as exc:
            raise ValueError(f"Invalid inventoryCycleId: {raw!r}") from exc

        cycle = self.session.get(InventoryCycle, cycle_id)
        if cycle is None or cycle.status != "running":
            raise ValueError(f"Inventory cycle {cycle_id} is not active")

        source_domain = _url_domain(payload.sourceUrl)
        if source_domain and source_domain != cycle.domain:
            raise ValueError(
                f"Inventory cycle {cycle_id} belongs to {cycle.domain}, "
                f"but payload source is {source_domain}"
            )
        return cycle_id

    # ---------- detail queue ----------

    def existing_active_urls(self, urls: Iterable[str]) -> List[str]:
        """Return only already-active URLs from a bounded recovery batch."""
        clean = list(
            dict.fromkeys(
                value.strip()
                for value in urls
                if isinstance(value, str)
                and value.strip().startswith(("http://", "https://"))
            )
        )[:5000]
        if not clean:
            return []
        rows = self.session.execute(
            select(Listing.item_url).where(
                Listing.active.is_(True),
                Listing.item_url.in_(clean),
            )
        ).scalars()
        return list(rows)

    def detail_queue(
        self,
        domain: Optional[str] = None,
        url_contains: Optional[str] = None,
        limit: int = 200,
    ) -> List[str]:
        """Return listing URLs that need a detail scrape.

        Primary source: `listings` table where detail_done = false.

        Backfill behavior:
        If `listings` is empty or doesn't have enough rows yet, we top-up by scanning
        recent `extraction_items.item_url` and inserting stub Listing rows (detail_done=false).
        """
        limit = max(1, min(int(limit), 500))

        stmt = select(Listing.item_url).where(Listing.detail_done.is_(False), Listing.active.is_(True))
        if domain:
            stmt = stmt.where(func.lower(Listing.domain).like(f"%{domain.lower()}%"))
        if url_contains:
            stmt = stmt.where(func.lower(Listing.item_url).like(f"%{url_contains.lower()}%"))
        stmt = stmt.order_by(desc(Listing.updated_at)).limit(limit)

        rows = self.session.execute(stmt).all()
        out: List[str] = [r[0] for r in rows if isinstance(r[0], str) and r[0].startswith("http")]

        remaining = limit - len(out)
        if remaining <= 0:
            return out

        # Top-up from historical items that do not yet exist in listings
        RunIndex = aliased(ExtractionRun)
        mode_index = self._mode_expr(RunIndex.meta_json)

        cand = (
            select(ExtractionItem.item_url)
            .select_from(ExtractionItem)
            .join(RunIndex, RunIndex.id == ExtractionItem.run_id)
            .where(ExtractionItem.item_url.is_not(None))
            .where(ExtractionItem.item_url != "")
            .where(mode_index != "detail")
        )

        if domain:
            cand = cand.where(func.lower(ExtractionItem.item_url).like(f"%{domain.lower()}%"))
        if url_contains:
            cand = cand.where(func.lower(ExtractionItem.item_url).like(f"%{url_contains.lower()}%"))
        if out:
            cand = cand.where(~ExtractionItem.item_url.in_(out))

        cand = cand.where(~exists(select(1).select_from(Listing).where(Listing.item_url == ExtractionItem.item_url)))
        # PostgreSQL requires ORDER BY expressions to be part of a DISTINCT
        # projection. Grouping by URL and ordering by its newest source run gives
        # the intended stable dedupe without that invalid DISTINCT query.
        cand = (
            cand.group_by(ExtractionItem.item_url)
            .order_by(desc(func.max(ExtractionItem.run_id)))
            .limit(remaining)
        )

        extra_rows = self.session.execute(cand).all()
        extra_urls = [r[0] for r in extra_rows if isinstance(r[0], str) and r[0].startswith("http")]

        if extra_urls:
            now = datetime.now(timezone.utc)
            for u in extra_urls:
                self.session.add(
                    Listing(
                        item_url=_sanitize(u),
                        domain=_url_domain(u),
                        title=None,
                        title_hash=None,
                        image=None,
                        created_at=now,
                        updated_at=now,
                        active=True,
                        last_seen_at=None,
                        last_seen_cycle_id=None,
                        missing_cycles=0,
                        inactive_at=None,
                        detail_done=False,
                        detail_scraped_at=None,
                        description=None,
                        description_hash=None,
                    )
                )
            self.session.commit()

        out.extend(extra_urls)
        return out

    # ---------- internals ----------

    def _create_index_run(self, payload: ExtractionIn, *, commit: bool) -> ExtractionOut:
        """Create a normal extraction run.

        Historical ExtractionRun/ExtractionItem rows are still created only for:
        - new URLs,
        - changed title hashes, or
        - reactivated URLs.

        Every seen URL, including unchanged URLs, receives a lightweight presence
        heartbeat on `listings`. This is what allows complete inventory cycles to
        safely determine which listings disappeared.
        """
        extracted_at = payload.extractedAt.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        cycle_id = self._cycle_id_for_payload(payload)

        # A timeout, rate-limit, or anti-bot page is not evidence that a known
        # listing disappeared. Heartbeat only rows that already exist and are
        # still active; never create a listing from unverifiable evidence and
        # never reactivate an inactive row this way.
        unverifiable_urls = _unverifiable_urls_from_meta(
            payload.meta or {}, source_url=payload.sourceUrl
        )
        if unverifiable_urls:
            for chunk in _chunks(unverifiable_urls, 800):
                values: Dict[str, Any] = {
                    "last_seen_at": extracted_at,
                    "missing_cycles": 0,
                }
                if cycle_id is not None:
                    values["last_seen_cycle_id"] = cycle_id
                self.session.execute(
                    update(Listing)
                    .where(
                        Listing.item_url.in_(chunk),
                        Listing.active.is_(True),
                    )
                    .values(**values)
                )

        # Deduplicate incoming items by URL (keep first occurrence)
        raw_items = list(payload.items or [])
        dedup: Dict[str, Any] = {}
        for it in raw_items:
            u = _sanitize(getattr(it, "url", None))
            if not u or not isinstance(u, str):
                continue
            if u not in dedup:
                dedup[u] = it

        urls = list(dedup.keys())
        if not urls:
            last = self._latest_run_for_source_url(_sanitize(payload.sourceUrl))
            if last is None:
                run = ExtractionRun(
                    source_url=_sanitize(payload.sourceUrl),
                    page_title=_sanitize(payload.pageTitle),
                    extracted_at=extracted_at,
                    item_count=0,
                    meta_json=_sanitize(payload.meta or {}),
                )
                self.session.add(run)
                self.session.flush()
                if commit:
                    self.session.commit()
                return self._to_out(run)
            return self._to_out(last)

        _lock_listing_urls(self.session, urls + unverifiable_urls)

        # Load existing listings for these URLs
        existing: Dict[str, Listing] = {}
        for chunk in _chunks(urls, 800):
            rows = self.session.execute(select(Listing).where(Listing.item_url.in_(chunk))).scalars().all()
            for r in rows:
                existing[r.item_url] = r

        new_urls: Set[str] = set()
        changed_urls: Set[str] = set()
        reactivated_urls: Set[str] = set()

        for u, it in dedup.items():
            title = _sanitize(getattr(it, "title", None))
            th = _index_fingerprint(it)
            row = existing.get(u)
            if row is None:
                new_urls.add(u)
                continue

            was_inactive = not bool(row.active)

            # Presence heartbeat: intentionally does NOT touch updated_at.
            row.domain = _url_domain(u) or row.domain
            row.last_seen_at = extracted_at
            row.active = True
            row.missing_cycles = 0
            row.inactive_at = None
            if cycle_id is not None:
                row.last_seen_cycle_id = cycle_id

            if was_inactive:
                reactivated_urls.add(u)
            if (row.title_hash or None) != (th or None):
                changed_urls.add(u)

        to_store = new_urls | changed_urls | reactivated_urls

        # If nothing changed, persist only the lightweight heartbeat and return the
        # latest historical run. No duplicate ExtractionItem rows are created.
        if not to_store:
            if commit:
                self.session.commit()
            else:
                self.session.flush()

            last = self._latest_run_for_source_url(_sanitize(payload.sourceUrl))
            if last is not None:
                return self._to_out(last)

            run = ExtractionRun(
                source_url=_sanitize(payload.sourceUrl),
                page_title=_sanitize(payload.pageTitle),
                extracted_at=extracted_at,
                item_count=0,
                meta_json=_sanitize(payload.meta or {}),
            )
            self.session.add(run)
            self.session.flush()
            if commit:
                self.session.commit()
            return self._to_out(run)

        # Create run only when we have something new/changed/reactivated.
        run = ExtractionRun(
            source_url=_sanitize(payload.sourceUrl),
            page_title=_sanitize(payload.pageTitle),
            extracted_at=extracted_at,
            item_count=len(to_store),
            meta_json=_sanitize(payload.meta or {}),
        )
        self.session.add(run)
        self.session.flush()

        # Persist only the new/changed/reactivated items
        stored_items: List[ExtractionItem] = []
        for u in urls:
            if u not in to_store:
                continue

            it = dedup[u]
            row = existing.get(u)

            raw_text = _sanitize(getattr(it, "rawText", None))
            if row is not None and row.description:
                try:
                    if not raw_text or len(row.description) > len(raw_text):
                        raw_text = row.description
                except Exception:
                    raw_text = raw_text or row.description

            stored_items.append(
                ExtractionItem(
                    run_id=run.id,
                    title=_sanitize(getattr(it, "title", None)),
                    item_url=_sanitize(getattr(it, "url", None)),
                    image=_sanitize(getattr(it, "image", None)),
                    images=_sanitize(list(getattr(it, "images", None) or [])),
                    texts=_sanitize(list(getattr(it, "texts", None) or [])),
                    raw_text=raw_text,
                )
            )

        self.session.add_all(stored_items)

        # Update canonical Listing state
        for u in to_store:
            it = dedup[u]
            title = _sanitize(getattr(it, "title", None))
            th = _index_fingerprint(it)
            img = _sanitize(getattr(it, "image", None))

            row = existing.get(u)
            if row is None:
                row = Listing(
                    item_url=u,
                    domain=_url_domain(u),
                    title=title,
                    title_hash=th,
                    image=img,
                    created_at=now,
                    updated_at=now,
                    active=True,
                    last_seen_at=extracted_at,
                    last_seen_cycle_id=cycle_id,
                    missing_cycles=0,
                    inactive_at=None,
                    detail_done=False,
                    detail_scraped_at=None,
                    description=None,
                    description_hash=None,
                )
                self.session.add(row)
                existing[u] = row
            else:
                row.domain = _url_domain(u) or row.domain
                row.title = title
                row.title_hash = th
                if img:
                    row.image = img
                row.updated_at = now
                row.last_seen_at = extracted_at
                row.active = True
                row.missing_cycles = 0
                row.inactive_at = None
                if cycle_id is not None:
                    row.last_seen_cycle_id = cycle_id

                # Re-scrape details when the index snapshot changed or the listing
                # reappeared after being inactive.
                row.detail_done = False
                row.detail_scraped_at = None

        if commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_out(run)

    def _apply_detail_state(self, payload: ExtractionIn, *, commit: bool) -> ExtractionOut:
        """Apply raw detail evidence to latest state without rewriting history.

        Detail runs do not create duplicate index snapshots. They also no longer
        mutate the latest historical ExtractionItem: index history remains exactly
        as it arrived, while the full untransformed detail payload lives on the
        canonical Listing row for downstream normalization.
        """
        source_url = _sanitize(payload.sourceUrl)
        extracted_at = payload.extractedAt.astimezone(timezone.utc)

        # The detail runner posts 1 item where rawText is the full description.
        items_in = list(payload.items or [])
        first = items_in[0] if items_in else None

        # Never mark an empty/block/error page as detailed. Missing images are fine,
        # and no plausibility checks belong here; normalization handles quality later.
        if not _detail_payload_has_meaningful_content(first):
            raise ValueError(
                f"Detail payload for {source_url} contains no meaningful text or structured evidence"
            )

        full_desc = _sanitize(getattr(first, "rawText", None)) if first is not None else None
        full_desc = (full_desc or "").strip() or None
        desc_hash = _hash_text(full_desc) if full_desc else None
        cover = _sanitize(getattr(first, "image", None)) if first is not None else None
        images = _sanitize(list(getattr(first, "images", None) or [])) if first is not None else []
        images = _normalize_url_list(images)
        has_new_images = bool(images)

        now = datetime.now(timezone.utc)

        # 1) Update canonical listing state
        listing = self.session.execute(select(Listing).where(Listing.item_url == source_url)).scalars().first()
        if listing is None:
            listing = Listing(
                item_url=source_url,
                domain=_url_domain(source_url),
                title=_sanitize(payload.pageTitle) or _sanitize(getattr(first, "title", None)),
                title_hash=_hash_text(_sanitize(payload.pageTitle) or _sanitize(getattr(first, "title", None))),
                image=cover,
                created_at=now,
                updated_at=now,
                active=True,
                last_seen_at=now,
                last_seen_cycle_id=None,
                missing_cycles=0,
                inactive_at=None,
                detail_done=True,
                detail_scraped_at=extracted_at,
                description=full_desc,
                description_hash=desc_hash,
                raw_payload=_jsonable(payload.dict()),
            )
            self.session.add(listing)
        else:
            # Always refresh raw detail evidence when a listing is intentionally re-scraped.
            # A changed index listing may have the same description but different structured data.
            listing.updated_at = now
            listing.detail_done = True
            listing.detail_scraped_at = extracted_at
            listing.description = full_desc
            listing.description_hash = desc_hash
            # store full raw v2 payload for later normalization
            try:
                listing.raw_payload = _jsonable(payload.dict())
            except Exception:
                listing.raw_payload = _jsonable(payload)

        # Persist cover + images to listings (images column may not be mapped on the ORM model)
        if cover and listing is not None:
            listing.image = cover
        if has_new_images or cover:
            self.session.flush()
            _apply_images_to_listing(self.session, item_url=source_url, cover=cover, images=images)
        # Persist raw payload even if ORM is out-of-sync with schema
        _apply_raw_payload_to_listing(self.session, item_url=source_url, raw_payload=_jsonable(payload.dict()))

        # 2) Resolve the index run only for the API response. Never rewrite an
        # ExtractionItem here: those rows are immutable historical snapshots.
        _idx_item, idx_run = self._latest_index_item_and_run(source_url)

        if commit:
            self.session.commit()
        else:
            self.session.flush()

        if idx_run is None:
            idx_run = self._latest_index_run_for_url(source_url)

        if idx_run is None:
            raise RuntimeError(
                "Detail state update succeeded in listings, but no existing index run was found for this URL. "
                "Run the regular scraper first so the listing exists in extraction_runs/items."
            )

        return self._to_out(idx_run)

    def _mode_expr(self, meta_col):
        """Extract meta.mode as text (Postgres + best-effort SQLite)."""
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            return func.coalesce(meta_col.op("->>")("mode"), "")
        return func.coalesce(func.json_extract(meta_col, "$.mode"), "")

    def _latest_index_item_and_run(self, item_url: str) -> Tuple[Optional[ExtractionItem], Optional[ExtractionRun]]:
        """Return the latest NON-detail ExtractionItem + its run for this item_url."""
        RunIndex = aliased(ExtractionRun)
        mode_index = self._mode_expr(RunIndex.meta_json)

        stmt = (
            select(ExtractionItem, RunIndex)
            .select_from(ExtractionItem)
            .join(RunIndex, RunIndex.id == ExtractionItem.run_id)
            .where(ExtractionItem.item_url == item_url)
            .where(mode_index != "detail")
            .order_by(desc(RunIndex.id))
            .limit(1)
        )
        row = self.session.execute(stmt).first()
        if not row:
            return None, None
        return row[0], row[1]

    def _latest_index_run_for_url(self, item_url: str) -> Optional[ExtractionRun]:
        _item, run = self._latest_index_item_and_run(item_url)
        return run

    def _latest_run_for_source_url(self, source_url: Optional[str]) -> Optional[ExtractionRun]:
        if not source_url:
            return None
        stmt = select(ExtractionRun).where(ExtractionRun.source_url == source_url).order_by(desc(ExtractionRun.id)).limit(1)
        return self.session.execute(stmt).scalars().first()
    def _apply_detail_fields_to_listing(db, source_url: str, item: dict) -> None:
        """
        Apply selected extracted fields to listings row matched by item_url == source_url.

        Works even if ORM Listing model doesn't yet include the new columns,
        because we update via SQL text().
        """
        cover = item.get("image")
        images = item.get("images") or []
        if not isinstance(images, list):
            images = []

        # ensure all are strings and non-empty
        images = [str(u) for u in images if u]

        db.execute(
            text(
                """
                UPDATE listings
                SET
                image = COALESCE(:image, image),
                images = CAST(:images AS text[]),
                detail_done = TRUE
                WHERE item_url = :item_url
                """
            ),
            {
                "image": cover,
                "images": images,
                "item_url": source_url,
            },
        )
    def _to_out(self, run: ExtractionRun) -> ExtractionOut:
        items = [
            ExtractedItem(
                title=i.title,
                url=i.item_url,
                image=i.image,
                images=i.images or [],
                texts=i.texts or [],
                rawText=i.raw_text,
            )
            for i in (run.items or [])
        ]

        return ExtractionOut(
            id=run.id,
            dataVersion=1,
            sourceUrl=run.source_url,
            pageTitle=run.page_title,
            extractedAt=run.extracted_at,
            receivedAt=run.received_at,
            itemCount=run.item_count,
            meta=run.meta_json or {},
            items=items,
        )

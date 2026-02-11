
#!/usr/bin/env python3
"""
homes_api_pager.py  (partition-aware)

Goal: fetch ALL listing URLs from homes.bg by calling their JSON API in a way that
actually respects filters present in the list_url (e.g., neighbourhoods[]).

Key fix vs previous iterations:
- We DO NOT rely on the API inferring criteria from cookies/session alone.
- We copy the query-string filters from the list_url and append them to /api/offers
  together with startIndex/stopIndex. This makes partitions effective.

Usage (inside container):
  python scraper/homes_api_pager.py --list-url "<homes.bg url>" --out data/homes_urls.txt
  python scraper/homes_api_pager.py --partitions-file data/homes_partitions.txt --out data/homes_urls.txt

Notes:
- homes.bg API appears to hard-cap at ~1000 results per criteria set.
- Partition your search into multiple criteria sets (e.g., neighbourhood groups) to exceed this cap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Optional DB support (psycopg3). If not available, run with --no-db.
try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover
    psycopg = None  # type: ignore


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def _clean_line(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # allow comments
    if s.startswith("#"):
        return ""
    # ignore common shell/heredoc artifacts and any non-URL lines
    if s.upper() == "EOF":
        return ""
    if not (s.startswith("http://") or s.startswith("https://")):
        return ""
    return s


def read_urls_file(path: Path) -> List[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = _clean_line(raw)
        if s:
            lines.append(s)
    return lines


def normalize_offer_url(view_href_or_url: str) -> str:
    if not view_href_or_url:
        return ""
    if view_href_or_url.startswith("http"):
        return view_href_or_url
    if view_href_or_url.startswith("/"):
        return "https://www.homes.bg" + view_href_or_url
    return "https://www.homes.bg/" + view_href_or_url.lstrip("/")


def db_dsn_from_env(explicit: Optional[str] = None) -> str:
    """Build a psycopg-compatible DSN from env.

    Prefers explicit arg, then DATABASE_URL, then POSTGRES_* vars.
    """

    if explicit:
        return explicit

    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url:
        # SQLAlchemy style: postgresql+psycopg://...
        if db_url.startswith("postgresql+psycopg://"):
            db_url = "postgresql://" + db_url[len("postgresql+psycopg://") :]
        if db_url.startswith("postgresql+psycopg2://"):
            db_url = "postgresql://" + db_url[len("postgresql+psycopg2://") :]
        return db_url

    user = os.getenv("POSTGRES_USER", "").strip()
    pw = os.getenv("POSTGRES_PASSWORD", "").strip()
    db = os.getenv("POSTGRES_DB", "").strip()
    host = os.getenv("POSTGRES_HOST", "db").strip() or "db"
    port = os.getenv("POSTGRES_PORT", "5432").strip() or "5432"
    if not (user and pw and db):
        raise RuntimeError("DB env missing: set DATABASE_URL or POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"



class DbWriter:
    """
    Small helper to write URL rows into an existing Postgres table.

    Assumptions:
      - table has at least an `item_url` TEXT/VARCHAR column
      - table may also have `title` and/or `source` columns (optional)
    """

    def __init__(self, dsn: str, table: str, run_id: int | None = None, source: str = "homes.bg") -> None:
        self.dsn = dsn
        self.table = table
        self.run_id: int | None = run_id
        self.source = source
        self.conn = None
        # Cached set of column names in the target table.
        self._cols: Optional[Set[str]] = None
        # Backwards-compatible attribute used by earlier revisions.
        # Some call-sites expect `self.columns`.
        self.columns: Optional[Set[str]] = None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
                self._cols = None
                self.columns = None

    def connect(self) -> None:
        assert psycopg is not None
        if self.conn is None:
            self.conn = psycopg.connect(self.dsn)
            # simple + safe for batch inserts
            self.conn.autocommit = True
            self._cols = None

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
        finally:
            self.conn = None

    def __enter__(self) -> "DbWriter":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _introspect_columns(self) -> Set[str]:
        assert self.conn is not None
        if self._cols is not None:
            return self._cols

        cols: Set[str] = set()
        with self.conn.cursor() as cur:
            # works across schemas; prefer current_schema() unless table is schema-qualified
            if "." in self.table:
                schema, table = self.table.split(".", 1)
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    """,
                    (schema, table),
                )
            else:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = %s
                    """,
                    (self.table,),
                )
            for (c,) in cur.fetchall():
                cols.add(str(c))
        self._cols = cols

        # Keep a public alias so earlier code paths can check `self.columns`.
        self.columns = cols
        return cols

    def insert_urls(self, urls: List[str]) -> int:
        """
        Inserts URLs into the configured table, skipping existing rows
        WITHOUT relying on a UNIQUE constraint (uses WHERE NOT EXISTS).

        Returns number of attempted URLs (not exact inserted count, since Postgres
        doesn't report it reliably with WHERE NOT EXISTS + unnest in autocommit).
        """
        if not urls:
            return 0
        assert psycopg is not None
        assert self.conn is not None

        # dedupe while preserving order
        seen: Set[str] = set()
        uniq: List[str] = []
        for u in urls:
            u = u.strip()
            if not u or u in seen:
                continue
            seen.add(u)
            uniq.append(u)

        if not uniq:
            return 0

        cols = self._introspect_columns()
        if "run_id" in self.columns and self.run_id is None:
            with self.conn.cursor() as cur:
                cur.execute(f"SELECT COALESCE(MAX(run_id), 0) FROM {self.table}")
                self.run_id = int(cur.fetchone()[0]) + 1
            log(f"[db] using run_id={self.run_id}")
        has_title = "title" in cols
        has_source = "source" in cols or "site" in cols or "domain" in cols

        # choose a source column name, if any
        source_col = None
        for c in ("source", "site", "domain"):
            if c in cols:
                source_col = c
                break

        # build INSERT with psycopg.sql for safe identifiers
        from psycopg import sql

        if "." in self.table:
            schema, table = self.table.split(".", 1)
            table_ident = sql.Identifier(schema, table)
        else:
            table_ident = sql.Identifier(self.table)

        insert_cols = ["item_url"]
        select_exprs = ["u.url"]
        params: List[object] = [uniq]

        # Some deployments require run_id NOT NULL. If present, we insert a constant run_id per pager run.
        if "run_id" in self.columns:
            if self.run_id is None:
                raise RuntimeError("DbWriter.run_id is not set (table requires run_id). Call connect() first or pass --run-id.")
            insert_cols.insert(0, "run_id")
            select_exprs.insert(0, "%s")
            params.insert(0, self.run_id)

        if has_title:
            insert_cols.append("title")
            select_exprs.append("NULL")

        if source_col is not None:
            insert_cols.append(source_col)
            select_exprs.append("%s")
            params.append(self.source)

        query = sql.SQL(
            "INSERT INTO {tbl} ({cols}) "
            "SELECT {sels} "
            "FROM unnest(%s::text[]) AS u(url) "
            "WHERE NOT EXISTS (SELECT 1 FROM {tbl} t WHERE t.item_url = u.url)"
        ).format(
            tbl=table_ident,
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in insert_cols),
            sels=sql.SQL(", ").join(sql.SQL(s) for s in select_exprs),
        )

        with self.conn.cursor() as cur:
            cur.execute(query, params)

        return len(uniq)



def list_url_to_api_url(
    list_url: str,
    offers_api_base: str,
    start_index: int,
    stop_index: int,
) -> str:
    """
    Build an API URL that includes the same filter params as list_url.
    This is the critical fix: neighbourhoods[] MUST be present in the API request.
    """
    parsed = urlparse(list_url)
    q = parse_qs(parsed.query, keep_blank_values=True)

    # Always include pagination params expected by the API.
    q["startIndex"] = [str(start_index)]
    q["stopIndex"] = [str(stop_index)]

    # homes.bg uses typeId in list pages; keep it if present (e.g., ApartmentSell).
    # Keep currencyId, filterOrderBy, locationId, neighbourhoods[] etc.
    # We don't try to translate anything here; just forward the filters.

    # urlencode with doseq=True to keep neighbourhoods[]=... repeated.
    new_query = urlencode(q, doseq=True)

    api_parsed = urlparse(offers_api_base)
    return urlunparse((api_parsed.scheme, api_parsed.netloc, api_parsed.path, "", new_query, ""))


async def _click_consent_if_present(page) -> None:
    # The consent banner tends to be a simple button. Keep this permissive.
    selectors = [
        "button:has-text('Съгласен')",
        "button:has-text('Приемам')",
        "button:has-text('I Agree')",
        "button:has-text('Accept')",
        "text=Съгласен",
        "text=Приемам",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=1500)
                log("[capture] clicked consent button")
                return
        except Exception:
            continue


async def capture_session_cookie_header(list_url: str, headless: bool, timeout_ms: int) -> str:
    """
    Navigate to list_url to obtain any cookies the API might require.
    Return Cookie header string (may be empty).
    """
    log(f"[capture] goto={list_url} headless={headless}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(list_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            log("[capture] WARN: domcontentloaded timeout; continuing")
        await _click_consent_if_present(page)

        # Give the page a moment to set cookies/localstorage
        await page.wait_for_timeout(800)

        cookies = await context.cookies()
        await browser.close()

    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")])
    return cookie_header


@dataclass
class PageStats:
    start: int
    stop: int
    n: int
    new: int
    total_unique: int
    has_more: Optional[bool]
    elapsed_s: float


async def fetch_all_offers_for_list_url(
    list_url: str,
    offers_api_base: str,
    page_size: int,
    max_items: int,
    sleep_s: float,
    headless: bool,
    timeout_ms: int,
    global_seen_ids: Set[str],
    global_seen_urls: Set[str],
    db_writer: Optional["DbWriter"] = None,
) -> Tuple[int, int, Optional[int]]:
    """
    Fetch offer list for a single partition list_url.
    Returns: (added_new_urls, global_unique_urls, offersCount_from_api)
    """
    cookie_header = await capture_session_cookie_header(list_url, headless=headless, timeout_ms=timeout_ms)
    log(f"[capture] cookie_len={len(cookie_header)}")

    headers = {
        "User-Agent": os.environ.get("UA", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": list_url,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    added_total = 0
    offers_count: Optional[int] = None

    empty_streak = 0
    start_index = 0
    effective_page_size = page_size
    t0 = time.time()

    async with async_playwright() as p:
        # Use Playwright's APIRequestContext (shares TLS/headers nicely).
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context(extra_http_headers=headers)
        request = context.request

        while start_index < max_items:
            stop_index = start_index + effective_page_size - 1
            api_url = list_url_to_api_url(list_url, offers_api_base, start_index, stop_index)

            t_req = time.time()
            resp = await request.get(api_url, timeout=timeout_ms)
            elapsed = time.time() - t_req

            ct = (resp.headers.get("content-type") or "").lower()
            if resp.status != 200 or "application/json" not in ct:
                text = await resp.text()
                log(f"[api] ERROR status={resp.status} ct='{ct}' url={api_url} body_snip={text[:200]!r}")
                break

            data = await resp.json()
            if offers_count is None:
                try:
                    offers_count = int(data.get("offersCount")) if data.get("offersCount") is not None else None
                except Exception:
                    offers_count = None
                # Print criteria from API (useful to confirm partitions)
                sc = data.get("searchCriteria")
                if isinstance(sc, dict):
                    log(f"criteria_from_api={sc}")

            result = data.get("result") or []
            # Homes.bg API appears to cap page size (often at 100). If we request a larger window
            # but receive fewer items on the very first non-empty page, adapt the step/window to avoid skipping.
            if result and len(result) < effective_page_size:
                effective_page_size = len(result)
            has_more = data.get("hasMoreItems")
            if not result:
                empty_streak += 1
                log(f"[api] empty page start={start_index} stop={stop_index} empty_streak={empty_streak} hasMore={has_more} offersCount={offers_count}")
                if empty_streak >= 2:
                    break
                start_index += effective_page_size
                if sleep_s:
                    await asyncio.sleep(sleep_s)
                continue
            empty_streak = 0

            new_this = 0
            new_urls: List[str] = []
            for it in result:
                if not isinstance(it, dict):
                    continue
                oid = str(it.get("id") or "")
                view = it.get("viewHref") or it.get("url") or ""
                url = normalize_offer_url(view)
                if not url or not oid:
                    continue
                if oid in global_seen_ids:
                    continue
                global_seen_ids.add(oid)
                if url not in global_seen_urls:
                    global_seen_urls.add(url)
                    new_this += 1
                    new_urls.append(url)

            if db_writer and new_urls:
                db_writer.insert_urls(new_urls)

            added_total += new_this

            # Log every page
            sample_tail = list(list(global_seen_urls)[-3:])
            log(f"[page] start={start_index}..{stop_index} n={len(result)} new={new_this} total_unique={len(global_seen_urls)} hasMore={has_more} elapsed={elapsed:.1f}s sample_tail={sample_tail}")

            start_index += effective_page_size
            if sleep_s:
                await asyncio.sleep(sleep_s)

        await browser.close()

    return added_total, len(global_seen_urls), offers_count


def write_urls(out_path: Path, urls: Iterable[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(sorted(urls)) + "\n", encoding="utf-8")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-url", help="Single homes.bg list URL to scrape.", default=None)
    ap.add_argument("--partitions-file", help="Text file with one list URL per line.", default=None)
    ap.add_argument("--out", help="Output file for offer URLs.", default="data/homes_urls.txt")
    ap.add_argument("--no-file", action="store_true", help="Do not write output file.", default=False)

    # DB output (default ON). Use --no-db to disable.
    ap.add_argument("--db", dest="db", action="store_true", default=True, help="Write discovered offer URLs into Postgres.")
    ap.add_argument("--no-db", dest="db", action="store_false", help="Disable Postgres writes.")
    ap.add_argument("--db-url", default=None, help="Override Postgres DSN/URL. Otherwise uses DATABASE_URL/POSTGRES_* env vars.")
    ap.add_argument("--db-table", default="extraction_items", help="Target table to insert into (default: extraction_items)")
    ap.add_argument("--run-id", type=int, default=None, help="Optional run_id to use when inserting into extraction_items. If omitted, uses MAX(run_id)+1.")
    ap.add_argument("--offers-api-base", help="Offers API base URL.", default="https://www.homes.bg/api/offers")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--max-items", type=int, default=20000)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--headless", action="store_true", default=True)  # kept for compatibility; we always launch headless for API
    ap.add_argument("--timeout-ms", type=int, default=45000)
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    out_path = Path(args.out) if args.out else None
    if (not args.no_file) and out_path is None:
        raise SystemExit("--out is required unless --no-file is set")
    list_urls: List[str] = []

    if args.partitions_file:
        pf = Path(args.partitions_file)
        if not pf.exists():
            raise SystemExit(f"Partitions file not found: {pf}")
        list_urls.extend(read_urls_file(pf))

    if args.list_url:
        list_urls.append(args.list_url)

    if not list_urls:
        raise SystemExit("Provide --list-url or --partitions-file")

    # de-dupe partitions
    seen_partitions = []
    seen_set = set()
    for u in list_urls:
        if u not in seen_set:
            seen_set.add(u)
            seen_partitions.append(u)
    list_urls = seen_partitions

    global_seen_ids: Set[str] = set()
    global_seen_urls: Set[str] = set()

    db_writer: Optional[DbWriter] = None
    if args.db:
        if psycopg is None:
            raise SystemExit("psycopg is not installed in this container. Rebuild image or run with --no-db.")
        db_writer = DbWriter(dsn=db_dsn_from_env(args.db_url), table=args.db_table, run_id=args.run_id)
        db_writer.connect()

    async def _run() -> None:
        try:
            for i, u in enumerate(list_urls, start=1):
                log(f"=== partition {i}/{len(list_urls)} ===")
                log(f"list_url={u}")
                added, total_unique, oc = await fetch_all_offers_for_list_url(
                    list_url=u,
                    offers_api_base=args.offers_api_base,
                    page_size=args.page_size,
                    max_items=args.max_items,
                    sleep_s=args.sleep,
                    headless=args.headless,
                    timeout_ms=args.timeout_ms,
                    global_seen_ids=global_seen_ids,
                    global_seen_urls=global_seen_urls,
                    db_writer=db_writer,
                )
                log(f"partition_done added={added} total_unique={total_unique} offersCount_partition={oc}")
                if not args.no_file:
                    write_urls(out_path, global_seen_urls)

            if not args.no_file:
                log(f"WROTE {len(global_seen_urls)} lines -> {out_path}")
            log(f"DONE unique_ids={len(global_seen_ids)}")
        finally:
            if db_writer is not None:
                db_writer.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()

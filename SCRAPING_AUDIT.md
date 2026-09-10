# Scraping reliability and coverage audit

> Historical baseline only. The current 34-domain/81-target configuration,
> production ingestion counts, new Address and OLX adapters, and live blockers
> are documented in `INGESTION_REPORT_2026-09-10.md`.

Audit date: 2026-09-09

## Result

The scheduler now covers 29 active domains with 55 logical targets: 17 complete
sitemap inventories and 38 browser targets. Authoritative feeds supersede the
old Sofia partitions at scheduling time, while those older definitions remain
in YAML as documented fallbacks. Sitemap `lastmod`, title, image, and text
evidence participate in change detection, so updated listings re-enter the
detail queue even when the sitemap has no title.

Index runs use bounded per-domain concurrency, enforce source-specific minimum
counts, and serialize overlapping PostgreSQL upserts with advisory locks. Raw
index history is stored only for new, changed, or reactivated URLs; unchanged
URLs receive a lightweight presence heartbeat. A listing becomes inactive only
after two complete successful domain cycles omit it. Failed or truncated cycles
never deactivate data.

## Production ingestion evidence

The final six-worker sitemap dry run completed 17/17 domains in 124.15 seconds
and found 1,155,372 unique current URLs. The production API/PostgreSQL run also
completed 17/17 in 424.05 seconds. The separate BCPEA browser inventory reached
its natural end at page 43 and stored 1,547 auctions in 149.45 seconds.

| Source | Current complete inventory |
| --- | ---: |
| ERA (sale + rent) | 5,327 |
| Home2U projects/apartments | 10,124 |
| DSK Home | 107,526 |
| Imoti.info (sale + rent) | 216,787 |
| Revolution Estate | 4,265 |
| Homes.bg | 82,382 |
| Imoti.net | 67,833 |
| Imoti.com | 222,896 |
| Imot.bg | 205,213 during production run |
| Holmes.bg | 212,955 |
| Unique Estates | 1,883 Bulgarian URLs |
| Yavlena | 5,341 |
| Suprimmo | 9,536 |
| Luximmo Bulgaria | 3,807 |
| BuildingBox | 1,341 |
| NoviteSgradi | 768 |
| IMOTNO | 7 |
| BCPEA public sales | 1,547 |

After ingestion PostgreSQL contained 1,228,903 canonical listings across all
sources, 1,228,903 active, and zero duplicate `item_url` values. The database
occupied 1.7 GB. Current feeds omitted some legacy URLs; those are deliberately
retained until a second successful daily cycle confirms their absence.

## Restricted or incomplete requested sources

| Source | Status | Next requirement |
| --- | --- | --- |
| Realistimo | Disabled. Its current robots policy explicitly prohibits automated scraping/data mining for this use. | Obtain written permission, then enable and validate the existing adapter. |
| Address.bg | Its sale/rental adapters are configured, but clean unattended Chromium sessions currently receive HTTP 403. | Ask the publisher for feed/API access or provide an authorized browser session; keep failed cycles non-authoritative. |
| Imoteka | Clean headless sessions receive Cloudflare HTTP 403. | Capture an authorized browser storage state at `IMOTEKA_STORAGE_STATE_PATH`; do not attempt bypasses. |
| OLX | Adapter works and 1,219 unique URLs were stored, but the public result UI exposes only 25 pages while reporting more than 1,000 results. The target fails closed so it cannot reconcile/deactivate inventory. | Add stable geographic + property-type + non-overlapping price partitions, with overlap dedupe and completeness assertions. |
| ALO | Twenty nationwide sale/rental property-type targets are configured. | It has no listing sitemap; a complete cycle must reach the natural end of every category and is the largest browser-only runtime component. |
| Oikia | Disabled because its robots policy disallows `/listing/` and specifies a ten-second crawl delay. | Obtain permission before enabling detail collection. |

## Important additional coverage gaps

| Source/segment | Estimated scrapability | Why it matters |
| --- | --- | --- |
| Municipal and NRA public auctions | Medium | Adds tax and municipal disposals beyond BCPEA; each publisher has a different document-oriented workflow. |
| Bank repossessions outside DSK Home | Medium | Bank-owned inventory can be differentiated and less duplicated than portals, but feeds are fragmented. |
| Developer-direct project sites | Low-medium | Useful for earliest new-build availability, but there are hundreds of small sites and no common catalogue format. |
| Facebook groups/Marketplace | Low | Large supply but poor stability, access restrictions, and difficult compliance; not appropriate for the unattended scraper. |

## Runtime and deployment recommendation

Measured on the current local Docker stack:

- complete authoritative discovery, no writes: 2m04s;
- complete authoritative database update: 7m04s;
- BCPEA browser inventory: 2m29s;
- scraper container during the write test: about 0.5 GB RAM and 559 MB received;
- the complete browser tail is expected to take roughly 3–8 hours because
  ALO/Bazar page counts, polite delays, and anti-bot gates dominate it.

The daily index update is feasible. Schedule the complete index at 00:30 Europe/Sofia,
then drain only new/changed detail URLs. The initial million-listing detail
backfill is not a one-night job; at six global pages, at most two pages per
domain, and a planning service-time range of 2–4 seconds per detail, allow roughly
4–10 days for the bootstrap including throttling and retries. Once caught up,
a planning assumption of 1–5% daily new/changed inventory means about
12,000–58,000 detail pages, or roughly 2–12 hours. Measure actual seven-day
churn before setting the final SLA. Removed listings are marked inactive only
after two successful complete inventories omit them, so normal retirement
latency is about 24–48 hours rather than an unsafe immediate delete.

Recommended production layout:

| Component | Starting specification |
| --- | --- |
| Scraper/API VM | 8 vCPU, 16 GB RAM, 100 GB NVMe, 1 Gbit/s, Ubuntu 24.04 LTS, Docker, static outbound IP, EU region |
| PostgreSQL 16 | Managed service, 4 vCPU, 16 GB RAM, 300 GB SSD/NVMe to start, at least 3,000 provisioned IOPS, automatic storage growth |
| Database operations | Same region/private network as scraper, TLS, PITR 14–30 days, daily backup, connection pooling, disk alerts at 70/85% |

Use 6 domain workers for index discovery. Keep one index worker per domain and a
maximum of 2 detail pages per domain. Raising global concurrency above 6–8 does
not help slow single-domain catalogues and increases throttling risk.

## Verification performed

- 22 unit tests pass, covering sitemap/gzip parsing, URL filtering, pagination URL
  generation, disabled/duplicate target validation, dry-run write prevention,
  site-profile injection, scheduler parsing, update fingerprints, JSON-LD
  normalization, and PostgreSQL lock behavior.
- `node --check extension/content.js` passes.
- `docker compose config --quiet` passes.
- `git diff --check` passes.
- A forced Address.bg HTTP 403 smoke run exits with status 2, allowing the
  scheduler/monitoring layer to detect an incomplete market run.
- Live index smoke tests passed for ALO, OLX, BulgarianProperties, Bazar,
  Arco Real Estate, Mirela, and Property.bg.
- Live detail/API/database tests passed for ERA, Imoti.info, Home2U, DSK Home,
  BulgarianProperties, Revolution Estate, NoviteSgradi, IMOTNO, and BCPEA.

The 17-domain sitemap production run used full inventory cycles and reconciled
every domain successfully. Target-specific browser smoke/ingestion commands use
`--only`, so reconciliation remains disabled for those partial runs. The daily
full scheduler reconciles a domain only after every enabled target for that
domain succeeds and all completeness checks pass.

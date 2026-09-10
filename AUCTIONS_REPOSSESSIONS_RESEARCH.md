# Bulgarian public auctions and bank repossessions

Research and implementation snapshot: **10 September 2026**. Scope: publicly
accessible Bulgarian real-property inventories, nationwide where a national source
exists, with official publishers preferred over aggregators.

## Outcome

Five reliable sources are now part of both the regular inventory scraper and the
detail pipeline:

| Source | Inventory found | Detail result | Implementation |
|---|---:|---:|---|
| APPC national state-property platform | 17 | 17/17 | Live |
| UBB bank-owned/repossessed property | 63 | 63/63 | Live |
| Bulgarian Development Bank assets | 15 | 15/15 | Live |
| Sofia municipal property tenders | 155 property-relevant records | 155/155 | Live |
| Plovdiv municipal property notices | 23 property notices | 23/23 | Live, including PDF/DOCX |
| **Total** | **273** | **273/273** | **Stored in the database** |

The national APPC platform is the strongest non-enforcement source: it is the
official electronic-sale platform for private state property and property owned by
companies under majority state control.[^1] Its upcoming inventory exposes stable
auction IDs and structured fields including seller, settlement, region, cadastral
description, starting price, deposit, and dates.[^2]

UBB publishes a dedicated sale catalogue with stable `/sales/<id>` records and
separate real-estate, machinery, and vehicle categories.[^3] The scraper explicitly
selects real estate and proved natural pagination termination after 63 unique
properties. BDB publishes its real-property assets on a dedicated official page;
15 stable detail pages were available in this snapshot.[^4]

Sofia's official tender register contained 184 announcements across 19 pages; 155
matched real-property terminology after excluding movable goods, vehicles, waste,
vending-machine notices, withdrawals, and termination notices.[^5] Plovdiv's current
page was document-oriented: 26 PDF/DOCX links were present and 23 were property
notices. The implementation downloads and extracts both PDF and DOCX content rather
than treating the document link as sufficient detail.[^6]

## Other researched sources

| Source | Value | Scrapeability | Decision |
|---|---|---|---|
| NRA public sales | High; 283 current sales shown in this snapshot | Blocked for clean unattended Chromium by a verification challenge | Configured but disabled; seek an official feed/API or publisher permission for a persistent session. No challenge bypass. |
| Burgas municipality | Medium; real-estate sales and leases are present | Technically straightforward HTML, but the category mixes current, historical, movable-goods, and withdrawal records | Do not mark as authoritative until source-status/end-date semantics exist. |
| Varna municipality | Low current yield | Fragmented category with plans, results, cancellations, and older notices rather than a dependable active inventory | Research watch-list, not production inventory. |
| UniCredit Bulbank | Low incremental yield | Four properties appeared in the official table, but public-sale inventory links to the bank's `imot.bg` channel | Avoid duplicate ingestion because `imot.bg` is already an authoritative source. |
| Allianz Bank | Low and mixed | One procurement/tender page mixes a real-property item with vehicles and does not expose stable per-property URLs | Do not manufacture synthetic listing identity; monitor for a dedicated feed. |
| Ministry of Economy tender announcements | Discovery value | Large mixed announcement archive | APPC is the canonical structured source for the overlapping state-property sales. |
| Postbank, Fibank, ProCredit, CCB | Unknown/fragmented | No dependable current official per-property inventory was located | Quarterly re-check or direct publisher outreach. |

The NRA portal is unquestionably valuable and distinguishes current, postponed,
terminated, and archived sales, but unattended access currently stops at the site's
security verification.[^7] The right next step is institutional access, not evasion.
The portal also offers notification functionality, which may be a viable publisher-
approved ingestion route if NRA can document its terms or provide a machine feed.

Burgas proves why municipal onboarding needs a status model: its official register
contains valid property tenders alongside movable-goods sales and withdrawal
orders.[^8] Varna's official privatization section is similarly useful for discovery
but not a clean current-listings feed.[^9] UniCredit's official page is small and
redirects the broader public-sale catalogue to an `imot.bg` channel already covered
by ImotiAI.[^10] Allianz's official tender page presently mixes property and vehicle
disposals on one non-unique page.[^11]

## Database cleanup and recurrence prevention

Five provable non-property records were removed from both `listings` and their
`extraction_items` history: Facebook, Instagram, YouTube, `javascript:;`, and a
Cloudflare 504 help page. A narrow, auditable migration records the exact URLs. No
title-based bulk deletion and no ambiguous property record deletion were performed.
Known Sofia theme graphics and UBB/BDB logo or navigation images were also removed
from 233 canonical records; their raw source payloads remain preserved as evidence.

The regular runner now rejects:

- non-HTTP(S) item URLs;
- cross-domain links discovered inside a source page;
- URLs outside a target's explicit listing pattern;
- mixed-register records failing the target's property-title allow-list or matching
  its movable-goods/cancellation deny-list.

This prevents the identified navigation/promotional records from reappearing. A
promoted placement for a real property remains a property listing and is not deleted.

Final database verification:

- 1,229,171 canonical listings;
- 1,229,171 active listings;
- 720 detailed listings;
- 1,228,451 active listings still awaiting an initial detail scrape;
- zero duplicate canonical URLs;
- zero remaining records from the exact non-listing set;
- all 273 newly onboarded auction/repo records detailed.

## Current lifecycle: what actually happens

The index runner starts one inventory cycle per domain and attaches the cycle ID to
every successful index batch.

1. A seen URL is inserted or receives a lightweight `last_seen_at` heartbeat.
2. A new, reactivated, or index-changed URL is queued for detail by setting
   `detail_done=false`. Unchanged URLs do not create duplicate history rows.
3. A domain is reconciled only when every configured target succeeds. A failed or
   incomplete cycle does **not** mark anything missing.
4. If a domain with at least 100 active listings suddenly returns less than 50% of
   its previous active inventory, reconciliation is rejected.
5. A listing absent from one accepted complete cycle remains active with
   `missing_cycles=1`.
6. Absence from a second consecutive accepted complete cycle changes it to
   `active=false` and sets `inactive_at`.
7. Reappearance resets `missing_cycles`, clears `inactive_at`, reactivates the row,
   and queues detail again.

There is currently **no automatic hard deletion** of inactive listings and no code
that equates disappearance with “sold”. That is the safe behavior: portals remove
records for many reasons, and auctions can be postponed, terminated, awarded, or
simply moved to an archive. A hard-delete retention policy remains an architecture
decision.

For auction sources, the generic two-cycle disappearance rule should eventually be
augmented with `sale_channel`, `source_status`, `sale_start_at`, `sale_end_at`, and
`status_observed_at`. An explicitly terminated or ended auction can then leave the
active market immediately while its evidence and status history remain queryable.

## Scheduling assessment (explanation only)

The project already contains optional Docker scheduler services, but they should not
be enabled as a production policy without the decisions below.

Recommended daily sequence for a single local host:

1. **00:30 Europe/Sofia — index inventory.** Run all 60 effective targets at six
   concurrent domains. The most recent nationwide observed run took about 7 minutes;
   budget 15–30 minutes for normal variability and 60 minutes as the alert threshold.
2. **After a successful index completion — detail delta.** Process only active
   `detail_done=false` URLs (new, changed, or reactivated), with global concurrency
   6 and per-domain concurrency 1–2. Triggering by index completion is safer than a
   fixed clock because a slow index cannot overlap its consumer.
3. **Retry/dead-letter pass.** Failed detail URLs need attempt count, next-attempt
   time, error class, and quarantine/dead-letter state. They must not immediately
   occupy the head of the queue again.
4. **Reconciliation/report.** Alert on target failure, rejected inventory cycles,
   suspicious count deltas, queue age, and failure rate. Back up PostgreSQL before
   any separately approved purge job.

Daily incremental operation is feasible on the previously recommended local server.
The five new sources took under a minute to index concurrently, and 273 details were
processed successfully at two concurrent pages per domain. A realistic daily delta
of hundreds or a few thousand changed listings fits comfortably in an overnight
window. The existing 1.228-million-record initial detail backlog does not: at the
observed browser rate it represents days to weeks of continuous work and should be a
throttled one-time backfill, not part of the daily SLA.

Three scheduler limitations are important:

- `detail_runner --drain` currently stops only when the API returns zero candidates.
  A permanently failing URL remains eligible and can make the drain loop repeat
  indefinitely. Do not enable unrestricted drain until retry/quarantine semantics
  are chosen.
- An already-empty detail queue currently exits with code 2 even though there is no
  work to do. A production supervisor must not treat that as an incident; preferably
  the runner contract should later distinguish “empty/success” from “attempted but
  all failed”.
- The scheduler logs subprocess exit codes but has no distributed lock, run table,
  alert destination, or retry policy. One container will not overlap itself, but two
  scheduler instances can overlap. Production should use a PostgreSQL advisory lock
  (or an equivalent single-run lease) and persist run outcomes.

Systemd timers are the simplest host-level supervisor on Ubuntu; Docker Compose can
remain responsible for API, database, and scraper containers. The timer should start
one idempotent run command, record the exit status, and use `Persistent=true` so a
missed run starts after reboot. A Compose-resident scheduler is acceptable for a
single always-on host, but it still needs the database lock and health alerts above.

## Architecture decisions needed

1. Should inactive listings be retained indefinitely, or hard-deleted after a chosen
   period (for example 90 days) while keeping a tombstone/status history?
2. Should an auction that reaches its explicit end date become inactive immediately,
   or remain active until it disappears from the publisher's current feed?
3. Do municipal leases (shops, agricultural land, kiosks, parking, and small parts of
   public property) belong in the same market as sale listings? They are included now
   because the current requirement covers all property listing types.
4. Is the required initial-detail scope all 1.229 million active rows, or only new and
   changed rows from this point forward? This changes the backfill capacity plan by
   orders of magnitude.
5. May we ask NRA for a feed/API or permission to use an authenticated persistent
   browser profile? Without one, NRA should remain disabled rather than brittle.
6. Where should operational alerts go (email, Slack, or another channel), and what
   failure/count-drop threshold should wake a human?

## Sources

[^1]: [APPC — electronic platform for sale of state property](https://appk.government.bg/bg/privatizacia/about)
[^2]: [APPC electronic sales — upcoming public auctions](https://estate-sales.uslugi.io/upcoming-public)
[^3]: [UBB Estates — sale catalogue](https://estates.ubb.bg/)
[^4]: [Bulgarian Development Bank — real estate for sale](https://bbr.bg/bg/produkti-i-uslugi/prodazhba-na-aktivi/nedvijimi-imoti/)
[^5]: [Sofia Municipality — tenders](https://www.sofia.bg/tenders)
[^6]: [Plovdiv Municipality — auctions](https://www.plovdiv.bg/announcements/auctions/turgove/)
[^7]: [National Revenue Agency — public sales of real estate](https://sales.nra.bg/realestate)
[^8]: [Burgas Municipality — tenders](https://www.burgas.bg/bg/targove)
[^9]: [Varna Municipality — privatization](https://www.varna.bg/bg/privatizaciq)
[^10]: [UniCredit Bulbank — properties for sale](https://www.unicreditbulbank.bg/bg/za-nas/imoti-za-prodazhba/)
[^11]: [Allianz Bulgaria — tenders and disposals](https://www.allianz.bg/bg_BG/individuals/tender-and-procurement.html)

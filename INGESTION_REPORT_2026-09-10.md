# Initial market ingestion report — 2026-09-10

## Scope

The active configuration contains 81 effective inventory targets across 34
enabled domains. Every effective target has a same-domain listing URL regex.
The older regional fallbacks remain documented in `targets.yml`, but the loader
suppresses them whenever a complete authoritative sitemap exists for that
domain. This prevents the same catalogue from being crawled twice.

All original/main domains have been configured and invoked. After the recovery
and cleanup work, PostgreSQL holds **1,038,163 unique active listing URLs across
31 populated domains**. There are zero inactive rows and the unique `item_url`
constraint reports zero duplicates. Three configured domains remain empty due
to reproducible external access blockers described below.

The YAML file deliberately contains more records than the scheduler runs: 202
raw records across 37 domains, 25 explicitly disabled records, and 81 effective
targets across 34 enabled domains. Twenty-two disabled records are superseded
`imoti.info` regional partitions; its nationwide authoritative sitemap is used
instead. The other disabled domains are `realistimo.com` (explicit robots
restriction), `oikia.com` (detail crawling requires permission), and
`sales.nra.bg` (no stable public listing feed/publisher access). The loader also
suppresses enabled legacy regional fallbacks whenever a nationwide
authoritative feed exists. These are coverage definitions and fallbacks, not
missing scheduled work.

## Four empty domains

| Domain | Coverage implemented | Live result |
| --- | --- | --- |
| `address.bg` | Nationwide sale and rent. A source-specific HTTP inventory adapter reads the publisher's structured paginator, verifies every requested page number, accepts only rows where `is_active=1`, and then validates every listing redirect/status before insertion. | Cycle 69 completed both targets. Sale: 19,378 source rows, 19,322 visited (99.71%), 19,191 active accepted, 131 explicitly inactive rejected. Rent: 1,714 source rows, 1,712 visited and active. The cycle saw 20,898 URLs; its 54 previously known omissions were checked directly and all remained live, so they were correctly retained. The database now contains 20,952 active Address URLs. |
| `imoteka.bg` | Nationwide sale and rent targets, persistent Playwright storage-state support. | The origin returns an explicit Cloudflare human-verification page to clean sessions. It has not been bypassed or ingested without authorization. |
| `alo.bg` | Ten nationwide sale property categories and ten nationwide rent categories. Demand/service categories are deliberately excluded. | The shared publisher origin at `82.118.229.98` times out at TCP connect from this machine, before any HTTP response. |
| `en.realestates.bg` | The same 20 concrete sale/rent property categories as ALO's English catalogue. | It resolves to the same unreachable publisher IP and fails at TCP connect. |

`SCRAPER_PROXY_URL` is now supported by the browser and Chrome-compatible HTTP
validation transports. This is operational egress support, not an anti-bot
bypass. It allows ALO/Realestates to be retried from an approved server/network
route without changing code.

## Completeness and safety rules

- Address uses deterministic `?page=N` requests and checks the returned
  `current_page`, `last_page`, and `total`; a repeated or wrong page fails the
  source instead of silently losing results.
- A full Address cycle must visit at least 98% of all source-reported rows and
  separately emits only active rows. This distinction matters because Address
  currently keeps explicitly inactive records in its paginator. Below
  the source-row threshold the cycle fails and reconciliation is disabled.
- All targets are restricted by exact listing URL regexes. General search,
  location, category, and domain-root URLs cannot enter through the index
  runner.
- Definitive 404/410, cross-domain redirects, listing-to-category redirects,
  soft 404s, inactive pages, and empty invalid responses are rejected before a
  new row is inserted.
- 403, 429, timeouts, and 5xx responses are transient/unverifiable, not proof a
  listing was removed. They do not create new rows and do not deactivate old
  rows.
- Reconciliation occurs only after every target for a domain completes. A URL
  is marked inactive only after two successful complete cycles omit it. Failed,
  interrupted, truncated, or partial `--only` runs never mark listings missing.
- Overlapping category targets deduplicate through the database's unique
  `item_url` index and the runner's in-cycle URL sets.

## Concurrency

The default scheduler runs at most six domains concurrently. Large domains do
not split a moving page range between workers. Address fetches four explicit
pages concurrently, then validates at most six URLs from that domain at once
with a 75 ms start interval. A 500-listing stress run accepted 500/500 with no
dead or unverifiable URLs. At the earlier 12-validator setting, 27/500 became
temporarily unverifiable with HTTP 429; that result was rejected, the cycle was
closed as failed, and concurrency was reduced. OLX uses four paced API requests
at a time and never divides a moving page sequence between workers.

## Recovered capped inventory

OLX's website reports the real market size but exposes at most 1,000 results
for any one search. The former guarded browser targets could therefore retain
only about 2,700 URLs. The production adapter now reads the publisher's current
JSON inventory, covers sale, rent, and roommates across all 28 Bulgarian
regions, recursively subdivides any still-capped region by overlapping price
ranges, rejects non-active/non-offer records, and deduplicates range boundaries.

The live production proof recovered approximately 35,100 sale URLs, 14,284
rental URLs, and all 5 roommate URLs. Two complete three-target cycles repeated
the enumeration and applied reconciliation; cycle 71 saw 49,425 URLs and the
database currently holds 49,429 active OLX URLs. The small difference consists
of URLs retained by the two-successful-omissions rule during a moving live
catalogue. The separate `Imoti v chuzhbina` category is excluded because this
database is scoped to properties in Bulgaria.

## Current inventory snapshot

| Domain | Active URLs | Domain | Active URLs |
| --- | ---: | --- | ---: |
| `imoti.info` | 215,772 | `holmes.bg` | 206,427 |
| `imot.bg` | 171,834 | `dskhome.bg` | 107,523 |
| `homes.bg` | 81,747 | `imoti.net` | 66,398 |
| `imoti.com` | 55,413 | `olx.bg` | 49,429 |
| `address.bg` | 20,952 | `suprimmo.bg` | 9,518 |
| `home2u.bg` | 8,897 | `property.bg` | 7,488 |
| `era.bg` | 5,318 | `yavlena.com` | 5,278 |
| `bulgarianproperties.com` | 4,670 | `revolution-estate.bg` | 4,248 |
| `luximmo.com` | 3,808 | `bazar.bg` | 3,368 |
| `mirela.bg` | 2,448 | `ues.bg` | 1,883 |
| `sales.bcpea.org` | 1,528 | `buildingbox.bg` | 1,338 |
| `domaza.bg` | 1,015 | `arcoreal.bg` | 815 |
| `novitesgradi.bg` | 768 | `sofia.bg` | 155 |
| `estates.ubb.bg` | 63 | `plovdiv.bg` | 23 |
| `estate-sales.uslugi.io` | 17 | `bbr.bg` | 15 |
| `imotno.bg` | 7 | **Total** | **1,038,163** |

This is intentionally the requested light/index collection. Five records were
detailed only as smoke tests; 1,038,158 remain in the detail queue. Sitemap-only
rows can therefore have no title or image until the detail backfill runs. The
canonical URL, domain, activity heartbeat and lifecycle state are already
present for every row.

The current count is lower than a prior historical 1.6 million-row collection
because that figure included stale, inactive, generic/category and duplicate
records, and because two large publishers currently expose incomplete or
unavailable inventories. Counts are not padded with unverified rows.

### Large-source limitations

- `imot.bg`: a healthy earlier source snapshot exposed roughly 205,000 unique
  listing URLs. Its current official index contains 39 numbered shards with
  209,672 raw rows but only 158,116 unique URLs (75.41% uniqueness) because
  many shards overlap while the publisher rebuilds them in place. Gap recovery
  increased the retained database inventory to 171,834. The new 95% uniqueness
  guard fails this snapshot and disables reconciliation instead of incorrectly
  deleting the roughly 33,000 records temporarily absent from it.
- `imoti.com`: the database retains 55,413 validated URLs, but both its sitemap
  and individual listing requests currently return origin-wide HTTP 520. A
  historical healthy inventory was about 223,000. The scraper treats 520 as
  transient and therefore neither inserts unverified URLs nor deletes retained
  records.

## Verification performed

- 51 Python tests pass, including URL health classification, semaphore
  fairness, source-page JSON parsing, inactive-row rejection, proxy parsing,
  sitemap parsing/recovery, update fingerprints, detail normalization, and DB
  cycle behavior.
- `python -m py_compile` passes for the changed scraper modules.
- `git diff --check` and `docker compose config --quiet` pass.
- The rebuilt production scraper image passed a one-page Address test: 20/20
  valid.
- A 25-page Address stress test passed: 500/500 valid, zero dead, zero
  unverifiable.
- The OLX production adapter completed all sale, rent, and roommate partitions;
  its sale proof traversed 1,010 API pages and recovered 99.97% of the live
  source-reported count during a moving catalogue crawl.
- Exhaustive audit of all 1,038,163 active records: zero non-HTTP URLs, zero
  duplicates, and zero URLs outside the exact listing pattern configured for
  their domain. This excludes roots, location/search pages, categories and
  navigation/advertisement links.
- A live three-URL sample from every populated domain checked 93 records: 90
  valid, zero dead, and three unverifiable. All three unverifiable records were
  from `imoti.com` and returned its current origin-wide HTTP 520.
- The supplied dead Homes URL (`as1699996`) is not in the database. A separate
  1,560-URL Homes tail check rejected 29 definitive dead URLs before insert.
- Address and OLX detailed-scrape smoke tests produced meaningful descriptions
  for 2/2 and 3/3 records respectively.
- ALO and Realestates one-page container tests reproduce safe navigation
  timeouts. Imoteka reproduces HTTP 403 / human verification. None of those
  failed tests reconciled inventory.

## Running and recovery

Run a complete reconcilable domain cycle with:

```bash
docker compose run --rm scraper \
  python -u -m scraper.runner --only-domain address.bg
```

`--only NAME --page-limit N --dry-run` remains the smoke-test form and is never
reconcilable. The daily scheduler uses the complete target set. If ALO's origin
is still unreachable from the server, configure an approved static egress
route as `SCRAPER_PROXY_URL`; do not use a rotating proxy because stable source
behavior and reproducible validation matter more than request volume.

For an interrupted large sitemap, use the gap-aware recovery form:

```bash
docker compose run --rm scraper \
  python -u -m scraper.runner \
  --only imot_bg_inventory_sitemap \
  --sitemap-skip-existing
```

The runner asks the authenticated API which URLs in each batch are already
active, then validates and posts only the missing URLs. This avoids the unsafe
assumption that a database row count corresponds to a contiguous prefix of a
sitemap. It remains a partial, non-reconciling run; normal complete domain
cycles still revisit the whole source and own lifecycle reconciliation.

Imoteka requires an operator-authorized session at
`data/imoteka-storage-state.json` or a publisher feed/API. The current empty
state file has zero cookies and cannot pass the human-verification page.

## Daily operation and capacity

The repository includes optional index and detail scheduler containers. The
index run defaults to 00:30 Europe/Sofia; the detail queue defaults to 05:00.
For production, start them with the Compose `schedule` profile and supervise
Docker with systemd (Linux `Restart=always` plus a health check). A single
coordinator or systemd dependency should eventually start the detail drain only
after the index process finishes, rather than relying solely on the clock.

Observed individual runs range from about 40 seconds for a large clean sitemap
to 43 minutes for Address's 20,000 validated URLs; the nationwide OLX inventory
takes about three minutes. Allow **3–8 hours** for a normal complete daily index
window because browser-only sources and publisher throttling dominate. The
initial one-million-record detail backfill is a separate multi-day job: at six
polite browser workers, budget roughly **4–10 days**. After backfill, a daily
detail run processes only new/changed queue items and should usually fit in
**2–12 hours**, but this must be re-baselined from production metrics.

Recommended starting host:

- scraper/API: 8 dedicated vCPU, 16 GB RAM, 100 GB NVMe, 1 Gbit/s network,
  Ubuntu 24.04 LTS, Docker Engine/Compose, static Bulgarian or nearby EU egress;
- PostgreSQL 16: managed service or separate VM with 4 vCPU, 16 GB RAM,
  300 GB SSD/NVMe, at least 3,000 sustained IOPS, daily snapshots and point-in-
  time recovery;
- alert on failed/rejected inventory cycles, a domain count change above 20%,
  HTTP 429/5xx rates, detail backlog age, disk use and database backups.

These sizes leave headroom for six concurrent domain workers and Chromium. Do
not increase concurrency until per-domain 429 and completeness metrics remain
stable for at least several days.

## Listing lifecycle

1. A complete index run opens one inventory cycle per domain and records the
   exact number of targets expected for that domain.
2. Accepted URLs are inserted or updated, stamped with the cycle id and
   `last_seen_at`, reset to zero missing cycles, and remain active.
3. Reconciliation runs only if every expected target succeeds and the domain
   count passes the 50% collapse guard. A failed, blocked, partial or interrupted
   cycle records evidence but never marks anything missing.
4. An active URL absent from one successful complete cycle receives one missing
   strike. Absence from a second successful complete cycle marks it inactive.
   A transient timeout, 403, 429 or 5xx is never treated as removal evidence.
5. If the URL reappears, normal ingestion reactivates it. Inactive rows may be
   physically purged later under a separate retention policy; the production
   scrape itself does not need destructive deletion to stay current.

## Remaining acceptance conditions

The database is a clean production-ready initial collection for the 31
reachable/populated domains, but cannot honestly be called fully ingested for
all 34 enabled domains until these external conditions are resolved:

1. Imoteka human verification is completed by an authorized operator or the
   publisher supplies a feed.
2. ALO/Realestates is reachable from the production egress route.
3. `imot.bg` publishes a non-overlapping sitemap snapshot and `imoti.com`
   recovers from HTTP 520; additive retries can then fill their missing gaps.

The current behavior is fail-closed: unresolved domains stay empty, but they
cannot corrupt or deactivate the valid 31-domain collection.

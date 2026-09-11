# Detail ingestion verification — 2026-09-11

## Outcome

The detail pipeline was stopped when OLX began returning systemic HTTP 403
responses, hardened, and tested with a balanced sample of up to 100 pending
rows from every configured domain. The representative final samples covered
2,720 URLs: 2,119 detail snapshots were stored, 146 listings were confirmed
inactive, and 455 were safely deferred because the publisher blocked access.
Four configured domains had no canonical rows available to sample.

The production database now contains 1,038,163 canonical rows: 1,037,211
active, 952 inactive, 37,598 active rows with completed detail, and 999,613
active rows still awaiting detail.

## Balanced live results

`Stored` means meaningful listing-specific detail reached the API and database.
`Inactive` requires definitive evidence: HTTP 404/410, a same-domain redirect
to a category page, a recognized soft-404, or an exact publisher inactive
badge. `Deferred` remains active and pending.

| Domain | Sampled | Stored | Inactive | Deferred |
|---|---:|---:|---:|---:|
| address.bg | 100 | 2 | 0 | 98 |
| arcoreal.bg | 100 | 100 | 0 | 0 |
| bazar.bg | 100 | 95 | 5 | 0 |
| bbr.bg | 14 | 14 | 0 | 0 |
| buildingbox.bg | 100 | 99 | 1 | 0 |
| bulgarianproperties.com | 100 | 100 | 0 | 0 |
| domaza.bg | 100 | 100 | 0 | 0 |
| dskhome.bg | 100 | 1 | 0 | 99 |
| era.bg | 100 | 99 | 1 | 0 |
| estate-sales.uslugi.io | 16 | 16 | 0 | 0 |
| estates.ubb.bg | 62 | 62 | 0 | 0 |
| holmes.bg | 100 | 96 | 4 | 0 |
| home2u.bg | 100 | 100 | 0 | 0 |
| homes.bg | 100 | 100 | 0 | 0 |
| imot.bg | 100 | 93 | 7 | 0 |
| imoti.com | 100 | 0 | 0 | 100 |
| imoti.info | 100 | 100 | 0 | 0 |
| imoti.net | 100 | 99 | 1 | 0 |
| imotno.bg | 6 | 6 | 0 | 0 |
| luximmo.com | 100 | 100 | 0 | 0 |
| mirela.bg | 100 | 14 | 0 | 86 |
| novitesgradi.bg | 100 | 16 | 84 | 0 |
| olx.bg | 100 | 28 | 0 | 72 |
| plovdiv.bg | 22 | 22 | 0 | 0 |
| property.bg | 100 | 100 | 0 | 0 |
| revolution-estate.bg | 100 | 99 | 1 | 0 |
| sales.bcpea.org | 100 | 61 | 39 | 0 |
| sofia.bg | 100 | 100 | 0 | 0 |
| suprimmo.bg | 100 | 100 | 0 | 0 |
| ues.bg | 100 | 98 | 2 | 0 |
| yavlena.com | 100 | 99 | 1 | 0 |
| alo.bg | 0 | 0 | 0 | 0 |
| en.realestates.bg | 0 | 0 | 0 | 0 |
| imoteka.bg | 0 | 0 | 0 | 0 |
| realistimo.com | 0 | 0 | 0 | 0 |

The Novite Sgradi final sample was intentionally biased toward rows restored
during inactive-marker validation. All 84 removals were rechecked against the
direct `ПРОДАДЕНА / НЕАКТУАЛНА` ribbon or the legacy
`Изпуснахте тази сграда?` sold panel. No related-card text was accepted as
inactive evidence.

## Problems found and fixed

- A domain-level circuit breaker now opens after two explicit publisher blocks
  or five repeated extraction/runtime failures. Remaining rows are deferred,
  never deactivated. This stopped OLX after two 403s, DSK after its Radware
  redirect, and imoti.com after HTTP 520 responses.
- Navigation starts are paced per publisher. The default gap is 250 ms;
  Address, DSK, imoti.com, Mirela, and OLX run with one page worker and a 6.5 s
  start gap. The global production ceiling remains six pages, with no more than
  two pages for an ordinary publisher.
- Explicit block responses are not retried immediately. Retrying the same URL
  during an active publisher block only worsens throttling.
- OLX media accepts only Apollo/OLX immutable file-gallery URLs. Recommendation
  cards and banners from the page's image-resizer are rejected.
- Arco Real media accepts only first-party `/image?id=...` listing photos.
- BuildingBox keeps first-party upload media and rejects logos/chat assets.
- Homes anchors media to the current listing's upload directory and rejects
  broker logos and portraits.
- Home2U and imoti.net collapse responsive thumbnail variants to one logical
  photo.
- UES anchors images to the offer-specific Azure prefix, excluding site logos,
  OG placeholders, confidential placeholders, and related offers.
- Novite Sgradi uses only main gallery/featured-image anchors, rejects maps and
  site chrome, and scopes inactive checks to the direct sold elements.
- A completed detail response is now authoritative for media. If the page has
  no valid publisher-owned image, the backend clears stale `image`/`images`
  values instead of preserving an old logo or placeholder.
- A duplicate Python test class name was renamed; it had silently hidden two
  tests from standard `unittest` discovery.

## Correctness controls

- Detail completion requires a valid live listing page plus meaningful
  property text or structured listing evidence.
- Cookie consent, browser checks, access-denied copy, empty pages, HTTP 5xx,
  timeouts, and challenges are not accepted as detail.
- Cross-domain security/challenge redirects stay active and pending.
- A stable queue offset prevents failed rows at the head from starving the rest
  of a drain; the next scheduled run probes them again from offset zero.
- Detail titles update display text without changing the index fingerprint, so
  an unchanged listing is not requeued every day.
- Descriptions use PostgreSQL `EXTERNAL` TOAST storage to avoid invalid UTF-8
  boundaries observed with substring operations on some compressed Bulgarian
  text.

## Database validation

Post-run SQL checks returned:

- 0 completed rows with cookie/challenge/access-denied descriptions;
- 0 active completed rows missing a description or raw detail payload;
- 0 completed rows containing the known rejected logo, banner, chat, map-tile,
  confidential-placeholder, or lazy-placeholder media patterns.

The 146 representative inactive results were supported by publisher evidence.
Examples include Bazar/Holmes category redirects, direct 404s on imot.bg, ERA,
BuildingBox, imoti.net, Revolution and BCPEA, UES soft-404 pages, and exact
Novite Sgradi sold markers.

## Throughput and scheduling implications

The audit intentionally ran up to eight isolated domain processes at once,
with two page workers per ordinary domain, to complete broad validation quickly.
Production is more conservative: six pages globally and at most two per domain;
rate-sensitive publishers use one.

In the live audit, ordinary 100-row domains completed in roughly 2–7 minutes
under CPU contention; BuildingBox took about 12 minutes. A full initial detail
backfill of roughly one million pending rows is therefore a multi-day job. The
daily steady-state update is feasible because only new rows and rows whose
index fingerprint changed are requeued.

The existing `detail_scheduler` can run the drain daily at 05:00 Europe/Sofia.
It should remain single-instance. Blocked domains should be retried on the next
run or through an approved proxy/session arrangement, not by raising
concurrency.

## Automated verification

- 70 scraper/index-scheduler tests passed.
- 6 backend service tests passed, including authoritative clearing of stale
  detail media.
- `node --check scraper/detail_extractor.js` passed.
- `git diff --check` passed.

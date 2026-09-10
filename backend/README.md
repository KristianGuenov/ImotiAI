# Legal List Extractor (Backend) — Postgres-ready

This backend receives extraction payloads from the companion Chrome extension and stores them in a database.

---

## Quickstart (docker-compose, recommended)

From repo root:

```bash
docker compose up --build
```

API:
- `http://localhost:8787/health`
- `POST http://localhost:8787/api/v1/extractions` (requires `X-API-Key`)

---

## Quickstart (local Python + external Postgres)

1) Run Postgres (example with Docker):

```bash
docker run --name extractor-db -e POSTGRES_PASSWORD=extractor -e POSTGRES_USER=extractor -e POSTGRES_DB=extractor -p 5432:5432 -d postgres:16
```

2) Configure `.env`:

```env
API_KEY=dev-key-change-me
DATABASE_URL=postgresql+psycopg://extractor:extractor@localhost:5432/extractor
```

3) Run:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8787
```

---

## Custom Notes

### Real estate websites

#### OK
- Imot bg     OK
- Imoti.net.  OK
- Imoti.info. OK
- Bazar.bg Real Estate. OK
- Address.bg - nationwide structured inventory + live URL validation
- Holmes.bg - OK
- Luximmo.bg - OK
- domaza.bg - OK
- Mirela.bg  - OK
- arcoreal.bg - OK
- suprimmo  - OK
- Building Box - OK
- realestates.bg - configured; current publisher host is unreachable from this egress
- property.bg - OK
- imoti.com - OK
- Unique Estates - OK
- Yavlena  - OK
- homes.bg - OK

#### Nationwide inventory adapters

- ERA: complete published offer sitemap (sale and rent)
- Home2U (`home2u.bg`): published project and apartment sitemaps
- DSK Home: published listing sitemap index and shards
- Bulgarian Properties: deterministic `indexN.html` sale and rental pagination
- Imoti.info: published sale and rental sitemap shards
- Imoti.net: complete detail-listing sitemap shards
- Revolution Estate: published Bulgarian property sitemap
- ALO: separate nationwide sale and rental property-type pagination targets
- Address: server-rendered sale/rent paginator, explicit active-row filtering,
  and status/redirect validation
- OLX: official current JSON inventory, partitioned across all 28 Bulgarian
  regions and recursively by price to avoid its 1,000-result query cap
- NoviteSgradi and IMOTNO: published listing sitemaps
- BCPEA public sales: numbered nationwide auction pagination

#### Authorization/session-gated

- Realistimo is disabled because its current robots policy prohibits automated
  scraping/data mining without prior written permission.
- Imoteka remains configured but Cloudflare rejects clean headless sessions. Set
  `IMOTEKA_STORAGE_STATE_PATH` to reuse an authorized browser session; an empty or
  blocked run fails its inventory cycle and cannot deactivate listings.

---

## Homes.bg: API pager

This is the command to run the pager for homes.bg

```bash
docker compose --profile scrape run --rm   -e DATABASE_URL='%URL'   scraper   python /app/scraper/homes_api_pager.py     --partitions /app/data/homes_partitions.txt     --db     --db-table extraction_items
```

## Safe scraper verification

Run one target without writing inventory cycles or extraction data:

```bash
docker compose --profile scrape run --rm scraper \
  python -m scraper.runner --only era_inventory_sitemap --dry-run
```

The runner rejects duplicate target names/URLs and unsupported modes at startup.
Targets that discover fewer than `min_items` fail closed. Published sitemap feeds
are processed sequentially and posted in bounded batches.

Repair gaps after an interrupted sitemap run without revalidating known active
URLs:

```bash
docker compose --profile scrape run --rm scraper \
  python -m scraper.runner --only imot_bg_inventory_sitemap \
  --sitemap-skip-existing
```

This recovery form is deliberately non-reconciling. Complete scheduled domain
cycles remain responsible for missing/inactive lifecycle decisions.

Run only the fast authoritative inventories, with six domains in flight:

```bash
docker compose --profile scrape run --rm scraper \
  python -m scraper.runner --only-mode sitemap --domain-concurrency 6
```

Enable the optional daily inventory and detail schedulers:

```bash
docker compose --profile schedule up -d --build index_scheduler detail_scheduler
```

`INDEX_RUN_AT` defaults to `00:30` Europe/Sofia and
`SCRAPER_DOMAIN_CONCURRENCY` defaults to `6`. A listing is made inactive only
after two complete successful domain inventories omit it. Failed, truncated, or
blocked runs never remove listings.

---

## Domain audit + detail rules automation (audit.py)

We introduced an **audit workflow** to onboard many domains for detail scraping by:

- capturing **3 representative detail pages** per domain,
- extracting structural “signals” (JSON-LD presence, state blob presence, KV tables, images, etc.),
- auto-classifying each domain into an extraction **template family**,
- and generating/updating `detail_rules.yml` so **every domain is covered** by detail URL include/exclude rules.

### Key files produced/used

- `scraper/domain_audit.yml`  
  Stores:
  - one entry per domain,
  - 3 sample URLs (manually filled if not using autofill),
  - `detected` signals (written by `audit.py run`),
  - `classification` (written by `audit.py classify`).

- `scraper/audits/`  
  Contains one folder per domain with:
  - `standard.json`
  - `rich_features.json`
  - `different_type_or_edge.json`
  - `domain_detection.json`

- `scraper/detail_rules.yml`  
  Rules used by the detail scraper to decide what is a “detail URL” per domain:
  - defaults block + per-domain include/exclude patterns + concurrency.
  - `audit.py write-rules` fills and updates this file **in place**.

### Where audit.py should live

Place `audit.py` inside the `scraper/` folder so it runs at:

- `/app/scraper/audit.py` (inside Docker)

### Commands for running audit.py

Build the scraper image (no-cache):

```bash
docker compose build --no-cache scraper
```

#### 1) Initialize domain_audit.yml (domains discovered from targets.yml)

```bash
docker compose run --rm scraper   python /app/scraper/audit.py init   --targets /app/scraper/targets.yml   --out /app/scraper/domain_audit.yml
```

> NOTE: Samples may be filled manually in `domain_audit.yml`.

#### 2) Run the audit scrape (writes audits/* and fills detected signals)

```bash
docker compose run --rm scraper   python /app/scraper/audit.py run   --audit /app/scraper/domain_audit.yml   --audits-dir /app/scraper/audits
```

#### 3) Classify domains into template families (based on detected signals)

```bash
docker compose run --rm scraper   python /app/scraper/audit.py classify   --audit /app/scraper/domain_audit.yml
```

#### 4) Update detail_rules.yml in place (complete + regenerate per-domain rules)

Safe (fills missing domains, minimal overwrite):
```bash
docker compose run --rm scraper   python /app/scraper/audit.py write-rules   --audit /app/scraper/domain_audit.yml   --rules /app/scraper/detail_rules.yml
```

Full regeneration behavior (overwrite include/concurrency for all domains):
```bash
docker compose run --rm scraper   python /app/scraper/audit.py write-rules   --audit /app/scraper/domain_audit.yml   --rules /app/scraper/detail_rules.yml   --force
```

---

## Troubleshooting

### "I wrote domain_audit.yml but I don't see it on my host"
Confirm your `docker-compose.yml` mounts:

- `./scraper:/app/scraper`

If no mount exists, the file was written only in the container filesystem and disappears after the container exits.

### "audit.py run --domain homes.bg says: ⚠️ no domains to audit"
`--domain` must match exactly what’s in `domain_audit.yml` (e.g., `www.homes.bg` vs `homes.bg`).  
Run without `--domain` to audit all domains.

### "write-rules fails with: Read-only file system: /app/scraper/detail_rules.yml"
This means `/app/scraper` is not writable inside the container. Common causes:

1) The folder is coming from the image layer (not bind-mounted), or it is mounted read-only.
2) Your compose `volumes:` entry uses `:ro`.

Fixes:
- Ensure your compose mounts `./scraper:/app/scraper` **without** `:ro`.
- Then rerun `write-rules`.

Workaround (if you can’t change mounts immediately):
- Write to a writable path (e.g., `/app/data/detail_rules.yml`) and copy back on the host.
  Example:
  ```bash
  docker compose run --rm scraper     python /app/scraper/audit.py write-rules     --audit /app/scraper/domain_audit.yml     --rules /app/data/detail_rules.yml
  ```
  Then copy `/app/data/detail_rules.yml` out of the container or move it on the host.

---

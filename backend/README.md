# Legal List Extractor (Backend) — Postgres-ready

This backend receives extraction payloads from the companion Chrome extension and stores them in a database.

## Quickstart (docker-compose, recommended)

From repo root:

```bash
docker compose up --build
```

API:
- `http://localhost:8787/health`
- `POST http://localhost:8787/api/v1/extractions` (requires `X-API-Key`)

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

Cuystome Notes
Real estate websites:
Imot bg     OK
Imoti.net.  OK
Imoti.info. OK
Bazar.bg Real Estate. OK
Address.bg - OK
Holmes.bg - OK
Luximmo.bg - OK
domaza.bg - OK
Mirela.bg  - OK
arcoreal.bg - OK
suprimmo  - OK

Scroll
Unique Estates - load more
Yavlena  - infinite scroll 
homes.bg - infinite scroll


Currently
Building Box
Realistimo

Later - buggy
Homes2u.bg
Imoteka.bg - leave for now
dskhome
BulgarianProperties.bg

Next
imoti.com
realestates.bg
property.bg
era.bg


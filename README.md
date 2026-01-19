# Legal List Extractor — Postgres Edition (Extension + Backend)

This project replicates the core *in-browser* approach used by one-click list scrapers:
it detects repeating DOM structures in the current tab and extracts each item into JSON.

**Use only where you have authorization and where extraction complies with the site’s terms and applicable law.**

## Run with Docker (Postgres + API)

```bash
docker compose up --build
```

API:
- `http://localhost:8787/health`
- `POST http://localhost:8787/api/v1/extractions` (requires `X-API-Key`)

## Install Chrome extension (unpacked)

1. Chrome → `chrome://extensions`
2. Enable **Developer mode**
3. **Load unpacked** → select the `extension/` folder

## Configure extension

Open the extension Options:
- Endpoint: `http://localhost:8787/api/v1/extractions` (or your server URL)
- API key: matches backend `API_KEY`
- Optional: enable **Auto-send** after each extraction

## Notes on Postgres

For production, set:
- `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db`

## Important security note

The extension should **not** connect directly to Postgres. Instead:
Extension → HTTPS API → Postgres.
Direct DB connections from a browser are insecure and not feasible on the public internet.


## Extension: Pagination and Load More

The popup includes:
- **Extract this page**
- **Extract next pages** (pagination batch): clicks the site’s Next-page control and extracts each page up to a max.
- **Load more + extract**: scrolls to trigger “infinite scroll” loading and extracts once the page stabilizes.

Notes:
- Batch pagination is operator-initiated from the popup.
- It stops when no next page is found or when the max page count is reached.

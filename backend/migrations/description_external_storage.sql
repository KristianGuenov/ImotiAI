BEGIN;

-- Some PostgreSQL 16/aarch64 installations can return an invalid UTF-8 boundary
-- when text_left()/substring() reads a compressed multilingual TOAST value.
-- Full values remain valid UTF-8, but previews such as LEFT(description, 240)
-- can fail. Store descriptions externally without PGLZ compression so both full
-- reads and character-based previews are reliable. Raw JSON payloads retain the
-- default compression policy.
ALTER TABLE listings ALTER COLUMN description SET STORAGE EXTERNAL;

-- Rewrite existing detail values under the new storage policy.
UPDATE listings
SET description = description || ''
WHERE detail_done = TRUE
  AND description IS NOT NULL;

COMMIT;

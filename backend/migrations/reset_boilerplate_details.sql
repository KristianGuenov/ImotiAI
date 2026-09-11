BEGIN;

-- Detail extraction must never treat consent/challenge copy as property text.
-- Keep valid index media/title state, but clear the invalid detail snapshot so
-- the corrected extractor can retry it.
UPDATE listings
SET detail_done = FALSE,
    detail_scraped_at = NULL,
    description = NULL,
    description_hash = NULL,
    raw_payload = NULL
WHERE lower(coalesce(description, '')) LIKE '%използваме бисквитки%'
   OR lower(coalesce(description, '')) LIKE '%настройки на бисквитките%'
   OR lower(coalesce(description, '')) LIKE '%verify you are human%'
   OR lower(coalesce(description, '')) LIKE '%enable javascript and cookies%'
   OR lower(coalesce(description, '')) LIKE '%checking your browser%';

COMMIT;

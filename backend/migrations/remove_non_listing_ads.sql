BEGIN;

-- Exact, reviewed non-property records produced by old broad list selectors.
-- Keep this allow-list intentionally narrow: no title heuristics and no domain-wide
-- deletion are used here, so legitimate listings cannot be removed accidentally.
CREATE TEMP TABLE non_listing_urls (item_url TEXT PRIMARY KEY) ON COMMIT DROP;
INSERT INTO non_listing_urls (item_url) VALUES
    ('https://www.facebook.com/superimoti.bg/'),
    ('https://www.youtube.com/channel/UCDQAYsLdhB2DQNzKBmZloTw'),
    ('https://www.instagram.com/suprimmo.bg/'),
    ('javascript:;'),
    ('https://www.cloudflare.com/5xx-error-landing?utm_source=errorcode_504&utm_campaign=www.holmes.bg');

CREATE TEMP TABLE affected_runs (id INTEGER PRIMARY KEY) ON COMMIT DROP;
INSERT INTO affected_runs (id)
SELECT DISTINCT run_id
FROM extraction_items
WHERE item_url IN (SELECT item_url FROM non_listing_urls);

DELETE FROM extraction_items
WHERE item_url IN (SELECT item_url FROM non_listing_urls);

UPDATE extraction_runs AS run
SET item_count = (
    SELECT COUNT(*)
    FROM extraction_items AS item
    WHERE item.run_id = run.id
)
WHERE run.id IN (SELECT id FROM affected_runs);

DELETE FROM listings
WHERE item_url IN (SELECT item_url FROM non_listing_urls);

COMMIT;

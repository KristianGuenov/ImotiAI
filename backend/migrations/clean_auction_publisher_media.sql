BEGIN;

-- Remove publisher logos/navigation/theme graphics from canonical listing media.
-- The untouched raw_payload remains available as source evidence.
WITH cleaned AS (
    SELECT
        id,
        ARRAY(
            SELECT media_url
            FROM unnest(COALESCE(images, ARRAY[]::TEXT[])) AS media_url
            WHERE CASE domain
                WHEN 'sofia.bg' THEN
                    media_url NOT LIKE '%/image/layout_set_logo%'
                    AND media_url NOT LIKE '%/o/epsof-0601-theme/%'
                WHEN 'estates.ubb.bg' THEN
                    media_url NOT LIKE '%/images/og_image.png%'
                    AND media_url NOT LIKE '%/images/arrow-top.png%'
                WHEN 'bbr.bg' THEN
                    rtrim(media_url, '/') <> 'https://bbr.bg'
                    AND media_url NOT LIKE '%/static/dist/assets/images/default-card-img.png%'
                ELSE TRUE
            END
        ) AS clean_images
    FROM listings
    WHERE domain IN ('sofia.bg', 'estates.ubb.bg', 'bbr.bg')
)
UPDATE listings AS listing
SET images = cleaned.clean_images,
    image = CASE
        WHEN listing.domain = 'sofia.bg'
             AND (listing.image LIKE '%/image/layout_set_logo%'
                  OR listing.image LIKE '%/o/epsof-0601-theme/%')
            THEN cleaned.clean_images[1]
        WHEN listing.domain = 'estates.ubb.bg'
             AND (listing.image LIKE '%/images/og_image.png%'
                  OR listing.image LIKE '%/images/arrow-top.png%')
            THEN cleaned.clean_images[1]
        WHEN listing.domain = 'bbr.bg'
             AND (rtrim(listing.image, '/') = 'https://bbr.bg'
                  OR listing.image LIKE '%/static/dist/assets/images/default-card-img.png%')
            THEN cleaned.clean_images[1]
        ELSE listing.image
    END
FROM cleaned
WHERE listing.id = cleaned.id;

COMMIT;

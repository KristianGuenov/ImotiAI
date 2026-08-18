BEGIN;

CREATE TABLE IF NOT EXISTS inventory_cycles (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    targets_expected INTEGER NOT NULL DEFAULT 0,
    targets_succeeded INTEGER NOT NULL DEFAULT 0,
    targets_failed INTEGER NOT NULL DEFAULT 0,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    listings_missing INTEGER NOT NULL DEFAULT 0,
    listings_deactivated INTEGER NOT NULL DEFAULT 0,
    meta_json JSON NULL
);

CREATE INDEX IF NOT EXISTS ix_inventory_cycles_domain ON inventory_cycles(domain);
CREATE INDEX IF NOT EXISTS ix_inventory_cycles_status ON inventory_cycles(status);

ALTER TABLE listings ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_seen_cycle_id INTEGER NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS missing_cycles INTEGER NOT NULL DEFAULT 0;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS inactive_at TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_listings_last_seen_cycle_id'
    ) THEN
        ALTER TABLE listings
        ADD CONSTRAINT fk_listings_last_seen_cycle_id
        FOREIGN KEY (last_seen_cycle_id)
        REFERENCES inventory_cycles(id)
        ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_listings_active ON listings(active);
CREATE INDEX IF NOT EXISTS ix_listings_last_seen_at ON listings(last_seen_at);
CREATE INDEX IF NOT EXISTS ix_listings_last_seen_cycle_id ON listings(last_seen_cycle_id);
CREATE INDEX IF NOT EXISTS ix_listings_domain_active ON listings(domain, active);
CREATE INDEX IF NOT EXISTS ix_listings_domain_last_seen_cycle ON listings(domain, last_seen_cycle_id);

-- Existing rows start active. Presence fields will be populated by the next full index cycle.
UPDATE listings
SET active = TRUE,
    missing_cycles = COALESCE(missing_cycles, 0)
WHERE active IS DISTINCT FROM TRUE OR missing_cycles IS NULL;

COMMIT;
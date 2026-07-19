-- =====================================================================
-- iSupply Scan — heartbeat: kdo právě používá aplikaci
-- Migrace 004
-- =====================================================================

BEGIN;

-- ON CONFLICT v heartbeatu potřebuje tenhle unikátní index.
-- Tabulka `activations` ho už z app.py má jako UNIQUE(license_id, hwid),
-- ale pro jistotu ho vytvoříme, kdyby chyběl.
CREATE UNIQUE INDEX IF NOT EXISTS activations_license_hwid_idx
  ON activations (license_id, hwid);

-- Rychlé hledání, kdo je právě online
CREATE INDEX IF NOT EXISTS activations_last_seen_idx
  ON activations (last_seen DESC);

COMMIT;

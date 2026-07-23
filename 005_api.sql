-- =====================================================================
-- iSupply Scan — migrace 005: zákaznické API + veřejné ověření skenu
--
-- Staví na tom, co už existuje (devices / device_components /
-- device_component_history / scan_events). Nic nepřepisuje.
--
-- Přidává:
--   1) API klíč na licenci  — aby si zákazník mohl data tahat programově
--   2) Veřejný ověřovací kód u skenu — odkaz, který obchodník dá
--      ke svojí nabídce a koncový kupující si podle něj ověří test
--   3) Souhrn skenu (grade, kondice baterie, počet OK komponent),
--      aby veřejná stránka nemusela dopočítávat z historie
-- =====================================================================

BEGIN;

-- ── 1) API klíč ──────────────────────────────────────────────────────
-- Zámerne NENÍ to samé co license_key: ten slouží k aktivaci aplikace
-- a nechceme, aby kolovala v integracích. API klíč jde kdykoli otočit,
-- aniž by to shodilo aplikace u techniků.
ALTER TABLE licenses
  ADD COLUMN IF NOT EXISTS api_key         TEXT,
  ADD COLUMN IF NOT EXISTS api_key_created TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS licenses_api_key_idx
  ON licenses (api_key) WHERE api_key IS NOT NULL;

-- ── 2) Veřejné ověření skenu ────────────────────────────────────────
-- Krátký kód v odkazu isupply-scan.cz/overit/<kod>. Musí být náhodný,
-- ne pořadové číslo — jinak by šlo procházet cizí skeny inkrementem.
ALTER TABLE scan_events
  ADD COLUMN IF NOT EXISTS verify_code      TEXT,
  ADD COLUMN IF NOT EXISTS battery_health   INTEGER,
  ADD COLUMN IF NOT EXISTS battery_cycles   INTEGER,
  ADD COLUMN IF NOT EXISTS components_ok    INTEGER,
  ADD COLUMN IF NOT EXISTS components_total INTEGER,
  ADD COLUMN IF NOT EXISTS technician       TEXT,
  ADD COLUMN IF NOT EXISTS published        BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS scan_events_verify_idx
  ON scan_events (verify_code) WHERE verify_code IS NOT NULL;

-- Rychlé listování přes API: nejnovější skeny licence.
CREATE INDEX IF NOT EXISTS scan_events_licence_time_idx
  ON scan_events (license_id, created_at DESC);

-- ── 3) Životní cyklus kusu ──────────────────────────────────────────
-- Zámerne NEDĚLÁME sklad (množství, rezervace, výdejky) — to je práce
-- e-shopu a ERP. Tohle je jen stav kusu, ať technik pozná, že ho už
-- někdy měl v ruce, a ať jde napojit e-shop.
ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'tested',
  ADD COLUMN IF NOT EXISTS last_grade      TEXT,
  ADD COLUMN IF NOT EXISTS external_ref    TEXT;

ALTER TABLE devices
  DROP CONSTRAINT IF EXISTS devices_lifecycle_chk;
ALTER TABLE devices
  ADD CONSTRAINT devices_lifecycle_chk CHECK (
    lifecycle_state IN ('tested','listed','sold','returned','scrapped'));

-- ── 4) Odchozí webhooky ─────────────────────────────────────────────
-- Univerzální cesta pro e-shopy, na které nebudeme psát konektor.
CREATE TABLE IF NOT EXISTS webhooks (
  id          BIGSERIAL PRIMARY KEY,
  license_id  INTEGER     NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
  url         TEXT        NOT NULL,
  secret      TEXT        NOT NULL,
  event       TEXT        NOT NULL DEFAULT 'scan.completed',
  active      BOOLEAN     NOT NULL DEFAULT TRUE,
  last_status INTEGER,
  last_error  TEXT,
  last_sent_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhooks_licence_idx
  ON webhooks (license_id) WHERE active;

COMMIT;

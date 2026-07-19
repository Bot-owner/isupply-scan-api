-- =====================================================================
-- iSupply Scan — kvóty skenů, kredity, trvalá baseline komponent
-- Migrace 001  ·  VERZE PRO SKUTEČNÉ SCHÉMA Z app.py
--
-- Tabulka se jmenuje `licenses` (ne licences) a má sloupce
-- license_key / plan / active. Migrace ji jen rozšiřuje,
-- nic stávajícího nepřepisuje ani neruší.
-- =====================================================================

BEGIN;

-- ── 1) Rozšíření tabulky licenses ────────────────────────────────────
ALTER TABLE licenses
  ADD COLUMN IF NOT EXISTS scan_limit   INTEGER     NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS unlimited    BOOLEAN     NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS period_start TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS period_end   TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 days',
  ADD COLUMN IF NOT EXISTS vat_id       TEXT,
  ADD COLUMN IF NOT EXISTS stripe_customer_id     TEXT,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS licenses_stripe_sub_idx
  ON licenses (stripe_subscription_id)
  WHERE stripe_subscription_id IS NOT NULL;

UPDATE licenses SET scan_limit = CASE lower(plan)
    WHEN 'test'     THEN 50
    WHEN 'basic'    THEN 200
    WHEN 'pro'      THEN 500
    WHEN 'business' THEN 1000
    ELSE scan_limit END
WHERE scan_limit = 0;

UPDATE licenses SET unlimited = TRUE WHERE lower(plan) = 'enterprise';

-- ── 2) Dokoupené kredity ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS credit_packs (
  id                    BIGSERIAL PRIMARY KEY,
  license_id            INTEGER     NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
  amount                INTEGER     NOT NULL CHECK (amount > 0),
  remaining             INTEGER     NOT NULL CHECK (remaining >= 0),
  price_cents           INTEGER,
  currency              TEXT        NOT NULL DEFAULT 'eur',
  stripe_payment_intent TEXT        UNIQUE,
  stripe_session_id     TEXT        UNIQUE,
  expires_at            TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS credit_packs_usable_idx
  ON credit_packs (license_id, expires_at NULLS LAST, created_at)
  WHERE remaining > 0;

-- ── 3) Fakturační ledger skenů ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS scan_events (
  id             BIGSERIAL PRIMARY KEY,
  license_id     INTEGER     NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
  imei           TEXT        NOT NULL,
  billed         BOOLEAN     NOT NULL,
  parent_id      BIGINT      REFERENCES scan_events(id) ON DELETE SET NULL,
  free_until     TIMESTAMPTZ,
  source         TEXT        CHECK (source IN ('subscription','credit_pack')),
  credit_pack_id BIGINT      REFERENCES credit_packs(id) ON DELETE SET NULL,
  period_start   TIMESTAMPTZ NOT NULL,
  model          TEXT,
  ios_version    TEXT,
  grade          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scan_events_billed_shape CHECK (
    (billed AND free_until IS NOT NULL AND source IS NOT NULL) OR
    (NOT billed AND parent_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS scan_events_window_idx
  ON scan_events (license_id, imei, free_until DESC) WHERE billed;

CREATE INDEX IF NOT EXISTS scan_events_usage_idx
  ON scan_events (license_id, period_start) WHERE billed;

-- ── 4) TRVALÁ evidence zařízení a komponent ──────────────────────────
CREATE TABLE IF NOT EXISTS devices (
  imei             TEXT PRIMARY KEY,
  serial_number    TEXT,
  model            TEXT,
  model_identifier TEXT,
  capacity_gb      INTEGER,
  color            TEXT,
  first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  scan_count       INTEGER     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS device_components (
  imei          TEXT        NOT NULL REFERENCES devices(imei) ON DELETE RESTRICT,
  component     TEXT        NOT NULL,
  is_factory    BOOLEAN     NOT NULL DEFAULT FALSE,
  serial        TEXT,
  source_path   TEXT,
  source_key    TEXT,
  ios_version   TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (imei, component, is_factory)
);

CREATE TABLE IF NOT EXISTS device_component_history (
  id            BIGSERIAL PRIMARY KEY,
  imei          TEXT        NOT NULL REFERENCES devices(imei) ON DELETE RESTRICT,
  component     TEXT        NOT NULL,
  old_serial    TEXT,
  new_serial    TEXT,
  verdict       TEXT        CHECK (verdict IN ('MATCH','MISMATCH','POSSIBLE_FAULT','FIRST_SEEN')),
  scan_event_id BIGINT      REFERENCES scan_events(id) ON DELETE SET NULL,
  source_path   TEXT,
  source_key    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS device_component_history_imei_idx
  ON device_component_history (imei, created_at DESC);

COMMIT;

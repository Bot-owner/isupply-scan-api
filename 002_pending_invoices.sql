-- =====================================================================
-- iSupply Scan — fronta faktur čekajících na ruční vystavení
-- Migrace 002
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS pending_invoices (
  id                  BIGSERIAL PRIMARY KEY,
  ref                 TEXT        NOT NULL UNIQUE,   -- krátká značka do Telegramu, např. OBJ-4F2A9C
  licence_id          INT         REFERENCES licences(id) ON DELETE SET NULL,

  -- fakturační údaje ze Stripe checkoutu
  email               TEXT        NOT NULL,
  company             TEXT,
  vat_id              TEXT,
  address             TEXT,
  country             TEXT,

  -- co se platilo
  kind                TEXT        NOT NULL CHECK (kind IN ('subscription','credits')),
  description         TEXT        NOT NULL,
  amount_cents        INT         NOT NULL,
  currency            TEXT        NOT NULL DEFAULT 'eur',

  stripe_session_id   TEXT        UNIQUE,
  stripe_payment_intent TEXT,

  -- vazba na zprávu v Telegramu, aby šlo fakturu přiřadit odpovědí
  telegram_message_id BIGINT,

  status              TEXT        NOT NULL DEFAULT 'waiting'
                                  CHECK (status IN ('waiting','sent','cancelled')),
  invoice_filename    TEXT,
  sent_at             TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pending_invoices_waiting_idx
  ON pending_invoices (created_at)
  WHERE status = 'waiting';

CREATE INDEX IF NOT EXISTS pending_invoices_tg_idx
  ON pending_invoices (telegram_message_id)
  WHERE status = 'waiting';

COMMIT;

-- =====================================================================
-- iSupply Scan — napojení na POHODU přes lokálního agenta
-- Migrace 003
-- =====================================================================

BEGIN;

ALTER TABLE pending_invoices
  -- jak zákazník zaplatil: kartou přes Stripe, nebo převodem na fakturu
  ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT 'card'
      CHECK (payment_method IN ('card','transfer')),

  -- co přidělila POHODA (nikdy negenerujeme sami)
  ADD COLUMN IF NOT EXISTS pohoda_number   TEXT,
  ADD COLUMN IF NOT EXISTS pohoda_vs       TEXT,
  ADD COLUMN IF NOT EXISTS pohoda_synced_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS pohoda_error    TEXT,
  ADD COLUMN IF NOT EXISTS pohoda_attempts INT NOT NULL DEFAULT 0,

  -- stav vůči agentovi
  ADD COLUMN IF NOT EXISTS pohoda_status TEXT NOT NULL DEFAULT 'queued'
      CHECK (pohoda_status IN ('queued','claimed','done','failed','skipped')),
  ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- unikátnost čísla dokladu — pojistka proti dvojímu zápisu téže faktury
CREATE UNIQUE INDEX IF NOT EXISTS pending_invoices_pohoda_number_idx
  ON pending_invoices (pohoda_number)
  WHERE pohoda_number IS NOT NULL;

-- fronta pro agenta
CREATE INDEX IF NOT EXISTS pending_invoices_pohoda_queue_idx
  ON pending_invoices (created_at)
  WHERE pohoda_status IN ('queued','claimed');

-- ── Výplaty ze Stripe = převod mezi účty, ne úhrada faktur ───────────
CREATE TABLE IF NOT EXISTS stripe_payouts (
  id                BIGSERIAL PRIMARY KEY,
  stripe_payout_id  TEXT        NOT NULL UNIQUE,
  amount_cents      INT         NOT NULL,      -- co reálně dorazí na účet
  gross_cents       INT,                       -- hrubý objem plateb ve výplatě
  fee_cents         INT,                       -- poplatky Stripe = náklad
  currency          TEXT        NOT NULL DEFAULT 'eur',
  arrival_date      DATE,
  pohoda_status     TEXT        NOT NULL DEFAULT 'queued'
                                CHECK (pohoda_status IN ('queued','claimed','done','failed','skipped')),
  pohoda_number     TEXT,
  pohoda_error      TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

"""
iSupply Scan — API pro lokálního POHODA agenta.

Agent běží na tvém PC vedle POHODY a pouze se PTÁ tohoto serveru.
Ven se nic neotevírá, veškerá komunikace je odchozí z tvé sítě.

Tok:
    agent  → GET  /api/pohoda/queue      "máš něco k vystavení?"
    agent  → POST /api/pohoda/result     "hotovo, POHODA přidělila číslo X, VS Y"
    agent  → POST /api/pohoda/heartbeat  "žiju" (pro hlídání výpadku)

Autentizace: hlavička  X-Agent-Token: <POHODA_AGENT_TOKEN>

ENV: POHODA_AGENT_TOKEN (dlouhý náhodný řetězec), TELEGRAM_* pro upozornění
"""

import hmac
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

from invoices import notify
from quota import db

bp = Blueprint("pohoda", __name__)

AGENT_TOKEN = os.environ.get("POHODA_AGENT_TOKEN", "")

# Když si agent položku vezme a do téhle doby nenahlásí výsledek,
# vrátí se do fronty (spadl, restart Windows, zavřená POHODA...).
CLAIM_TIMEOUT_MIN = 15

BATCH = 20


def require_agent(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        sent = request.headers.get("X-Agent-Token", "")
        if not AGENT_TOKEN or not hmac.compare_digest(sent, AGENT_TOKEN):
            return jsonify(error="forbidden"), 403
        return fn(*args, **kwargs)
    return wrapper


def _release_stale(cur):
    cur.execute(
        """UPDATE pending_invoices
           SET pohoda_status = 'queued'
           WHERE pohoda_status = 'claimed'
             AND claimed_at < now() - %s * INTERVAL '1 minute'""",
        (CLAIM_TIMEOUT_MIN,),
    )


# ─────────────────────────────────────────────────────────────────────
# 1) Co je k vystavení
# ─────────────────────────────────────────────────────────────────────
@bp.get("/api/pohoda/queue")
@require_agent
def pohoda_queue():
    with db() as cur:
        _release_stale(cur)

        cur.execute(
            """UPDATE pending_invoices SET pohoda_status = 'claimed', claimed_at = now()
               WHERE id IN (
                 SELECT id FROM pending_invoices
                 WHERE pohoda_status = 'queued' AND status <> 'cancelled'
                   AND pohoda_attempts < 5
                 ORDER BY created_at
                 LIMIT %s FOR UPDATE SKIP LOCKED
               )
               RETURNING id, ref, email, company, vat_id, address, country,
                         kind, description, amount_cents, currency,
                         payment_method, created_at""",
            (BATCH,),
        )
        invoices = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """UPDATE stripe_payouts SET pohoda_status = 'claimed'
               WHERE id IN (
                 SELECT id FROM stripe_payouts
                 WHERE pohoda_status = 'queued'
                 ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED
               )
               RETURNING id, stripe_payout_id, amount_cents, gross_cents,
                         fee_cents, currency, arrival_date""",
            (BATCH,),
        )
        payouts = [dict(r) for r in cur.fetchall()]

    for i in invoices:
        i["created_at"] = i["created_at"].isoformat()
        i["amount"] = i["amount_cents"] / 100
    for p in payouts:
        if p.get("arrival_date"):
            p["arrival_date"] = p["arrival_date"].isoformat()

    return jsonify(invoices=invoices, payouts=payouts,
                   server_time=datetime.now(timezone.utc).isoformat())


# ─────────────────────────────────────────────────────────────────────
# 2) Výsledek z POHODY
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/pohoda/result")
@require_agent
def pohoda_result():
    """
    { "invoices": [ {"id":1,"ok":true,"number":"250100279","vs":"250100279"},
                    {"id":2,"ok":false,"error":"..."} ],
      "payouts":  [ {"id":5,"ok":true,"number":"..."} ] }
    """
    data = request.get_json(silent=True) or {}
    done = failed = 0

    with db() as cur:
        for r in data.get("invoices", []):
            if r.get("ok"):
                cur.execute(
                    """UPDATE pending_invoices
                       SET pohoda_status = 'done', pohoda_number = %s, pohoda_vs = %s,
                           pohoda_synced_at = now(), pohoda_error = NULL
                       WHERE id = %s""",
                    (r.get("number"), r.get("vs"), r["id"]),
                )
                done += 1
            else:
                cur.execute(
                    """UPDATE pending_invoices
                       SET pohoda_status = CASE WHEN pohoda_attempts + 1 >= 5
                                                THEN 'failed' ELSE 'queued' END,
                           pohoda_attempts = pohoda_attempts + 1,
                           pohoda_error = %s
                       WHERE id = %s
                       RETURNING ref, pohoda_attempts""",
                    (str(r.get("error"))[:500], r["id"]),
                )
                row = cur.fetchone()
                failed += 1
                if row and row["pohoda_attempts"] >= 5:
                    notify(f"⚠️ POHODA: doklad <code>{row['ref']}</code> se nepodařilo "
                           f"vystavit ani na pátý pokus.\n<i>{str(r.get('error'))[:300]}</i>")

        for r in data.get("payouts", []):
            cur.execute(
                """UPDATE stripe_payouts
                   SET pohoda_status = %s, pohoda_number = %s, pohoda_error = %s
                   WHERE id = %s""",
                ("done" if r.get("ok") else "failed", r.get("number"),
                 None if r.get("ok") else str(r.get("error"))[:500], r["id"]),
            )

    return jsonify(ok=True, done=done, failed=failed)


# ─────────────────────────────────────────────────────────────────────
# 3) Heartbeat — ať víš, že agent běží
# ─────────────────────────────────────────────────────────────────────
_last_beat = {"at": None, "warned": False}


@bp.post("/api/pohoda/heartbeat")
@require_agent
def pohoda_heartbeat():
    _last_beat["at"] = datetime.now(timezone.utc)
    _last_beat["warned"] = False
    return jsonify(ok=True)


@bp.get("/api/pohoda/health")
def pohoda_health():
    """Bez tokenu — jen pro monitoring, nevrací nic citlivého."""
    at = _last_beat["at"]
    alive = bool(at and datetime.now(timezone.utc) - at < timedelta(minutes=10))
    with db() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pending_invoices WHERE pohoda_status = 'queued'")
        queued = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM pending_invoices WHERE pohoda_status = 'failed'")
        failed = cur.fetchone()["n"]
    return jsonify(agent_alive=alive,
                   last_seen=at.isoformat() if at else None,
                   queued=queued, failed=failed)

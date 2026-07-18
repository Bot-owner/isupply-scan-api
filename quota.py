"""
iSupply Scan — kvóty, fakturační okno a dokupované kredity.

Registrace v aplikaci:
    from quota import bp as quota_bp
    app.register_blueprint(quota_bp)

Endpointy:
    POST /api/scan/authorize   volá EXE PŘED skenem  -> povolit / 402
    POST /api/scan/complete    volá EXE PO skenu     -> uloží baseline komponent
    GET  /api/licence/status   zůstatek pro UI aplikace
    POST /api/credits/checkout vytvoří Stripe Checkout na balíček
    POST /api/stripe/webhook   připíše kredity, roluje období předplatného

ENV: DATABASE_URL, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, APP_BASE_URL
"""

import os
import re
from contextlib import contextmanager
from functools import wraps

import psycopg2
import psycopg2.extras
import stripe
from flask import Blueprint, jsonify, request

from provisioning import TIER_LIMITS, create_licence, send_licence_email

bp = Blueprint("quota", __name__)

DATABASE_URL = os.environ["DATABASE_URL"]
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
BASE_URL = os.environ.get("APP_BASE_URL", "https://isupply-scan.cz")

# Placený sken pokrývá opakované skeny téhož IMEI po tuto dobu.
FREE_RESCAN_DAYS = int(os.environ.get("FREE_RESCAN_DAYS", "7"))

# Kredity propadají po 12 měsících. None = nikdy nepropadnou.
CREDIT_VALIDITY_MONTHS = 12

# Jednorázové balíčky: velikost -> (Stripe Price ID, cena v centech)
CREDIT_PACKS = {
    100: (os.environ.get("STRIPE_PRICE_CREDITS_100", ""), 2300),
    200: (os.environ.get("STRIPE_PRICE_CREDITS_200", ""), 4500),
    500: (os.environ.get("STRIPE_PRICE_CREDITS_500", ""), 11000),
}

# ── Funkce podle tarifu ──────────────────────────────────────────────
# Basic nemá Excel import/export — odemyká se od Pro výš.
TIER_FEATURES = {
    "basic":      {"diagnostics", "label_print"},
    "pro":        {"diagnostics", "label_print", "excel_io"},
    "business":   {"diagnostics", "label_print", "excel_io", "priority_support"},
    "enterprise": {"diagnostics", "label_print", "excel_io", "priority_support", "api"},
}

# Kam poslat uživatele, když na funkci nemá nárok
FEATURE_MIN_TIER = {"excel_io": "pro", "api": "enterprise"}


def features_for(tier):
    return TIER_FEATURES.get((tier or "").lower(), TIER_FEATURES["basic"])


def has_feature(tier, feature):
    return feature in features_for(tier)

IMEI_RE = re.compile(r"^\d{14,17}$")


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                yield cur
    finally:
        conn.close()


def _locked_licence(cur, licence_key):
    """Načte licenci a zamkne řádek do konce transakce (proti souběžným skenům)."""
    cur.execute(
        """SELECT id, tier, status, scan_limit, unlimited, period_start, period_end
           FROM licences WHERE key = %s FOR UPDATE""",
        (licence_key,),
    )
    return cur.fetchone()


def _usage(cur, licence_id, period_start):
    cur.execute(
        """SELECT count(*) FROM scan_events
           WHERE licence_id = %s AND billed AND source = 'subscription'
             AND period_start = %s""",
        (licence_id, period_start),
    )
    return cur.fetchone()[0]


def _credits_left(cur, licence_id):
    cur.execute(
        """SELECT coalesce(sum(remaining), 0) FROM credit_packs
           WHERE licence_id = %s AND remaining > 0
             AND (expires_at IS NULL OR expires_at > now())""",
        (licence_id,),
    )
    return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────────────
# 1) Autorizace skenu
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/scan/authorize")
def authorize_scan():
    data = request.get_json(silent=True) or {}
    licence_key = (data.get("licence_key") or "").strip()
    imei = (data.get("imei") or "").strip()

    if not licence_key or not IMEI_RE.match(imei):
        return jsonify(error="bad_request", message="Chybí licence_key nebo platné IMEI."), 400

    with db() as cur:
        lic = _locked_licence(cur, licence_key)
        if not lic:
            return jsonify(error="licence_invalid"), 404
        if lic["status"] != "active":
            return jsonify(error="licence_inactive", status=lic["status"]), 403

        # ── a) Otevřené fakturační okno? Opakovaný sken je zdarma.
        cur.execute(
            """SELECT id, free_until FROM scan_events
               WHERE licence_id = %s AND imei = %s AND billed AND free_until > now()
               ORDER BY free_until DESC LIMIT 1""",
            (lic["id"], imei),
        )
        parent = cur.fetchone()
        if parent:
            cur.execute(
                """INSERT INTO scan_events
                     (licence_id, imei, billed, parent_id, period_start,
                      model, ios_version)
                   VALUES (%s, %s, FALSE, %s, %s, %s, %s) RETURNING id""",
                (lic["id"], imei, parent["id"], lic["period_start"],
                 data.get("model"), data.get("ios_version")),
            )
            return jsonify(
                allowed=True, billed=False, scan_event_id=cur.fetchone()[0],
                reason="free_rescan_window",
                free_until=parent["free_until"].isoformat(),
            )

        # ── b) Enterprise bez limitu
        if lic["unlimited"]:
            source, pack_id = "subscription", None

        else:
            used = _usage(cur, lic["id"], lic["period_start"])
            if used < lic["scan_limit"]:
                source, pack_id = "subscription", None
            else:
                # ── c) Čerpání z dokoupených kreditů (nejdřív ty, co dřív propadnou)
                cur.execute(
                    """SELECT id FROM credit_packs
                       WHERE licence_id = %s AND remaining > 0
                         AND (expires_at IS NULL OR expires_at > now())
                       ORDER BY expires_at NULLS LAST, created_at
                       LIMIT 1 FOR UPDATE SKIP LOCKED""",
                    (lic["id"],),
                )
                pack = cur.fetchone()
                if not pack:
                    # ── d) Došlo všechno → tvrdý stop
                    return jsonify(
                        allowed=False,
                        error="quota_exceeded",
                        tier=lic["tier"],
                        scan_limit=lic["scan_limit"],
                        used=used,
                        period_end=lic["period_end"].isoformat(),
                        upgrade_url=f"{BASE_URL}/#pricing",
                        topup_url=f"{BASE_URL}/credits",
                        packs=[{"credits": c, "price_eur": p / 100}
                               for c, (_, p) in sorted(CREDIT_PACKS.items())],
                    ), 402
                cur.execute(
                    "UPDATE credit_packs SET remaining = remaining - 1 WHERE id = %s",
                    (pack["id"],),
                )
                source, pack_id = "credit_pack", pack["id"]

        cur.execute(
            """INSERT INTO scan_events
                 (licence_id, imei, billed, free_until, source, credit_pack_id,
                  period_start, model, ios_version)
               VALUES (%s, %s, TRUE, now() + %s * INTERVAL '1 day', %s, %s, %s, %s, %s)
               RETURNING id, free_until""",
            (lic["id"], imei, FREE_RESCAN_DAYS, source, pack_id,
             lic["period_start"], data.get("model"), data.get("ios_version")),
        )
        ev = cur.fetchone()

        used_now = _usage(cur, lic["id"], lic["period_start"])
        return jsonify(
            allowed=True, billed=True, scan_event_id=ev["id"], source=source,
            free_until=ev["free_until"].isoformat(),
            subscription_remaining=(None if lic["unlimited"]
                                    else max(0, lic["scan_limit"] - used_now)),
            credits_remaining=_credits_left(cur, lic["id"]),
        )


# ─────────────────────────────────────────────────────────────────────
# 2) Uložení výsledku — trvalá baseline komponent
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/scan/complete")
def complete_scan():
    """
    Payload:
      { "licence_key": "...", "imei": "...", "scan_event_id": 123,
        "device": {"model": "iPhone 15 Pro", "serial_number": "...", ...},
        "components": [
          {"component":"battery","serial":"F5N...","source_path":"IOService:/...",
           "source_key":"BatterySerialNumber","is_factory":false}
        ] }
    """
    data = request.get_json(silent=True) or {}
    imei = (data.get("imei") or "").strip()
    if not IMEI_RE.match(imei):
        return jsonify(error="bad_request", message="Neplatné IMEI."), 400

    dev = data.get("device") or {}
    components = data.get("components") or []
    results = []

    with db() as cur:
        cur.execute(
            """INSERT INTO devices (imei, serial_number, model, model_identifier,
                                    capacity_gb, color, scan_count)
               VALUES (%s, %s, %s, %s, %s, %s, 1)
               ON CONFLICT (imei) DO UPDATE
                 SET last_seen_at = now(),
                     scan_count   = devices.scan_count + 1,
                     model        = coalesce(EXCLUDED.model, devices.model),
                     serial_number = coalesce(EXCLUDED.serial_number, devices.serial_number)""",
            (imei, dev.get("serial_number"), dev.get("model"),
             dev.get("model_identifier"), dev.get("capacity_gb"), dev.get("color")),
        )

        for c in components:
            name = (c.get("component") or "").strip()
            serial = c.get("serial")
            if not name:
                continue
            is_factory = bool(c.get("is_factory"))

            cur.execute(
                """SELECT serial FROM device_components
                   WHERE imei = %s AND component = %s AND is_factory = %s""",
                (imei, name, is_factory),
            )
            row = cur.fetchone()
            old = row["serial"] if row else None

            if old is None:
                verdict = "FIRST_SEEN"
            elif serial is None:
                verdict = "POSSIBLE_FAULT"
            elif old == serial:
                verdict = "MATCH"
            else:
                verdict = "MISMATCH"

            # Baseline se NIKDY nepřepisuje novou hodnotou — jen se osvěží last_seen_at.
            cur.execute(
                """INSERT INTO device_components
                     (imei, component, is_factory, serial, source_path, source_key, ios_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (imei, component, is_factory) DO UPDATE
                     SET last_seen_at = now()""",
                (imei, name, is_factory, serial, c.get("source_path"),
                 c.get("source_key"), dev.get("ios_version")),
            )

            if verdict != "MATCH":
                cur.execute(
                    """INSERT INTO device_component_history
                         (imei, component, old_serial, new_serial, verdict,
                          scan_event_id, source_path, source_key)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (imei, name, old, serial, verdict, data.get("scan_event_id"),
                     c.get("source_path"), c.get("source_key")),
                )

            results.append({"component": name, "verdict": verdict,
                            "baseline_serial": old, "scanned_serial": serial})

        if data.get("scan_event_id") and data.get("grade"):
            cur.execute("UPDATE scan_events SET grade = %s WHERE id = %s",
                        (data["grade"], data["scan_event_id"]))

    return jsonify(ok=True, imei=imei, components=results)


# ─────────────────────────────────────────────────────────────────────
# 3) Zůstatek pro UI aplikace
# ─────────────────────────────────────────────────────────────────────
@bp.get("/api/licence/status")
def licence_status():
    licence_key = (request.args.get("licence_key") or "").strip()
    if not licence_key:
        return jsonify(error="bad_request"), 400

    with db() as cur:
        cur.execute(
            """SELECT id, tier, status, scan_limit, unlimited, period_start, period_end
               FROM licences WHERE key = %s""",
            (licence_key,),
        )
        lic = cur.fetchone()
        if not lic:
            return jsonify(error="licence_invalid"), 404

        used = _usage(cur, lic["id"], lic["period_start"])
        return jsonify(
            tier=lic["tier"], status=lic["status"], unlimited=lic["unlimited"],
            scan_limit=lic["scan_limit"], used=used,
            subscription_remaining=(None if lic["unlimited"]
                                    else max(0, lic["scan_limit"] - used)),
            credits_remaining=_credits_left(cur, lic["id"]),
            period_end=lic["period_end"].isoformat(),
            free_rescan_days=FREE_RESCAN_DAYS,
            features=sorted(features_for(lic["tier"])),
        )


# ─────────────────────────────────────────────────────────────────────
# 4b) Zámek funkcí podle tarifu
# ─────────────────────────────────────────────────────────────────────
def require_feature(feature):
    """
    Dekorátor pro endpointy vázané na tarif. Licenční klíč se bere
    z JSON těla, query stringu nebo hlavičky X-Licence-Key.

        @bp.post("/api/export/xlsx")
        @require_feature("excel_io")
        def export_xlsx(licence):
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            body = request.get_json(silent=True) or {}
            licence_key = (body.get("licence_key")
                           or request.args.get("licence_key")
                           or request.headers.get("X-Licence-Key")
                           or "").strip()
            if not licence_key:
                return jsonify(error="bad_request", message="Chybí licence_key."), 400

            with db() as cur:
                cur.execute(
                    "SELECT id, tier, status FROM licences WHERE key = %s",
                    (licence_key,),
                )
                lic = cur.fetchone()

            if not lic:
                return jsonify(error="licence_invalid"), 404
            if lic["status"] != "active":
                return jsonify(error="licence_inactive", status=lic["status"]), 403
            if not has_feature(lic["tier"], feature):
                return jsonify(
                    error="feature_locked",
                    feature=feature,
                    tier=lic["tier"],
                    required_tier=FEATURE_MIN_TIER.get(feature, "pro"),
                    message="Tato funkce je dostupná od tarifu "
                            f"{FEATURE_MIN_TIER.get(feature, 'pro').upper()}.",
                    upgrade_url=f"{BASE_URL}/#pricing",
                ), 403

            return fn(*args, licence=dict(lic), **kwargs)
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────
# 4) Nákup balíčku kreditů
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/credits/checkout")
def credits_checkout():
    data = request.get_json(silent=True) or {}
    licence_key = (data.get("licence_key") or "").strip()
    try:
        credits = int(data.get("credits", 0))
    except (TypeError, ValueError):
        credits = 0

    if credits not in CREDIT_PACKS:
        return jsonify(error="bad_request",
                       message=f"Dostupné balíčky: {sorted(CREDIT_PACKS)}"), 400

    with db() as cur:
        cur.execute("SELECT id FROM licences WHERE key = %s", (licence_key,))
        if not cur.fetchone():
            return jsonify(error="licence_invalid"), 404

    price_id, _ = CREDIT_PACKS[credits]
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={"licence_key": licence_key, "credits": str(credits)},
        payment_intent_data={"metadata": {"licence_key": licence_key,
                                          "credits": str(credits)}},
        success_url=f"{BASE_URL}/credits/ok?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/credits",
        automatic_tax={"enabled": True},
        tax_id_collection={"enabled": True},   # B2B: sběr DIČ pro reverse charge
    )
    return jsonify(checkout_url=session.url, session_id=session.id)


# ─────────────────────────────────────────────────────────────────────
# 5) Stripe webhook
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/stripe/webhook")
def stripe_webhook():
    try:
        event = stripe.Webhook.construct_event(
            request.data, request.headers.get("Stripe-Signature", ""), WEBHOOK_SECRET
        )
    except Exception as exc:
        return jsonify(error="invalid_signature", detail=str(exc)), 400

    obj = event["data"]["object"]

    # ── Nové předplatné → vygenerovat licenci a poslat klíč e-mailem
    if event["type"] == "checkout.session.completed" and obj.get("mode") == "subscription":
        meta = obj.get("metadata") or {}
        email = ((obj.get("customer_details") or {}).get("email")
                 or obj.get("customer_email"))
        tier = (meta.get("tier") or "basic").lower()
        sub_id = obj.get("subscription")

        if email and sub_id:
            with db() as cur:
                # idempotence: webhook může dorazit vícekrát
                cur.execute(
                    "SELECT id, key, tier, scan_limit FROM licences WHERE stripe_subscription_id = %s",
                    (sub_id,),
                )
                existing = cur.fetchone()

                if not existing:
                    sub = stripe.Subscription.retrieve(sub_id)
                    cust = (obj.get("customer_details") or {})
                    lic = create_licence(
                        cur,
                        email=email,
                        tier=tier,
                        stripe_customer_id=obj.get("customer"),
                        stripe_subscription_id=sub_id,
                        period_start=sub["current_period_start"],
                        period_end=sub["current_period_end"],
                        company=cust.get("name"),
                        vat_id=((obj.get("customer_tax_ids") or [{}])[0] or {}).get("value"),
                    )
                    key, limit = lic["key"], TIER_LIMITS.get(tier, 200)
                else:
                    key, limit = existing["key"], existing["scan_limit"]

            if not existing:
                lang = "cs" if (obj.get("locale") == "cs"
                                or ((obj.get("customer_details") or {})
                                    .get("address") or {}).get("country") == "CZ") else "en"
                try:
                    send_licence_email(email, key, tier, limit, lang)
                except Exception as exc:            # e-mail nesmí shodit webhook
                    print(f"[provisioning] odeslání klíče selhalo pro {email}: {exc}", flush=True)

    # ── Doplacení kreditů
    elif event["type"] == "checkout.session.completed" and obj.get("mode") == "payment":
        meta = obj.get("metadata") or {}
        licence_key = meta.get("licence_key")
        credits = int(meta.get("credits", 0))
        if licence_key and credits:
            expires = ("now() + INTERVAL '%d months'" % CREDIT_VALIDITY_MONTHS
                       if CREDIT_VALIDITY_MONTHS else "NULL")
            with db() as cur:
                cur.execute("SELECT id FROM licences WHERE key = %s", (licence_key,))
                lic = cur.fetchone()
                if lic:
                    cur.execute(
                        f"""INSERT INTO credit_packs
                              (licence_id, amount, remaining, price_cents, currency,
                               stripe_payment_intent, stripe_session_id, expires_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, {expires})
                            ON CONFLICT (stripe_session_id) DO NOTHING""",
                        (lic["id"], credits, credits, obj.get("amount_total"),
                         obj.get("currency", "eur"), obj.get("payment_intent"),
                         obj.get("id")),
                    )

    # ── Obnova předplatného = reset měsíční kvóty
    elif event["type"] == "invoice.paid":
        sub_id = obj.get("subscription")
        period = (obj.get("lines", {}).get("data") or [{}])[0].get("period") or {}
        if sub_id and period.get("start") and period.get("end"):
            with db() as cur:
                cur.execute(
                    """UPDATE licences
                       SET period_start = to_timestamp(%s),
                           period_end   = to_timestamp(%s),
                           status       = 'active'
                       WHERE stripe_subscription_id = %s""",
                    (period["start"], period["end"], sub_id),
                )

    # ── Neplatič / zrušení
    elif event["type"] in ("customer.subscription.deleted",
                           "invoice.payment_failed"):
        sub_id = obj.get("id") if event["type"].startswith("customer") else obj.get("subscription")
        if sub_id:
            with db() as cur:
                cur.execute(
                    "UPDATE licences SET status = 'suspended' WHERE stripe_subscription_id = %s",
                    (sub_id,),
                )

    return jsonify(received=True)

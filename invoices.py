"""
iSupply Scan — doručení ručně vystavené faktury přes Telegram.

Tok:
  1. Přijde platba  → webhook založí `pending_invoices` a pošle ti zprávu do Telegramu
                      s údaji pro Pohodu (firma, DIČ, částka, popis).
  2. Vystavíš fakturu v Pohodě a PDF pošleš botovi jako ODPOVĚĎ na tu zprávu.
  3. Bot fakturu stáhne, odešle zákazníkovi e-mailem v příloze a potvrdí ti to.

Přiřazení funguje dvěma způsoby:
  · odpověď (reply) na oznamovací zprávu  ← doporučeno
  · popisek u souboru obsahující značku, např. "OBJ-4F2A9C"

Příkazy: /pending — seznam nevyřízených, /cancel OBJ-XXXX — zrušení položky.

ENV: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (kam chodí oznámení; víc oddělených čárkou),
     TELEGRAM_WEBHOOK_SECRET, RESEND_API_KEY, MAIL_FROM, APP_BASE_URL
"""

import base64
import os
import secrets

import requests
from flask import Blueprint, jsonify, request

from provisioning import BASE_URL

bp = Blueprint("invoices", __name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
ALLOWED_CHATS = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

MAX_ATTACHMENT_MB = 20  # limit Telegram getFile


# ─────────────────────────────────────────────────────────────────────
# Telegram helpery
# ─────────────────────────────────────────────────────────────────────
def tg(method, **payload):
    r = requests.post(f"{API}/{method}", json=payload, timeout=15)
    r.raise_for_status()
    return r.json().get("result")


def notify(text, chat_id=None, reply_markup=None):
    """Pošle zprávu do všech povolených chatů, vrátí message_id té první."""
    first = None
    targets = [chat_id] if chat_id else sorted(ALLOWED_CHATS)
    for cid in targets:
        try:
            res = tg("sendMessage", chat_id=cid, text=text,
                     parse_mode="HTML", disable_web_page_preview=True,
                     **({"reply_markup": reply_markup} if reply_markup else {}))
            if first is None:
                first = res.get("message_id")
        except Exception as exc:
            print(f"[telegram] odeslání do {cid} selhalo: {exc}", flush=True)
    return first


def make_ref():
    return "OBJ-" + secrets.token_hex(3).upper()


def money(cents, currency):
    return f"{cents / 100:.2f} {currency.upper()}"


# ─────────────────────────────────────────────────────────────────────
# 1) Založení položky po platbě  (volá se ze Stripe webhooku)
# ─────────────────────────────────────────────────────────────────────
def queue_invoice(cur, *, email, kind, description, amount_cents, currency="eur",
                  license_id=None, company=None, vat_id=None, address=None,
                  country=None, stripe_session_id=None, stripe_payment_intent=None):
    """Založí položku ve frontě a upozorní tě v Telegramu. Idempotentní přes session id."""
    if stripe_session_id:
        cur.execute("SELECT id FROM pending_invoices WHERE stripe_session_id = %s",
                    (stripe_session_id,))
        if cur.fetchone():
            return None

    ref = make_ref()
    cur.execute(
        """INSERT INTO pending_invoices
             (ref, license_id, email, company, vat_id, address, country,
              kind, description, amount_cents, currency,
              stripe_session_id, stripe_payment_intent)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id, ref""",
        (ref, license_id, email, company, vat_id, address, country,
         kind, description, amount_cents, currency,
         stripe_session_id, stripe_payment_intent),
    )
    row = cur.fetchone()

    vat_line = f"\n<b>DIČ:</b> <code>{vat_id}</code>" if vat_id else ""
    reverse_charge = (vat_id and country and country != "CZ")
    tax_note = ("\n⚠️ <i>Reverse charge — bez DPH</i>" if reverse_charge
                else "\n<i>S DPH 21 %</i>" if country == "CZ"
                else "")

    msg = (
        f"💰 <b>Nová platba — {money(amount_cents, currency)}</b>\n"
        f"<code>{ref}</code>\n\n"
        f"<b>Firma:</b> {company or '—'}"
        f"{vat_line}\n"
        f"<b>E-mail:</b> {email}\n"
        f"<b>Adresa:</b> {address or '—'}"
        f"{(' · ' + country) if country else ''}\n"
        f"<b>Položka:</b> {description}"
        f"{tax_note}\n\n"
        f"📎 <b>Odpověz na tuhle zprávu s PDF fakturou</b> a odejde zákazníkovi na e-mail."
    )
    message_id = notify(msg)
    if message_id:
        cur.execute("UPDATE pending_invoices SET telegram_message_id = %s WHERE id = %s",
                    (message_id, row["id"]))
    return row["ref"]


# ─────────────────────────────────────────────────────────────────────
# 2) E-mail s fakturou v příloze
# ─────────────────────────────────────────────────────────────────────
def invoice_email_html(inv, lang="cs"):
    if lang == "cs":
        t = dict(h="Faktura k tvé objednávce",
                 p="V příloze posíláme fakturu k platbě, která nám dorazila. "
                   "Děkujeme za důvěru.",
                 item="Položka", amount="Částka", ref="Značka",
                 help="Pokud na dokladu něco nesedí, stačí odpovědět na tento e-mail.",
                 faq="nápověda")
    else:
        t = dict(h="Invoice for your order",
                 p="Attached you'll find the invoice for the payment we received. "
                   "Thank you for your business.",
                 item="Item", amount="Amount", ref="Reference",
                 help="If anything on the document looks wrong, just reply to this email.",
                 faq="help pages")

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f6">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f6;padding:32px 12px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden;border:1px solid #e8e8ea">

  <tr><td style="background:#0a0a0c;padding:26px 32px">
    <span style="font:800 20px/1 Helvetica,Arial,sans-serif;color:#fff">iSupply</span>
    <span style="font:800 20px/1 Helvetica,Arial,sans-serif;color:#FB91D1"> Scan</span>
  </td></tr>

  <tr><td style="padding:32px">
    <h1 style="margin:0 0 14px;font:700 21px/1.3 Helvetica,Arial,sans-serif;color:#0a0a0c">{t['h']}</h1>
    <p style="margin:0 0 24px;font:400 15px/1.6 Helvetica,Arial,sans-serif;color:#3f3f46">{t['p']}</p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid #e8e8ea;border-radius:12px;padding:6px 4px">
      <tr>
        <td style="padding:11px 16px;font:400 13px/1.4 Helvetica,Arial,sans-serif;color:#71717a">{t['item']}</td>
        <td style="padding:11px 16px;font:600 14px/1.4 Helvetica,Arial,sans-serif;color:#0a0a0c;text-align:right">{inv['description']}</td>
      </tr>
      <tr>
        <td style="padding:11px 16px;font:400 13px/1.4 Helvetica,Arial,sans-serif;color:#71717a;border-top:1px solid #f0f0f2">{t['amount']}</td>
        <td style="padding:11px 16px;font:600 14px/1.4 Helvetica,Arial,sans-serif;color:#0a0a0c;text-align:right;border-top:1px solid #f0f0f2">{money(inv['amount_cents'], inv['currency'])}</td>
      </tr>
      <tr>
        <td style="padding:11px 16px;font:400 13px/1.4 Helvetica,Arial,sans-serif;color:#71717a;border-top:1px solid #f0f0f2">{t['ref']}</td>
        <td style="padding:11px 16px;font:600 13px/1.4 'SFMono-Regular',Consolas,monospace;color:#0a0a0c;text-align:right;border-top:1px solid #f0f0f2">{inv['ref']}</td>
      </tr>
    </table>

    <p style="margin:24px 0 0;font:400 13.5px/1.6 Helvetica,Arial,sans-serif;color:#71717a">{t['help']}</p>
  </td></tr>

  <tr><td style="padding:20px 32px 28px;border-top:1px solid #e8e8ea">
    <p style="margin:0;font:400 12.5px/1.6 Helvetica,Arial,sans-serif;color:#71717a">
      <a href="{BASE_URL}/support" style="color:#ec4899">{t['faq']}</a> ·
      iSupply trade s.r.o. · IČO 23199351 · DIČ CZ23199351<br>
      Křižíkova 177/29, Karlín, 186 00 Praha 8 · info@isupply.cz
    </p>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def send_invoice_email(inv, pdf_bytes, filename, lang="cs"):
    subject = (f"Faktura {inv['ref']} — iSupply Scan" if lang == "cs"
               else f"Invoice {inv['ref']} — iSupply Scan")
    return _send_with_attachment(
        inv["email"], subject, invoice_email_html(inv, lang), pdf_bytes, filename
    )


def _send_with_attachment(to, subject, html, pdf_bytes, filename):
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        raise RuntimeError("Chybí RESEND_API_KEY.")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": os.environ.get("MAIL_FROM", "iSupply Scan <noreply@isupply-scan.cz>"),
            "to": [to],
            "reply_to": os.environ.get("MAIL_REPLY_TO", "info@isupply.cz"),
            "subject": subject,
            "html": html,
            "attachments": [{
                "filename": filename,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
            }],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────
# 3) Webhook Telegramu
# ─────────────────────────────────────────────────────────────────────
@bp.post("/api/telegram/webhook")
def telegram_webhook():
    # Telegram posílá tajemství v hlavičce — bez něj endpoint nikoho neposlouchá
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return jsonify(error="forbidden"), 403

    from quota import db  # pozdní import kvůli cyklické závislosti

    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    if ALLOWED_CHATS and chat_id not in ALLOWED_CHATS:
        return jsonify(ok=True)  # cizí chat tiše ignorujeme

    text = (msg.get("text") or "").strip()

    # ── příkazy ──
    if text.startswith("/pending"):
        with db() as cur:
            cur.execute(
                """SELECT ref, company, email, amount_cents, currency, created_at
                   FROM pending_invoices WHERE status = 'waiting'
                   ORDER BY created_at LIMIT 20"""
            )
            rows = cur.fetchall()
        if not rows:
            notify("✅ Žádné faktury nečekají.", chat_id)
        else:
            lines = "\n".join(
                f"<code>{r['ref']}</code> · {money(r['amount_cents'], r['currency'])} · "
                f"{r['company'] or r['email']} · {r['created_at']:%d.%m.}"
                for r in rows
            )
            notify(f"📋 <b>Čeká na fakturu ({len(rows)}):</b>\n{lines}", chat_id)
        return jsonify(ok=True)

    if text.startswith("/cancel"):
        parts = text.split()
        if len(parts) < 2:
            notify("Použití: <code>/cancel OBJ-XXXXXX</code>", chat_id)
            return jsonify(ok=True)
        with db() as cur:
            cur.execute(
                """UPDATE pending_invoices SET status = 'cancelled'
                   WHERE ref = %s AND status = 'waiting' RETURNING ref""",
                (parts[1].strip().upper(),),
            )
            row = cur.fetchone()
        notify(f"🗑 Zrušeno: <code>{row['ref']}</code>" if row
               else "Nenašel jsem čekající položku s touhle značkou.", chat_id)
        return jsonify(ok=True)

    # ── příchozí dokument = faktura ──
    doc = msg.get("document")
    if not doc:
        return jsonify(ok=True)

    filename = doc.get("file_name") or "faktura.pdf"
    if not filename.lower().endswith(".pdf"):
        notify("❌ Přijímám jen PDF.", chat_id)
        return jsonify(ok=True)
    if (doc.get("file_size") or 0) > MAX_ATTACHMENT_MB * 1024 * 1024:
        notify(f"❌ Soubor je větší než {MAX_ATTACHMENT_MB} MB.", chat_id)
        return jsonify(ok=True)

    # přiřazení: odpověď na oznámení, jinak značka v popisku
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    caption = (msg.get("caption") or "").upper()

    with db() as cur:
        inv = None
        if reply_to:
            cur.execute(
                """SELECT * FROM pending_invoices
                   WHERE telegram_message_id = %s AND status = 'waiting'""",
                (reply_to,),
            )
            inv = cur.fetchone()
        if not inv and "OBJ-" in caption:
            ref = "OBJ-" + caption.split("OBJ-")[1][:6]
            cur.execute(
                "SELECT * FROM pending_invoices WHERE ref = %s AND status = 'waiting'",
                (ref,),
            )
            inv = cur.fetchone()

        if not inv:
            notify("❓ Nevím, ke které objednávce faktura patří.\n"
                   "Pošli ji jako <b>odpověď</b> na oznámení o platbě, nebo do popisku "
                   "napiš značku <code>OBJ-XXXXXX</code>. Seznam: /pending", chat_id)
            return jsonify(ok=True)

        inv = dict(inv)

        # stažení z Telegramu
        try:
            info = tg("getFile", file_id=doc["file_id"])
            fr = requests.get(f"{FILE_API}/{info['file_path']}", timeout=30)
            fr.raise_for_status()
            pdf_bytes = fr.content
        except Exception as exc:
            notify(f"❌ Nepodařilo se stáhnout soubor: {exc}", chat_id)
            return jsonify(ok=True)

        # odeslání zákazníkovi
        lang = "cs" if (inv.get("country") == "CZ") else "en"
        try:
            send_invoice_email(inv, pdf_bytes, filename, lang)
        except Exception as exc:
            notify(f"❌ E-mail se nepodařilo odeslat na {inv['email']}: {exc}\n"
                   f"Položka <code>{inv['ref']}</code> zůstává ve frontě, zkus to znovu.", chat_id)
            return jsonify(ok=True)

        cur.execute(
            """UPDATE pending_invoices
               SET status = 'sent', sent_at = now(), invoice_filename = %s
               WHERE id = %s""",
            (filename, inv["id"]),
        )

    notify(f"✅ Faktura <code>{inv['ref']}</code> odeslána na <b>{inv['email']}</b>\n"
           f"<i>{filename}</i>", chat_id)
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────
# 4) Jednorázová registrace webhooku u Telegramu
# ─────────────────────────────────────────────────────────────────────
def register_telegram_webhook():
    """Zavolej jednou po nasazení (např. z shellu na Railway)."""
    return tg("setWebhook",
              url=f"{BASE_URL}/api/telegram/webhook",
              secret_token=WEBHOOK_SECRET,
              allowed_updates=["message", "channel_post"])

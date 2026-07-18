"""
iSupply Scan — vygenerování licence po platbě a odeslání klíče e-mailem.

Volá se ze Stripe webhooku (checkout.session.completed, mode == 'subscription').
E-maily jdou přes Resend, protože Railway blokuje odchozí SMTP.

ENV: RESEND_API_KEY, MAIL_FROM (např. "iSupply Scan <noreply@isupply-scan.cz>"),
     APP_BASE_URL
"""

import os
import secrets
import string

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "iSupply Scan <noreply@isupply-scan.cz>")
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "info@isupply.cz")
BASE_URL = os.environ.get("APP_BASE_URL", "https://isupply-scan.cz")

# Bez znaků, které jdou splést: 0/O, 1/I/L
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

TIER_LIMITS = {"basic": 200, "pro": 500, "business": 1000, "enterprise": 0}


def generate_licence_key():
    """Formát ISPL-XXXX-XXXX-XXXX — čitelný do telefonu i přes zákaznickou linku."""
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3)]
    return "ISPL-" + "-".join(groups)


def create_licence(cur, email, tier, stripe_customer_id, stripe_subscription_id,
                   period_start, period_end, company=None, vat_id=None):
    """Vytvoří licenci s unikátním klíčem. `cur` je kurzor v probíhající transakci."""
    tier = (tier or "basic").lower()
    for _ in range(8):
        key = generate_licence_key()
        cur.execute("SELECT 1 FROM licences WHERE key = %s", (key,))
        if not cur.fetchone():
            break
    else:
        raise RuntimeError("Nepodařilo se vygenerovat unikátní licenční klíč.")

    cur.execute(
        """INSERT INTO licences
             (key, email, company, vat_id, tier, status, scan_limit, unlimited,
              stripe_customer_id, stripe_subscription_id, period_start, period_end)
           VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, %s,
                   to_timestamp(%s), to_timestamp(%s))
           RETURNING id, key""",
        (key, email, company, vat_id, tier,
         TIER_LIMITS.get(tier, 200), tier == "enterprise",
         stripe_customer_id, stripe_subscription_id, period_start, period_end),
    )
    return cur.fetchone()


# ─────────────────────────────────────────────────────────────────────
# E-mail
# ─────────────────────────────────────────────────────────────────────
def send_email(to, subject, html, text=None):
    if not RESEND_API_KEY:
        raise RuntimeError("Chybí RESEND_API_KEY.")
    payload = {
        "from": MAIL_FROM,
        "to": [to],
        "reply_to": MAIL_REPLY_TO,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def licence_email_html(key, tier, scan_limit, lang="cs"):
    tier_label = tier.capitalize()
    limit_label = "bez limitu" if not scan_limit else f"{scan_limit} skenů / měsíc"

    if lang == "cs":
        t = dict(
            subject_intro="Díky za nákup. Tvoje licence je aktivní.",
            key_label="Licenční klíč",
            steps_h="Jak to rozjet",
            s1="Stáhni si aplikaci pro Windows",
            s1b="jediný soubor, nic dalšího instalovat nemusíš",
            s2="Nainstaluj iTunes nebo Apple Devices",
            s2b="kvůli USB ovladačům; pokud chybí, aplikace nabídne stažení sama",
            s3="Spusť iSupply Scan a vlož klíč",
            s3b="klíč se ověří online a licence se aktivuje okamžitě",
            s4="Připoj iPhone přes USB a na telefonu potvrď „Trust“",
            s4b="výsledky naskakují průběžně, připojit jde až deset zařízení naráz",
            plan_h="Tvůj tarif",
            note="Sken téhož IMEI je následujících 7 dní zdarma — kus si můžeš během opravy přeměřit kolikrát potřebuješ.",
            help="Něco nefunguje? Odpověz rovnou na tento e-mail nebo se podívej do",
            faq="nápovědy",
            dl="Stáhnout pro Windows",
        )
    else:
        t = dict(
            subject_intro="Thanks for your purchase. Your licence is active.",
            key_label="Licence key",
            steps_h="Getting started",
            s1="Download the Windows app",
            s1b="a single file, nothing else to install",
            s2="Install iTunes or Apple Devices",
            s2b="needed for the USB drivers; the app offers a download if they're missing",
            s3="Launch iSupply Scan and paste your key",
            s3b="it's verified online and the licence activates immediately",
            s4="Connect an iPhone over USB and tap “Trust” on the phone",
            s4b="results appear live; up to ten devices can be connected at once",
            plan_h="Your plan",
            note="Re-scanning the same IMEI is free for the next 7 days — you can re-test a unit as often as you need during repair.",
            help="Something not working? Just reply to this email, or check the",
            faq="help pages",
            dl="Download for Windows",
        )

    steps = "".join(
        f'<tr><td style="padding:0 0 14px;vertical-align:top;width:30px">'
        f'<span style="display:inline-block;width:22px;height:22px;border-radius:6px;'
        f'background:#ec4899;color:#fff;font:600 12px/22px Helvetica,Arial,sans-serif;'
        f'text-align:center">{i}</span></td>'
        f'<td style="padding:0 0 14px;font:400 15px/1.5 Helvetica,Arial,sans-serif;color:#3f3f46">'
        f'<strong style="color:#0a0a0c">{a}</strong><br>'
        f'<span style="color:#71717a;font-size:14px">{b}</span></td></tr>'
        for i, (a, b) in enumerate(
            [(t["s1"], t["s1b"]), (t["s2"], t["s2b"]),
             (t["s3"], t["s3b"]), (t["s4"], t["s4b"])], start=1)
    )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f6">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f6;padding:32px 12px">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden;border:1px solid #e8e8ea">

  <tr><td style="background:#0a0a0c;padding:26px 32px">
    <span style="font:800 20px/1 Helvetica,Arial,sans-serif;color:#fff">iSupply</span>
    <span style="font:800 20px/1 Helvetica,Arial,sans-serif;color:#FB91D1"> Scan</span>
  </td></tr>

  <tr><td style="padding:32px 32px 8px">
    <p style="margin:0 0 22px;font:400 16px/1.55 Helvetica,Arial,sans-serif;color:#3f3f46">
      {t['subject_intro']}</p>

    <div style="border:1px solid #e8e8ea;border-radius:12px;padding:18px 20px;background:#fdf2f8">
      <div style="font:600 11px/1 Helvetica,Arial,sans-serif;letter-spacing:.12em;
                  text-transform:uppercase;color:#ec4899;margin-bottom:9px">{t['key_label']}</div>
      <div style="font:600 22px/1.3 'SFMono-Regular',Consolas,monospace;color:#0a0a0c;
                  letter-spacing:.06em;word-break:break-all">{key}</div>
    </div>

    <div style="margin:18px 0 26px;font:400 14px/1.55 Helvetica,Arial,sans-serif;color:#52525b">
      <strong style="color:#0a0a0c">{t['plan_h']}:</strong> {tier_label} · {limit_label}
    </div>

    <h2 style="margin:0 0 16px;font:700 17px/1.3 Helvetica,Arial,sans-serif;color:#0a0a0c">{t['steps_h']}</h2>
    <table cellpadding="0" cellspacing="0" width="100%">{steps}</table>

    <div style="text-align:center;margin:26px 0 8px">
      <a href="{BASE_URL}/#download"
         style="display:inline-block;background:#ec4899;color:#fff;text-decoration:none;
                font:600 15px/1 Helvetica,Arial,sans-serif;padding:14px 28px;border-radius:10px">
        {t['dl']}</a>
    </div>

    <p style="margin:22px 0 0;padding:14px 16px;background:#f5f5f6;border-radius:10px;
              font:400 13.5px/1.55 Helvetica,Arial,sans-serif;color:#52525b">{t['note']}</p>
  </td></tr>

  <tr><td style="padding:22px 32px 30px;border-top:1px solid #e8e8ea">
    <p style="margin:0;font:400 13px/1.6 Helvetica,Arial,sans-serif;color:#71717a">
      {t['help']} <a href="{BASE_URL}/support" style="color:#ec4899">{t['faq']}</a>.<br>
      iSupply trade s.r.o. · IČO 23199351 · info@isupply.cz
    </p>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def send_licence_email(email, key, tier, scan_limit, lang="cs"):
    subject = (f"Tvůj licenční klíč k iSupply Scan — {key}" if lang == "cs"
               else f"Your iSupply Scan licence key — {key}")
    text = (f"{key}\n\n{BASE_URL}/#download\n\n"
            f"Tarif: {tier} · {'bez limitu' if not scan_limit else str(scan_limit) + ' skenů/měsíc'}")
    return send_email(email, subject,
                      licence_email_html(key, tier, scan_limit, lang), text)

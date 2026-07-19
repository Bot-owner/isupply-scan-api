# Nasazení a ostrý test

Postup je chronologický. Nepřeskakuj kroky — každý další na tom předchozím staví.

---

## 1. Databáze

```bash
psql $DATABASE_URL -f 001_scan_quota.sql
psql $DATABASE_URL -f 002_pending_invoices.sql
psql $DATABASE_URL -f 003_pohoda.sql
```

Ověř, že sedí názvy sloupců v tabulce licencí — migrace předpokládá `licences`
se sloupci `key`, `tier`, `email`, `status`, `stripe_subscription_id`.
Pokud se liší, uprav migraci **před** spuštěním.

---

## 2. Stripe — produkty

V dashboardu založ:

| Produkt | Cena | Typ | Metadata |
|---|---|---|---|
| iSupply Scan Basic | 35 € / měsíc | recurring | `tier=basic` |
| iSupply Scan Pro | 49 € / měsíc | recurring | `tier=pro` |
| iSupply Scan Business | 79 € / měsíc | recurring | `tier=business` |
| **TEST — plný přístup** | **1 € / měsíc** | recurring | `tier=test` |
| Kredity 100 | 23 € | one-time | — |
| Kredity 200 | 45 € | one-time | — |
| Kredity 500 | 110 € | one-time | — |
| **TEST — 10 kreditů** | **1 €** | one-time | — |

⚠️ **Metadata `tier` musí sedět přesně.** Webhook podle nich přiděluje limit.
Pokud metadata chybí, licence se vydá jako `basic`.

⚠️ U testovacích produktů zaškrtni, že **nejsou** v Payment Links veřejně
dohledatelné, a odkaz nikam nedávej.

---

## 3. Proměnné prostředí na Railway

```
DATABASE_URL              (už máš)
STRIPE_SECRET_KEY         sk_live_…
STRIPE_WEBHOOK_SECRET     whsec_…            ← z kroku 4
STRIPE_PRICE_CREDITS_10   price_…            ← testovací
STRIPE_PRICE_CREDITS_100  price_…
STRIPE_PRICE_CREDITS_200  price_…
STRIPE_PRICE_CREDITS_500  price_…

RESEND_API_KEY            re_…
MAIL_FROM                 iSupply Scan <noreply@isupply-scan.cz>
MAIL_REPLY_TO             info@isupply.cz

TELEGRAM_BOT_TOKEN        …
TELEGRAM_CHAT_ID          tvoje chat id
TELEGRAM_WEBHOOK_SECRET   náhodný řetězec

POHODA_AGENT_TOKEN        dlouhý náhodný řetězec
APP_BASE_URL              https://isupply-scan.cz

TEST_ALLOWED_EMAILS       tvuj@email.cz      ← KRITICKÉ, viz níže
```

**`TEST_ALLOWED_EMAILS` je pojistka.** Testovací tarif za 1 € dostane jen
e-mail z tohoto seznamu. Komukoli jinému se licence vydá jako `basic`
a tobě přijde upozornění do Telegramu. Bez téhle proměnné by ti unikly
plné licence za euro.

---

## 4. Webhooky

**Stripe** → `https://isupply-scan.cz/api/stripe/webhook`
Události: `checkout.session.completed`, `invoice.paid`,
`invoice.payment_failed`, `customer.subscription.deleted`
Podpisový klíč vlož do `STRIPE_WEBHOOK_SECRET`.

**Telegram** — jednou po nasazení:
```bash
python -c "from invoices import register_telegram_webhook as r; print(r())"
```

---

## 5. Registrace blueprintů v `server.py`

```python
from quota import bp as quota_bp
from invoices import bp as invoices_bp
from pohoda import bp as pohoda_bp

app.register_blueprint(quota_bp)
app.register_blueprint(invoices_bp)
app.register_blueprint(pohoda_bp)
```

---

## 6. POHODA — příprava (ještě před agentem)

1. Založ **účet `STRIPE`** (typ banka / peníze na cestě)
2. Založ **číselnou řadu pro kartové platby** — oddělenou od té stávající `2501…`
3. Ověř, že máš zapnutý **mServer** a znáš port (výchozí 444)

---

## 7. Agent

Na PC s POHODOU:

```bash
copy agent.ini.example agent.ini
# vyplň token, číselnou řadu, účet STRIPE
python pohoda_agent.py --dry-run --once
```

Vznikne soubor `dryrun_OBJ-XXXXXX.xml`. **Naimportuj ho do POHODY ručně**
a zkontroluj:

- [ ] doklad spadl do správné číselné řady
- [ ] je označený jako uhrazený na účtu STRIPE
- [ ] u zákazníka z EU s DIČ je sazba DPH `none` a poznámka o reverse charge
- [ ] částka a měna sedí

Teprve když tohle sedí, pusť ostře: `python pohoda_agent.py`

---

## 8. Ostrý test — 1 €

Chronologicky, přesně tohle by se mělo stát:

1. Koupíš testovací předplatné za 1 € (e-mailem z `TEST_ALLOWED_EMAILS`)
2. **Do minuty** ti přijde e-mail s licenčním klíčem `ISPL-XXXX-XXXX-XXXX`
3. **Do Telegramu** dorazí zpráva s `OBJ-XXXXXX` a fakturačními údaji
4. **Do minuty** agent vystaví fakturu v POHODĚ, přidělí číslo
5. Fakturu z POHODY vytiskneš do PDF a pošleš botovi **jako odpověď** na tu zprávu
6. Na testovací e-mail dorazí zpráva s fakturou v příloze

Pak zkus:

7. Vlož klíč do aplikace → musí se aktivovat, Excel export musí být odemčený
8. Naskenuj iPhone → sken se odečte, `GET /api/licence/status` ukáže 49 z 50
9. **Naskenuj ten samý iPhone znovu** → nesmí se odečíst nic (7denní okno)
10. Koupíš testovací balíček 10 kreditů za 1 € → přičtou se, přijde druhá faktura
11. `/pending` v Telegramu → prázdný seznam

---

## 9. Po testu

- [ ] Smaž nebo deaktivuj oba testovací produkty ve Stripe
- [ ] Zruš testovací předplatné, ať se ti neúčtuje dál
- [ ] Zkontroluj, že testovací doklady v POHODĚ nedělají díru v číselné řadě
- [ ] `GET /api/pohoda/health` → `agent_alive: true`, `failed: 0`

---

## Kde hledat, když něco nedojde

| Problém | Kde se dívat |
|---|---|
| Nepřišel klíč | logy Railway, hledej `[provisioning]` |
| Nepřišla zpráva do Telegramu | `TELEGRAM_CHAT_ID`, logy `[telegram]` |
| Doklad není v POHODĚ | `agent.log` na PC, `GET /api/pohoda/health` |
| Bot nepoznal, kam faktura patří | poslal jsi ji jako **odpověď**? jinak `/pending` |
| Faktura v příloze nedorazila | ověřená doména v Resendu (SPF + DKIM) |

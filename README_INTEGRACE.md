# Napojení do server.py

```python
from quota import bp as quota_bp
from invoices import bp as invoices_bp

app.register_blueprint(quota_bp)
app.register_blueprint(invoices_bp)
```

## Migrace
```bash
psql $DATABASE_URL -f 001_scan_quota.sql
psql $DATABASE_URL -f 002_pending_invoices.sql
```

## Proměnné prostředí
| Proměnná | K čemu |
|---|---|
| `DATABASE_URL` | Postgres na Railway |
| `STRIPE_SECRET_KEY` | live klíč |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` ze Stripe dashboardu |
| `STRIPE_PRICE_CREDITS_100/200/500` | Price ID jednorázových balíčků |
| `RESEND_API_KEY` | odesílání e-mailů |
| `MAIL_FROM` | `iSupply Scan <noreply@isupply-scan.cz>` |
| `MAIL_REPLY_TO` | `info@isupply.cz` |
| `TELEGRAM_BOT_TOKEN` | token bota |
| `TELEGRAM_CHAT_ID` | tvoje chat ID (víc oddělených čárkou) |
| `TELEGRAM_WEBHOOK_SECRET` | libovolný náhodný řetězec |
| `APP_BASE_URL` | `https://isupply-scan.cz` |
| `FREE_RESCAN_DAYS` | volitelné, výchozí `7` |

## Jednorázová registrace Telegram webhooku
```bash
python -c "from invoices import register_telegram_webhook as r; print(r())"
```

## Stripe webhook
Endpoint: `https://isupply-scan.cz/api/stripe/webhook`
Události: `checkout.session.completed`, `invoice.paid`,
`invoice.payment_failed`, `customer.subscription.deleted`

## Kontrola po nasazení
1. Testovací platba v Stripe test módu → přijde e-mail s klíčem
2. Do Telegramu dorazí oznámení s `OBJ-XXXXXX`
3. Odpověz na zprávu libovolným PDF → zákazníkovi odejde e-mail s přílohou
4. `/pending` musí vrátit prázdný seznam

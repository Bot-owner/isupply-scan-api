"""
iSupply Scan – Licence API Server + Stripe Payments
=====================================================
Env proměnné na Railway:
  SECRET_KEY           = náhodný dlouhý řetězec
  ADMIN_PASSWORD       = heslo pro správu licencí
  DATABASE_URL         = PostgreSQL URL (Railway auto)
  STRIPE_SECRET_KEY    = sk_live_...
  STRIPE_PUBLISHABLE_KEY = pk_live_...
  STRIPE_WEBHOOK_SECRET  = whsec_... (z Stripe Dashboard → Webhooks)
  BASE_URL             = https://isupply-scan.cz
  MAIL_FROM            = info@isupply.cz
  SENDGRID_API_KEY     = SG.... (volitelné – pro emaily)
"""

import os
import hashlib
import datetime
import secrets
import jwt
import stripe
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

@app.before_request
def force_https():
    if not request.is_secure and request.headers.get("X-Forwarded-Proto", "http") != "https":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)

SECRET_KEY      = os.environ.get('SECRET_KEY', 'change-this-in-production')
ADMIN_PASS      = os.environ.get('ADMIN_PASSWORD', 'isupply-admin-2024')
DATABASE_URL    = os.environ.get('DATABASE_URL', '')
TOKEN_HOURS     = 24
BASE_URL        = os.environ.get('BASE_URL', 'https://isupply-scan.cz')

# Stripe
stripe.api_key  = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUB_KEY  = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WH_SEC   = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# Plány – ceny v centech EUR
PLANS = {
    'starter': {
        'name':        'Starter',
        'price_eur':   0,
        'devices':     20,
        'label':       '20 skenů / měsíc',
        'description': 'Zdarma – ideální pro vyzkoušení',
        'color':       '#6b7280',
    },
    'basic': {
        'name':        'Basic',
        'price_eur':   3500,   # €35.00
        'price_year':  31500,  # €315.00 (ušetří €105)
        'devices':     200,
        'label':       'do 200 skenů / měsíc',
        'description': 'Pro malé obchody a servisy',
        'color':       '#3b82f6',
    },
    'pro': {
        'name':        'Pro',
        'price_eur':   4900,   # €49.00
        'price_year':  44100,  # €441.00 (ušetří €147)
        'devices':     500,
        'label':       'do 500 skenů / měsíc',
        'description': 'Pro střední operace a výkupny',
        'color':       '#ec4899',
    },
    'business': {
        'name':        'Business',
        'price_eur':   7900,   # €79.00
        'price_year':  71100,  # €711.00 (ušetří €237)
        'devices':     1000,
        'label':       'do 1000 skenů / měsíc',
        'description': 'Pro velké refurbishery a velkoobchody',
        'color':       '#7c3aed',
    },
    'enterprise': {
        'name':        'Enterprise',
        'price_eur':   None,
        'devices':     None,
        'label':       'Neomezeno',
        'description': 'Individuální cena pro 1000+ ks/měsíc',
        'color':       '#0a0a0a',
    },
}


# ─── DB ─────────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS licenses (
            id              SERIAL PRIMARY KEY,
            license_key     TEXT    NOT NULL UNIQUE,
            email           TEXT    NOT NULL,
            company         TEXT    NOT NULL DEFAULT '',
            plan            TEXT    NOT NULL DEFAULT 'pro',
            seats           INTEGER NOT NULL DEFAULT 1,
            valid_until     DATE    NOT NULL,
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
            notes           TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS activations (
            id              SERIAL PRIMARY KEY,
            license_id      INTEGER NOT NULL REFERENCES licenses(id),
            hwid            TEXT    NOT NULL,
            hostname        TEXT    DEFAULT '',
            activated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
            last_seen       TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(license_id, hwid)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id              SERIAL PRIMARY KEY,
            license_key     TEXT    NOT NULL,
            hwid            TEXT    DEFAULT '',
            action          TEXT    NOT NULL,
            detail          TEXT    DEFAULT '',
            ip              TEXT    DEFAULT '',
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS stripe_orders (
            id              SERIAL PRIMARY KEY,
            session_id      TEXT    NOT NULL UNIQUE,
            email           TEXT    NOT NULL,
            plan            TEXT    NOT NULL,
            billing         TEXT    NOT NULL DEFAULT 'monthly',
            amount_eur      INTEGER NOT NULL,
            license_key     TEXT    DEFAULT '',
            status          TEXT    NOT NULL DEFAULT 'pending',
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        );
    ''')
    conn.commit()

    # Seed demo licence
    try:
        c.execute('''
            INSERT INTO licenses (license_key, email, company, plan, seats, valid_until, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (license_key) DO NOTHING
        ''', (
            'ISUP-DEMO-0000-0000',
            'demo@isupply.cz',
            'iSupply Demo',
            'trial',
            1,
            datetime.date.today() + datetime.timedelta(days=30),
            'Demo licence'
        ))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

    print("✓ DB inicializována")


def hash_hw(hwid: str) -> str:
    return hashlib.sha256(hwid.encode()).hexdigest()


def log_action(license_key, action, detail='', hwid='', ip=''):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            'INSERT INTO audit_log (license_key, hwid, action, detail, ip) VALUES (%s,%s,%s,%s,%s)',
            (license_key, hwid, action, detail, ip)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def generate_license_key():
    return 'ISUP-' + '-'.join(secrets.token_hex(2).upper() for _ in range(3))


def create_license_for_plan(email, plan, billing='monthly'):
    """Vytvoří licenci v DB po úspěšné platbě."""
    plan_info = PLANS.get(plan, PLANS['basic'])
    seats = plan_info['devices'] or 9999

    if billing == 'yearly':
        days = 366
    else:
        days = 32  # měsíc + buffer

    key = generate_license_key()
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO licenses (license_key, email, company, plan, seats, valid_until, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, license_key
        ''', (
            key,
            email,
            '',
            plan,
            seats,
            datetime.date.today() + datetime.timedelta(days=days),
            f'Auto-created via Stripe | billing={billing}',
        ))
        row = c.fetchone()
        conn.commit()
        conn.close()
        log_action(key, 'STRIPE_CREATED', f'plan={plan} billing={billing} email={email}')
        return key
    except Exception as e:
        conn.rollback()
        conn.close()
        raise e


def send_license_email(email, license_key, plan, billing):
    """Odešle licenční klíč emailem přes SendGrid (pokud je nakonfigurován)."""
    sendgrid_key = os.environ.get('SENDGRID_API_KEY', '')
    if not sendgrid_key:
        print(f"[EMAIL] Would send license {license_key} to {email} (no SendGrid configured)")
        return

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail

        plan_info = PLANS.get(plan, {})
        plan_name = plan_info.get('name', plan)

        message = Mail(
            from_email=os.environ.get('MAIL_FROM', 'info@isupply.cz'),
            to_emails=email,
            subject=f'iSupply Scan – váš licenční klíč ({plan_name})',
            html_content=f'''
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px">
    <span style="font-size:24px;font-weight:900">iSupply<span style="color:#ec4899"> Scan</span></span>
  </div>
  <h2 style="font-size:20px;margin-bottom:8px">Děkujeme za nákup!</h2>
  <p style="color:#6b7280;margin-bottom:24px">Váš licenční klíč pro plán <strong>{plan_name}</strong>:</p>
  <div style="background:#f9f9f9;border:2px solid #ec4899;border-radius:10px;padding:20px;text-align:center;margin-bottom:24px">
    <div style="font-family:monospace;font-size:22px;font-weight:700;letter-spacing:0.1em;color:#0a0a0a">{license_key}</div>
  </div>
  <p style="color:#6b7280;font-size:13px">Klíč zadejte při prvním spuštění aplikace iSupply Scan v sekci <strong>Activate licence</strong>.</p>
  <p style="color:#6b7280;font-size:13px">Stáhněte aplikaci na: <a href="{BASE_URL}" style="color:#ec4899">{BASE_URL}</a></p>
  <hr style="border:none;border-top:1px solid #e8e8e8;margin:24px 0">
  <p style="color:#9ca3af;font-size:11px">V případě problémů nás kontaktujte na info@isupply.cz</p>
</div>
            '''
        )
        sg = sendgrid.SendGridAPIClient(sendgrid_key)
        sg.send(message)
        print(f"[EMAIL] License sent to {email}")
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def make_token(license_key: str, plan: str, valid_until: str) -> str:
    payload = {
        'license_key': license_key,
        'plan':        plan,
        'valid_until': str(valid_until),
        'exp':         datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_HOURS),
        'iat':         datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    except Exception:
        return None


# ─── STATIC ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    from flask import send_from_directory
    return send_from_directory('.', 'index.html')

@app.route('/admin-panel')
def admin_panel():
    from flask import send_from_directory
    return send_from_directory('.', 'admin.html')

@app.route('/photo_2026-07-01_01-43-29.jpg')
def serve_logo():
    from flask import send_from_directory
    return send_from_directory('.', 'photo_2026-07-01_01-43-29.jpg')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'version': '2.0', 'service': 'iSupply Scan API'})


# ─── STRIPE CHECKOUT ─────────────────────────────────────────────────────────

@app.route('/api/plans', methods=['GET'])
def get_plans():
    """Vrátí veřejné info o plánech (bez secret dat)."""
    return jsonify({'ok': True, 'plans': PLANS, 'pub_key': STRIPE_PUB_KEY})


@app.route('/api/checkout', methods=['POST'])
def create_checkout():
    """
    Vytvoří Stripe Checkout session.
    Tělo: { plan, billing, email }
    billing: 'monthly' | 'yearly'
    """
    data    = request.get_json() or {}
    plan    = data.get('plan', 'basic')
    billing = data.get('billing', 'monthly')
    email   = data.get('email', '')

    if plan == 'starter':
        # Free plán – rovnou vytvoř licenci
        key = create_license_for_plan(email, 'starter', 'free')
        send_license_email(email, key, 'starter', 'free')
        return jsonify({'ok': True, 'free': True, 'license_key': key})

    if plan == 'enterprise':
        return jsonify({'ok': False, 'error': 'Enterprise – kontaktujte info@isupply.cz'}), 400

    plan_info = PLANS.get(plan)
    if not plan_info:
        return jsonify({'ok': False, 'error': 'Unknown plan'}), 400

    price = plan_info['price_year'] if billing == 'yearly' else plan_info['price_eur']

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "link"],
            customer_email=email or None,
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'unit_amount': price,
                    'product_data': {
                        'name': f'iSupply Scan – {plan_info["name"]} ({billing})',
                        'description': plan_info['description'],
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f'{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{BASE_URL}/#pricing',
            metadata={
                'plan':    plan,
                'billing': billing,
                'email':   email,
            }
        )

        # Ulož pending order
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO stripe_orders (session_id, email, plan, billing, amount_eur)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
        ''', (session.id, email, plan, billing, price))
        conn.commit()
        conn.close()

        return jsonify({'ok': True, 'url': session.url})

    except stripe.error.StripeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/checkout/success', methods=['GET'])
def checkout_success():
    """Ověří session a vrátí licenční klíč."""
    session_id = request.args.get('session_id', '')
    if not session_id:
        return jsonify({'ok': False, 'error': 'Missing session_id'}), 400

    # Zkontroluj v DB
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM stripe_orders WHERE session_id=%s', (session_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        return jsonify({'ok': False, 'error': 'Order not found'}), 404

    if order['license_key']:
        return jsonify({'ok': True, 'license_key': order['license_key'], 'plan': order['plan']})

    return jsonify({'ok': False, 'error': 'Payment not yet confirmed', 'status': order['status']}), 202


@app.route('/api/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Stripe Webhook – zpracuje úspěšnou platbu a vytvoří licenci."""
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH_SEC)
    except Exception as e:
        print(f"[WEBHOOK] Invalid signature: {e}")
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'checkout.session.completed':
        session  = event['data']['object']
        sid      = session['id']
        email    = session.get('customer_email') or session.get('metadata', {}).get('email', '')
        plan     = session.get('metadata', {}).get('plan', 'basic')
        billing  = session.get('metadata', {}).get('billing', 'monthly')

        print(f"[WEBHOOK] Payment completed: {email} | {plan} | {billing}")

        # Vytvoř licenci
        try:
            key = create_license_for_plan(email, plan, billing)
            send_license_email(email, key, plan, billing)

            # Aktualizuj order
            conn = get_db()
            c = conn.cursor()
            c.execute(
                'UPDATE stripe_orders SET license_key=%s, status=%s WHERE session_id=%s',
                (key, 'completed', sid)
            )
            conn.commit()
            conn.close()
            print(f"[WEBHOOK] License created: {key} for {email}")
        except Exception as e:
            print(f"[WEBHOOK] Error creating license: {e}")
            return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True})


# ─── LICENCE API (původní) ───────────────────────────────────────────────────

@app.route('/api/validate', methods=['POST'])
def validate_license():
    data   = request.get_json() or {}
    key    = (data.get('license_key') or '').strip().upper()
    hwid   = (data.get('hwid') or '').strip()
    host   = (data.get('hostname') or '')[:64]
    ip     = request.headers.get('X-Forwarded-For', request.remote_addr)

    if not key or not hwid:
        return jsonify({'ok': False, 'error': 'Missing license_key or hwid'}), 400

    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM licenses WHERE license_key=%s', (key,))
    lic = c.fetchone()

    if not lic:
        conn.close()
        log_action(key, 'INVALID_KEY', ip=ip)
        return jsonify({'ok': False, 'error': 'Invalid license key'}), 401

    if not lic['active']:
        conn.close()
        log_action(key, 'SUSPENDED', ip=ip)
        return jsonify({'ok': False, 'error': 'License suspended. Contact support: info@isupply.cz'}), 403

    if lic['valid_until'] < datetime.date.today():
        conn.close()
        log_action(key, 'EXPIRED', ip=ip)
        return jsonify({'ok': False, 'error': f"License expired on {lic['valid_until']}. Renew at isupply-scan.cz"}), 403

    c.execute('SELECT * FROM activations WHERE license_id=%s', (lic['id'],))
    activations = c.fetchall()
    hwid_hash   = hash_hw(hwid)
    existing    = next((a for a in activations if a['hwid'] == hwid_hash), None)

    if existing:
        c.execute('UPDATE activations SET last_seen=NOW(), hostname=%s WHERE id=%s', (host, existing['id']))
        conn.commit()
    else:
        active_count = len(activations)
        if active_count >= lic['seats']:
            conn.close()
            log_action(key, 'SEATS_EXCEEDED', f"{active_count}/{lic['seats']}", hwid=hwid_hash, ip=ip)
            return jsonify({
                'ok':    False,
                'error': f"Seat limit reached ({active_count}/{lic['seats']}). "
                         f"Deactivate another device or upgrade at isupply-scan.cz"
            }), 403
        c.execute('INSERT INTO activations (license_id, hwid, hostname) VALUES (%s,%s,%s)',
                  (lic['id'], hwid_hash, host))
        conn.commit()
        log_action(key, 'NEW_ACTIVATION', host, hwid=hwid_hash, ip=ip)

    token = make_token(key, lic['plan'], lic['valid_until'])
    log_action(key, 'VALIDATED', host, hwid=hwid_hash, ip=ip)

    c.execute('SELECT COUNT(*) as cnt FROM activations WHERE license_id=%s', (lic['id'],))
    seats_used = c.fetchone()['cnt']
    conn.close()

    return jsonify({
        'ok':          True,
        'token':       token,
        'plan':        lic['plan'],
        'company':     lic['company'],
        'email':       lic['email'],
        'valid_until': str(lic['valid_until']),
        'seats_used':  seats_used,
        'seats_total': lic['seats'],
    })


@app.route('/api/activate', methods=['POST'])
def activate_license():
    """Alias pro /api/validate pro zpětnou kompatibilitu."""
    return validate_license()


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json() or {}
    key  = (data.get('license_key') or '').strip().upper()
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT id FROM licenses WHERE license_key=%s AND active=TRUE', (key,))
    lic = c.fetchone()
    conn.close()
    if not lic:
        return jsonify({'ok': False, 'error': 'License key not found or inactive'}), 404
    log_action(key, 'PASSWORD_RESET_REQUESTED')
    return jsonify({'ok': True, 'message': 'Password reset initiated. Check your email.'})


@app.route('/api/token/verify', methods=['POST'])
def verify_token_endpoint():
    data  = request.get_json() or {}
    token = data.get('token', '')
    payload = verify_token(token)
    if not payload:
        return jsonify({'ok': False, 'error': 'Invalid or expired token'}), 401
    return jsonify({'ok': True, **payload})


@app.route('/api/deactivate', methods=['POST'])
def deactivate_device():
    data = request.get_json() or {}
    key  = (data.get('license_key') or '').strip().upper()
    hwid = hash_hw((data.get('hwid') or '').strip())
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT id FROM licenses WHERE license_key=%s AND active=TRUE', (key,))
    lic = c.fetchone()
    if not lic:
        conn.close()
        return jsonify({'ok': False, 'error': 'License not found'}), 404
    c.execute('DELETE FROM activations WHERE license_id=%s AND hwid=%s', (lic['id'], hwid))
    conn.commit()
    conn.close()
    log_action(key, 'DEACTIVATED', hwid=hwid)
    return jsonify({'ok': True})


# ─── ADMIN API ───────────────────────────────────────────────────────────────

def require_admin():
    auth = request.headers.get('X-Admin-Password', '')
    return auth == ADMIN_PASS


@app.route('/api/admin/licenses', methods=['GET'])
def admin_list_licenses():
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('''SELECT l.*, COUNT(a.id) as seats_used FROM licenses l
                 LEFT JOIN activations a ON a.license_id = l.id
                 GROUP BY l.id ORDER BY l.created_at DESC''')
    rows = c.fetchall(); conn.close()
    return jsonify({'ok': True, 'licenses': [dict(r) for r in rows]})


@app.route('/api/admin/licenses', methods=['POST'])
def admin_create_license():
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    data = request.get_json() or {}
    key  = generate_license_key()
    conn = get_db(); c = conn.cursor()
    try:
        c.execute('''INSERT INTO licenses (license_key, email, company, plan, seats, valid_until, notes)
                     VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, license_key''', (
            key, data.get('email',''), data.get('company',''),
            data.get('plan','pro'), data.get('seats',1),
            data.get('valid_until', str(datetime.date.today() + datetime.timedelta(days=365))),
            data.get('notes',''),
        ))
        row = c.fetchone(); conn.commit(); conn.close()
        log_action(key, 'CREATED', data.get('company',''))
        return jsonify({'ok': True, 'id': row['id'], 'license_key': row['license_key']}), 201
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/licenses/<int:lid>', methods=['PUT'])
def admin_update_license(lid):
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    data = request.get_json() or {}
    conn = get_db(); c = conn.cursor()
    fields, vals = [], []
    for f in ['email','company','plan','notes']:
        if f in data: fields.append(f'{f}=%s'); vals.append(data[f])
    if 'seats'       in data: fields.append('seats=%s');       vals.append(int(data['seats']))
    if 'valid_until' in data: fields.append('valid_until=%s'); vals.append(data['valid_until'])
    if 'active'      in data: fields.append('active=%s');      vals.append(bool(data['active']))
    if fields:
        vals.append(lid)
        c.execute(f"UPDATE licenses SET {','.join(fields)} WHERE id=%s", vals)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/licenses/<int:lid>/activations', methods=['GET'])
def admin_list_activations(lid):
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM activations WHERE license_id=%s ORDER BY last_seen DESC', (lid,))
    rows = c.fetchall(); conn.close()
    return jsonify({'ok': True, 'activations': [dict(r) for r in rows]})


@app.route('/api/admin/activations/<int:aid>', methods=['DELETE'])
def admin_delete_activation(aid):
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('DELETE FROM activations WHERE id=%s', (aid,))
    conn.commit(); conn.close()
    log_action('ADMIN', 'DEVICE_REMOVED', f'activation_id={aid}')
    return jsonify({'ok': True})


@app.route('/api/admin/licenses/<int:lid>', methods=['DELETE'])
def admin_delete_license(lid):
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT license_key FROM licenses WHERE id=%s', (lid,))
    row = c.fetchone()
    if not row: conn.close(); return jsonify({'ok': False, 'error': 'License not found'}), 404
    key = row['license_key']
    c.execute('DELETE FROM activations WHERE license_id=%s', (lid,))
    c.execute('DELETE FROM licenses WHERE id=%s', (lid,))
    conn.commit(); conn.close()
    log_action(key, 'DELETED', 'admin deleted license')
    return jsonify({'ok': True})


@app.route('/api/admin/orders', methods=['GET'])
def admin_orders():
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM stripe_orders ORDER BY created_at DESC LIMIT 100')
    rows = c.fetchall(); conn.close()
    return jsonify({'ok': True, 'orders': [dict(r) for r in rows]})


@app.route('/api/admin/log', methods=['GET'])
def admin_log():
    if not require_admin(): return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 200')
    rows = c.fetchall(); conn.close()
    return jsonify({'ok': True, 'log': [dict(r) for r in rows]})


# ─── START ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if DATABASE_URL:
        init_db()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

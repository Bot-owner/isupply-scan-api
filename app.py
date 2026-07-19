"""
iSupply Scan – Licence API Server
===================================
Deploy na Railway:
1. Nahraj tuto složku na GitHub
2. Na railway.app vytvoř nový projekt z GitHubu
3. Přidej PostgreSQL databázi
4. Nastav env proměnné (viz níže)

Env proměnné které musíš nastavit na Railway:
  SECRET_KEY    = náhodný dlouhý řetězec (vygeneruj na random.org)
  ADMIN_PASSWORD = heslo pro správu licencí
"""

import os
import hmac
import hashlib
import datetime
import secrets
import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

# Absolutni cesta ke slozce s app.py (Railway/gunicorn neni v korenu repa)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))

SECRET_KEY    = os.environ.get('SECRET_KEY', 'change-this-in-production')

# BEZPECNOST: zadny vychozi fallback. Repo je verejne, takze hardcodovane
# heslo by znamenalo, ze admin API je otevrene komukoli. Kdyz promenna chybi,
# vygeneruje se nahodne heslo -> admin API je nepristupne, dokud ji nenastavis.
ADMIN_PASS = os.environ.get('ADMIN_PASSWORD')
if not ADMIN_PASS:
    ADMIN_PASS = secrets.token_urlsafe(32)
    print('[SECURITY] ADMIN_PASSWORD neni nastavene! Admin API je zamcene '
          'nahodnym heslem. Nastav promennou na Railway a redeployni.', flush=True)

DATABASE_URL  = os.environ.get('DATABASE_URL', '')
TOKEN_HOURS   = 24  # Token platný 24 hodin


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


# ─── PUBLIC API ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    from flask import send_from_directory
    return send_from_directory(_APP_DIR, 'index.html')

@app.route('/admin-panel')
def admin_panel():
    from flask import send_from_directory
    return send_from_directory(_APP_DIR, 'admin.html')

@app.route('/admin')
def admin_page_alias():
    from flask import send_from_directory
    return send_from_directory(_APP_DIR, 'admin.html')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'version': '1.0', 'service': 'iSupply Scan API'})


# Staticke soubory (loga, ikony, css, js) - jen bezpecne pripony, absolutni cesta.
@app.route('/<path:filename>')
def static_files(filename):
    from flask import send_from_directory, abort
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    if ext not in ('ico','png','jpg','jpeg','gif','svg','webp','css','js','woff','woff2','ttf','map','txt'):
        abort(404)
    try:
        return send_from_directory(_APP_DIR, filename)
    except Exception:
        abort(404)

@app.route('/favicon.ico')
def favicon():
    from flask import send_from_directory, abort
    for cand in ('favicon.ico','icon.ico'):
        if os.path.exists(os.path.join(_APP_DIR, cand)):
            return send_from_directory(_APP_DIR, cand)
    abort(404)

@app.route('/api/validate', methods=['POST'])
def validate_license():
    """
    Validace licence při spuštění aplikace.
    Tělo: { license_key, hwid, hostname }
    Odpověď: { ok, token, plan, valid_until, seats_used, seats_total }
    """
    data   = request.get_json() or {}
    key    = (data.get('license_key') or '').strip().upper()
    hwid   = (data.get('hwid') or '').strip()
    host   = (data.get('hostname') or '')[:64]
    ip     = request.headers.get('X-Forwarded-For', request.remote_addr)

    if not key or not hwid:
        return jsonify({'ok': False, 'error': 'Missing license_key or hwid'}), 400

    conn = get_db()
    c    = conn.cursor()

    # Načti licenci
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
        return jsonify({'ok': False, 'error': f"License expired on {lic['valid_until']}. Renew at isupply.cz"}), 403

    # Zkontroluj aktivace (seats)
    c.execute('SELECT * FROM activations WHERE license_id=%s', (lic['id'],))
    activations = c.fetchall()
    hwid_hash   = hash_hw(hwid)

    existing = next((a for a in activations if a['hwid'] == hwid_hash), None)

    if existing:
        # Zařízení už aktivováno – obnov last_seen
        c.execute('UPDATE activations SET last_seen=NOW(), hostname=%s WHERE id=%s', (host, existing['id']))
        conn.commit()
    else:
        # NEOMEZENY POCET PC. Ucetni jednotkou je sken, ne pocet pocitacu —
        # kdyz firma preda pristup dal, spotrebuje vic skenu a zaplati vic.
        # Zarizeni se proto jen evidují kvuli prehledu v admin panelu.
        active_count = len(activations)

        # Aktivuj nové zařízení
        c.execute(
            'INSERT INTO activations (license_id, hwid, hostname) VALUES (%s,%s,%s)',
            (lic['id'], hwid_hash, host)
        )
        conn.commit()
        active_count += 1
        log_action(key, 'NEW_ACTIVATION', host, hwid=hwid_hash, ip=ip)

    # Vygeneruj token
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
        'seats_total': 0,          # 0 = bez limitu
    })


@app.route('/api/token/verify', methods=['POST'])
def verify_token_endpoint():
    """Ověří offline token (pro případ bez internetu)."""
    data  = request.get_json() or {}
    token = data.get('token', '')
    payload = verify_token(token)
    if not payload:
        return jsonify({'ok': False, 'error': 'Invalid or expired token'}), 401
    return jsonify({'ok': True, **payload})


@app.route('/api/deactivate', methods=['POST'])
def deactivate_device():
    """Deaktivuje jedno zařízení ze slot."""
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
    # hmac.compare_digest = porovnani v konstantnim case (proti timing utoku)
    return bool(auth) and hmac.compare_digest(auth, ADMIN_PASS)


@app.route('/api/admin/licenses', methods=['GET'])
def admin_list_licenses():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db()
    c    = conn.cursor()
    # Ke kazde licenci dopocitame spotrebu skenu v aktualnim obdobi
    # a zustatek dokoupenych kreditu.
    c.execute('''
        SELECT l.*,
               COUNT(DISTINCT a.id) AS seats_used,
               COALESCE((
                   SELECT count(*) FROM scan_events se
                   WHERE se.license_id = l.id AND se.billed
                     AND se.source = 'subscription'
                     AND se.period_start = l.period_start
               ), 0) AS scans_used,
               COALESCE((
                   SELECT sum(cp.remaining) FROM credit_packs cp
                   WHERE cp.license_id = l.id AND cp.remaining > 0
                     AND (cp.expires_at IS NULL OR cp.expires_at > now())
               ), 0) AS credits_remaining,
               (SELECT max(a2.last_seen) FROM activations a2
                 WHERE a2.license_id = l.id) AS last_seen,
               COALESCE((
                   SELECT count(*) FROM activations a3
                   WHERE a3.license_id = l.id
                     AND a3.last_seen > now() - INTERVAL '3 minutes'
               ), 0) AS devices_online
        FROM licenses l
        LEFT JOIN activations a ON a.license_id = l.id
        GROUP BY l.id
        ORDER BY l.created_at DESC
    ''')
    rows = c.fetchall()
    conn.close()
    return jsonify({'ok': True, 'licenses': [dict(r) for r in rows]})


@app.route('/api/admin/licenses', methods=['POST'])
def admin_create_license():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    data = request.get_json() or {}

    # Vygeneruj unikátní klíč
    key = 'ISUP-' + '-'.join(
        secrets.token_hex(2).upper() for _ in range(3)
    )

    conn = get_db()
    c    = conn.cursor()
    try:
        c.execute('''
            INSERT INTO licenses (license_key, email, company, plan, seats, valid_until, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, license_key
        ''', (
            key,
            data.get('email', ''),
            data.get('company', ''),
            data.get('plan', 'pro'),
            data.get('seats', 1),
            data.get('valid_until', str(datetime.date.today() + datetime.timedelta(days=365))),
            data.get('notes', ''),
        ))
        row = c.fetchone()
        conn.commit()
        conn.close()
        log_action(key, 'CREATED', data.get('company', ''))
        return jsonify({'ok': True, 'id': row['id'], 'license_key': row['license_key']}), 201
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/admin/licenses/<int:lid>', methods=['PUT'])
def admin_update_license(lid):
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    data = request.get_json() or {}
    conn = get_db()
    c    = conn.cursor()
    fields, vals = [], []
    for f in ['email', 'company', 'plan', 'notes']:
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
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM activations WHERE license_id=%s ORDER BY last_seen DESC', (lid,))
    rows = c.fetchall()
    conn.close()
    return jsonify({'ok': True, 'activations': [dict(r) for r in rows]})


@app.route('/api/admin/activations/<int:aid>', methods=['DELETE'])
def admin_delete_activation(aid):
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db()
    c    = conn.cursor()
    c.execute('DELETE FROM activations WHERE id=%s', (aid,))
    conn.commit()
    conn.close()
    log_action('ADMIN', 'DEVICE_REMOVED', f'activation_id={aid}')
    return jsonify({'ok': True})


@app.route('/api/admin/licenses/<int:lid>', methods=['DELETE'])
def admin_delete_license(lid):
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db()
    c    = conn.cursor()
    # Načti klíč pro log
    c.execute('SELECT license_key FROM licenses WHERE id=%s', (lid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'License not found'}), 404
    key = row['license_key']
    # Smaž aktivace a licenci
    c.execute('DELETE FROM activations WHERE license_id=%s', (lid,))
    c.execute('DELETE FROM licenses WHERE id=%s', (lid,))
    conn.commit()
    conn.close()
    log_action(key, 'DELETED', 'admin deleted license')
    return jsonify({'ok': True})


@app.route('/api/admin/log', methods=['GET'])
def admin_log():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 200')
    rows = c.fetchall()
    conn.close()
    return jsonify({'ok': True, 'log': [dict(r) for r in rows]})


# ─── MODULY: kvoty, fakturace, POHODA ────────────────────────────────────────
# Registrace je zamerne v try/except: kdyby nekteremu modulu chybela promenna
# prostredi, licencni API musi bezet dal. Do logu se napise, co se nepodarilo.

def _register_modules():
    try:
        from quota import bp as quota_bp
        app.register_blueprint(quota_bp)
        print('[modules] quota OK', flush=True)
    except Exception as exc:
        print(f'[modules] quota se nenacetl: {exc}', flush=True)

    try:
        from invoices import bp as invoices_bp
        app.register_blueprint(invoices_bp)
        print('[modules] invoices OK', flush=True)
    except Exception as exc:
        print(f'[modules] invoices se nenacetl: {exc}', flush=True)

    try:
        from pohoda import bp as pohoda_bp
        app.register_blueprint(pohoda_bp)
        print('[modules] pohoda OK', flush=True)
    except Exception as exc:
        print(f'[modules] pohoda se nenacetl: {exc}', flush=True)


# Vola se i pri startu pres gunicorn (tam neplati __name__ == '__main__')
_register_modules()


# ─── START ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if DATABASE_URL:
        init_db()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)

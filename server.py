"""
iSupply Scan – Backend Server
==============================
pip install flask flask-cors pymobiledevice3
python server.py
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3
import hashlib
import datetime
import os
import threading
import json
import time
import queue
import asyncio
import subprocess
import re

# ─── FIX pro zabaleny EXE (PyInstaller onefile) ─────────────────────────────
# Nektere baliky (napr. readchar, ktery tahne pymobiledevice3) si pri importu
# ctou svou verzi pres importlib.metadata.version("<jmeno>"). V onefile EXE
# casto chybi jejich metadata (.dist-info) -> vyjimka "No package metadata was
# found for readchar" a pad. Obalime version() tak, aby pri selhani vratil
# nahradni hodnotu misto vyjimky. Meni chovani JEN kdyz realny lookup selze,
# takze je to bezpecne. Musi bezet PRED importem pymobiledevice3.
try:
    import importlib.metadata as _ilm
    _ilm_version_orig = _ilm.version
    def _ilm_version_safe(name, *a, **k):
        try:
            return _ilm_version_orig(name, *a, **k)
        except Exception:
            return '0.0.0'
    _ilm.version = _ilm_version_safe
    # nektere baliky pouzivaji i metadata()/distribution() - obalime taky
    try:
        _ilm_metadata_orig = _ilm.metadata
        def _ilm_metadata_safe(name, *a, **k):
            try:
                return _ilm_metadata_orig(name, *a, **k)
            except Exception:
                from email.message import Message
                m = Message(); m['Name'] = name; m['Version'] = '0.0.0'
                return m
        _ilm.metadata = _ilm_metadata_safe
    except Exception:
        pass
except Exception:
    pass


# ─── APPLE DRIVER CHECK ─────────────────────────────────────────────────────
def _check_apple_driver():
    """Zkontroluje Apple Mobile Device Support pri startu."""
    try:
        import winreg
        reg_paths = [
            "SOFTWARE\\Apple Inc.\\Apple Mobile Device Support",
            "SOFTWARE\\WOW6432Node\\Apple Inc.\\Apple Mobile Device Support",
            "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Apple Mobile Device Support",
        ]
        for path in reg_paths:
            try:
                k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                winreg.CloseKey(k)
                print("  ✅ Apple Mobile Device Support nalezen")
                return True
            except FileNotFoundError:
                continue

        # Zkontroluj driver soubor
        driver_paths = [
            "C:\\Windows\\System32\\drivers\\usbaapl64.sys",
            "C:\\Windows\\System32\\drivers\\usbaapl.sys",
        ]
        for p in driver_paths:
            if os.path.exists(p):
                print("  ✅ Apple USB driver nalezen")
                return True

        # Driver nenalezen
        print()
        print("─" * 52)
        print("  ⚠  Apple Mobile Device Support neni nainstalovan!")
        print("─" * 52)
        print("  iPhone detekce nebude fungovat bez tohoto driveru.")
        print()
        import webbrowser, threading
        def _open_store():
            import time; time.sleep(2)
            webbrowser.open("ms-windows-store://pdp/?ProductId=9NP83LWLPZ9K")
        threading.Thread(target=_open_store, daemon=True).start()
        print("  Oteviran Microsoft Store - nainstalujte Apple Devices.")
        print("  Po instalaci restartujte iSupply Scan.")
        print("─" * 52)
        print()
        return False
    except ImportError:
        # Nejsme na Windows (vyvoj na Mac/Linux)
        print("  ℹ  Kontrola driveru preskocena (non-Windows)")
        return True

import sys as _sys

def _get_base_dir():
    """Vrátí správnou složku - vedle .exe nebo vedle server.py"""
    if getattr(_sys, 'frozen', False):
        # Běžíme jako PyInstaller .exe - použij složku vedle .exe
        return os.path.dirname(_sys.executable)
    # Běžíme jako script
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _get_base_dir()
app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

DB_PATH        = os.path.join(_get_base_dir(), 'isupply_users.db')
LICENSE_API    = os.environ.get('ISUPPLY_API', 'https://isupply-scan.cz')
LICENSE_KEY    = None   # nastaveno ze souboru licence.key
SESSION_TOKEN  = None   # JWT token z Railway API
TOKEN_FILE     = os.path.join(_get_base_dir(), '.token_cache')
LICENSE_FILE   = os.path.join(_get_base_dir(), 'licence.key')


# ─── LICENCE VALIDACE ───────────────────────────────────────────────────────

def get_hwid() -> str:
    """Unikátní ID počítače – hash z MAC + hostname."""
    import uuid, platform
    raw = f"{uuid.getnode()}-{platform.node()}-{platform.processor()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def load_license_key() -> str | None:
    """Načte licenční klíč ze souboru licence.key."""
    if os.path.exists(LICENSE_FILE):
        with open(LICENSE_FILE, 'r') as f:
            key = f.read().strip()
            if key:
                return key
    return None


def save_token_cache(token: str):
    """Uloží JWT token pro offline použití."""
    try:
        with open(TOKEN_FILE, 'w') as f:
            f.write(token)
    except Exception:
        pass


def load_token_cache() -> str | None:
    """Načte cached token."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    return None


def validate_license_online(license_key: str) -> dict:
    """Ověří licenci na Railway API serveru."""
    import socket
    try:
        import urllib.request, json as json_lib
        hwid = get_hwid()
        hostname = socket.gethostname()
        payload = json_lib.dumps({
            'license_key': license_key,
            'hwid':        hwid,
            'hostname':    hostname,
        }).encode()
        req = urllib.request.Request(
            f"{LICENSE_API}/api/validate",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json_lib.loads(resp.read())
    except Exception as e:
        return {'ok': False, 'error': f'Cannot reach licence server: {e}'}


def validate_token_offline(token: str) -> dict:
    """Ověří cached JWT token offline (platný 24h)."""
    try:
        import base64, json as json_lib
        parts = token.split('.')
        if len(parts) != 3:
            return {'ok': False}
        payload_b64 = parts[1] + '=='
        payload = json_lib.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get('exp', 0)
        if exp > datetime.datetime.utcnow().timestamp():
            return {'ok': True, **payload}
        return {'ok': False, 'error': 'Token expired'}
    except Exception:
        return {'ok': False}


def check_license() -> tuple[bool, str]:
    """
    Hlavní validační funkce.
    1. Pokud existuje platný cached token → OK (offline)
    2. Pokud je online → ověř na Railway a obnov token
    Vrací (ok, message)
    """
    global SESSION_TOKEN, LICENSE_KEY

    LICENSE_KEY = load_license_key()
    if not LICENSE_KEY:
        return False, "❌ Licence nenalezena. Vytvořte soubor 'licence.key' s vaším licenčním klíčem."

    # Zkus online validaci
    print(f"  Ověřuji licenci: {LICENSE_KEY[:12]}...")
    result = validate_license_online(LICENSE_KEY)

    if result.get('ok'):
        SESSION_TOKEN = result.get('token', '')
        save_token_cache(SESSION_TOKEN)
        plan     = result.get('plan', '?')
        company  = result.get('company', '')
        valid    = result.get('valid_until', '?')
        seats_u  = result.get('seats_used', 1)
        seats_t  = result.get('seats_total', 1)
        print(f"  ✓ Licence OK: {company} | Plan: {plan} | Platnost: {valid} | Seats: {seats_u}/{seats_t}")
        return True, f"✓ {company} · {plan.upper()} · platnost do {valid}"

    # Online selhalo – zkus offline token
    print(f"  Online validace selhala: {result.get('error', '?')}")
    cached = load_token_cache()
    if cached:
        offline = validate_token_offline(cached)
        if offline.get('ok'):
            SESSION_TOKEN = cached
            print(f"  ✓ Offline token platný (do {datetime.datetime.fromtimestamp(offline.get('exp',0))})")
            return True, f"✓ Offline mode · token platný 24h"

    # Pokud licence.key existuje ale server není dostupný → DEV MODE
    # (dočasné chování do nasazení Railway API)
    err = result.get('error', '')
    if 'Cannot reach' in err or 'connect' in err.lower():
        print(f"  ⚠ Licence server nedostupný – spouštím v DEV MODE")
        print(f"  ⚠ Po nasazení Railway API bude vyžadována online validace")
        return True, f"⚠ DEV MODE (licence server offline) · klíč: {LICENSE_KEY[:12]}..."

    # Klíč je neplatný nebo vypršel
    err = result.get('error', 'Neznámá chyba')
    return False, f"❌ {err}"

usb_event_queue = queue.Queue()
connected_devices = {}
# Globalni zamek: nikdy neotevirat 2 usbmux/lockdown spojeni soucasne (napr.
# aktivace + USB monitor na pozadi) - soubezna spojeni k jednomu telefonu
# umely zpusobovaly nahodne MuxException chyby.
_usbmux_lock = threading.Lock()

# ─── DB ─────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'technician',
            license_type TEXT NOT NULL DEFAULT 'pro',
            license_valid_until TEXT NOT NULL DEFAULT '2099-12-31',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT DEFAULT NULL,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imei TEXT,
            serial TEXT,
            model TEXT,
            storage TEXT,
            color TEXT,
            ios_version TEXT,
            battery_pct INTEGER,
            grade TEXT,
            result TEXT,
            technician TEXT,
            tests_json TEXT,
            scanned_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS component_baseline (
            udid TEXT NOT NULL,
            component_key TEXT NOT NULL,
            factory_value TEXT,
            captured_at TEXT NOT NULL,
            captured_by TEXT DEFAULT '',
            PRIMARY KEY (udid, component_key)
        );
    ''')
    now = datetime.datetime.now().isoformat(timespec='seconds')
    for (u, pw, fn, em, co, ro, li, va, no) in [
        ('admin',    'admin',     'Administrator',   'admin@isupply.cz',    'iSupply s.r.o.',      'admin',      'enterprise', '2099-12-31', 'Hlavní admin'),
        ('tester1',  'Test1234!', 'Jan Novák',       'jan@refurb.cz',       'Refurb Praha s.r.o.', 'technician', 'pro',        '2026-12-31', 'Technik Praha'),
        ('tester2',  'Scan5678!', 'Petra Svobodová', 'petra@mobilezone.cz', 'MobileZone EU',       'technician', 'pro',        '2026-12-31', 'Technik Brno'),
        ('manager1', 'Mgr9999!',  'Karel Dvořák',    'karel@mobilezone.cz', 'MobileZone EU',       'manager',    'enterprise', '2026-12-31', 'Vedoucí skladu'),
    ]:
        try:
            c.execute('INSERT INTO users (username,password_hash,full_name,email,company,role,license_type,license_valid_until,active,created_at,notes) VALUES (?,?,?,?,?,?,?,?,1,?,?)',
                      (u, hash_pw(pw), fn, em, co, ro, li, va, now, no))
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    print(f"✓ Databáze: {DB_PATH}")


# ─── COMPONENT BASELINE (tovarni reference = stav pri prvnim scanu) ───────────
# Telefon vydava pro kazdy dil jen JEDNU hodnotu (aktualni, zivou). Nezavisla
# tovarni reference v telefonu neexistuje (overeno factory-check probe: zadny
# realny dil nema druhy, nezavisly zaznam v SysCfg/NVRAM). Proto referenci
# ulozime pri PRVNIM scanu daneho UDID = "golden" zaznam. Kazdy dalsi scan pak
# porovnava zivy dil proti nemu. DUVERYHODNE jen kdyz je prvni scan na telefonu,
# kteremu duverujes (pri prijmu) - "tovarni" tu znamena "stav pri prijmu k nam",
# ne "z Apple tovarny" (tu telefon nedrzi).
#
# TRI STAVY:
#   MATCH          - aktualni hodnota odpovida ulozene referenci (zeleny text)
#   MISMATCH       - aktualni hodnota se lisi (dil byl pravdepodobne vymenen; oranzovy text)
#   POSSIBLE_FAULT - aktualni hodnotu se VUBEC nepodarilo precist, ackoliv
#                    komponenta je pro tuto generaci aplikovatelna. To NENI
#                    "vymeneno" (na to bychom potrebovali znat aktualni hodnotu
#                    a ta chybi uplne) - je to signal mozne poruchy (dilu,
#                    jeho pripojeni, nebo casteji zakladni desky).

def _baseline_get(udid):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT component_key, factory_value FROM component_baseline WHERE udid=?",
            (udid,)).fetchall()
        conn.close()
        return {r["component_key"]: r["factory_value"] for r in rows}
    except Exception:
        return {}

def _baseline_set_many(udid, kv, by=""):
    """Ulozi/aktualizuje tovarni referenci pro dane komponenty. kv = {key: value}."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        conn = get_db()
        for k, v in kv.items():
            if not v:
                continue
            conn.execute(
                "INSERT INTO component_baseline (udid, component_key, factory_value, captured_at, captured_by) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(udid, component_key) DO UPDATE SET factory_value=excluded.factory_value, "
                "captured_at=excluded.captured_at, captured_by=excluded.captured_by",
                (udid, k, v, now, by))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"baseline_set chyba: {e}")
        return False

def _baseline_delete(udid):
    try:
        conn = get_db()
        conn.execute("DELETE FROM component_baseline WHERE udid=?", (udid,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def _apply_baseline(udid, components, capture_if_missing=True, by=""):
    """Doplni ke komponentam factory_value + status (MATCH/MISMATCH/POSSIBLE_FAULT)
    proti baseline. Pri prvnim scanu (zadny baseline pro UDID) aktualni hodnoty
    ulozi jako tovarni. Nedotyka se komponent s applicable=False (generace je
    fyzicky nema - to neni chyba cteni, resi se jinde).
    Vraci (components, meta)."""
    baseline = _baseline_get(udid)
    first_scan = not baseline
    to_capture = {}
    captured_now = []
    for key, item in components.items():
        if not item.get("applicable", True):
            continue   # generacne nedostupne komponenty se baseline netykaji
        cur = item.get("current_value") or item.get("value")
        fac = baseline.get(key)
        if cur:
            if fac is None:
                # jeste nemame tovarni referenci pro tento dil -> zachyt ji ted
                if capture_if_missing:
                    to_capture[key] = cur
                    captured_now.append(key)
                    item["factory_value"] = cur
                    item["current_value"] = cur
                    item["match"] = True
                    item["status"] = "MATCH"
                    item["baseline_captured"] = True
            else:
                match = (str(fac).upper() == str(cur).upper())
                item["factory_value"] = fac
                item["current_value"] = cur
                item["match"] = match
                item["status"] = "MATCH" if match else "MISMATCH"
        else:
            # Aktualni hodnotu se vubec nepodarilo precist. To je jiny pripad
            # nez MISMATCH (tam znama aktualni hodnota nesedi s referenci) -
            # tady aktualni hodnota chybi uplne, coz muze znamenat poruchu
            # dilu / jeho pripojeni / zakladni desky, ne nutne vymenu.
            item["factory_value"] = fac  # ukaz referenci, pokud ji mame, jinak None
            item["current_value"] = None
            item["match"] = False
            item["status"] = "POSSIBLE_FAULT"
            item["note"] = ("Nelze přečíst aktuální hodnotu – může jít o závadu "
                            "(např. základní deska nebo daná komponenta), ne nutně o výměnu.")
    if to_capture:
        _baseline_set_many(udid, to_capture, by=by)
    return components, {"first_scan": first_scan, "captured_now": captured_now,
                        "baseline_size": len(baseline) + len(to_capture)}

# ─── MODEL DATABÁZE ─────────────────────────────────────────────────────────
# Překlad Apple ProductType → čitelný název + číslo modelu A-Series

APPLE_MODELS = {
    # iPhone 16 Series
    'iPhone17,1': ('iPhone 16 Pro', 'A3293'),
    'iPhone17,2': ('iPhone 16 Pro Max', 'A3292'),
    'iPhone17,3': ('iPhone 16 Plus', 'A3291'),
    'iPhone17,4': ('iPhone 16', 'A3290'),
    # iPhone 15 Series
    'iPhone16,1': ('iPhone 15 Pro', 'A3101'),
    'iPhone16,2': ('iPhone 15 Pro Max', 'A3105'),
    'iPhone15,4': ('iPhone 15', 'A3090'),
    'iPhone15,5': ('iPhone 15 Plus', 'A3093'),
    # iPhone 14 Series
    'iPhone15,2': ('iPhone 14 Pro', 'A2890'),
    'iPhone15,3': ('iPhone 14 Pro Max', 'A2893'),
    'iPhone14,7': ('iPhone 14', 'A2882'),
    'iPhone14,8': ('iPhone 14 Plus', 'A2886'),
    # iPhone 13 Series
    'iPhone14,4': ('iPhone 13 mini', 'A2628'),
    'iPhone14,5': ('iPhone 13', 'A2633'),
    'iPhone14,2': ('iPhone 13 Pro', 'A2636'),
    'iPhone14,3': ('iPhone 13 Pro Max', 'A2641'),
    # iPhone 12 Series
    'iPhone13,1': ('iPhone 12 mini', 'A2399'),
    'iPhone13,2': ('iPhone 12', 'A2403'),
    'iPhone13,3': ('iPhone 12 Pro', 'A2407'),
    'iPhone13,4': ('iPhone 12 Pro Max', 'A2411'),
    # iPhone 11 Series
    'iPhone12,1': ('iPhone 11', 'A2111'),
    'iPhone12,3': ('iPhone 11 Pro', 'A2160'),
    'iPhone12,5': ('iPhone 11 Pro Max', 'A2161'),
    # iPhone XS/XR Series
    'iPhone11,2': ('iPhone XS', 'A1920'),
    'iPhone11,4': ('iPhone XS Max', 'A1921'),
    'iPhone11,6': ('iPhone XS Max', 'A2104'),
    'iPhone11,8': ('iPhone XR', 'A1984'),
    # iPhone X Series
    'iPhone10,3': ('iPhone X', 'A1865'),
    'iPhone10,6': ('iPhone X', 'A1901'),
    # iPhone 8 Series
    'iPhone10,1': ('iPhone 8', 'A1863'),
    'iPhone10,4': ('iPhone 8', 'A1905'),
    'iPhone10,2': ('iPhone 8 Plus', 'A1864'),
    'iPhone10,5': ('iPhone 8 Plus', 'A1897'),
    # iPhone 7 Series
    'iPhone9,1':  ('iPhone 7', 'A1660'),
    'iPhone9,3':  ('iPhone 7', 'A1778'),
    'iPhone9,2':  ('iPhone 7 Plus', 'A1661'),
    'iPhone9,4':  ('iPhone 7 Plus', 'A1784'),
    # iPhone SE Series
    'iPhone14,6': ('iPhone SE (3. gen)', 'A2595'),
    'iPhone12,8': ('iPhone SE (2. gen)', 'A2275'),
    'iPhone8,4':  ('iPhone SE (1. gen)', 'A1662'),
    # iPhone 6S Series
    'iPhone8,1':  ('iPhone 6s', 'A1633'),
    'iPhone8,2':  ('iPhone 6s Plus', 'A1634'),
    # iPhone 6 Series
    'iPhone7,2':  ('iPhone 6', 'A1549'),
    'iPhone7,1':  ('iPhone 6 Plus', 'A1522'),
}

def resolve_model(product_type, sales_model=None):
    """Vrátí (čitelný název, číslo modelu A-Series)"""
    if product_type in APPLE_MODELS:
        name, a_number = APPLE_MODELS[product_type]
        return name, a_number
    # Pokud není v databázi, použij SalesModel nebo ProductType
    if sales_model and sales_model not in ('N/A', '', None):
        return product_type, sales_model
    return product_type, 'N/A'

# ─── USB DETEKCE – vlastní event loop v separátním vlákně ───────────────────

def get_device_info(udid):
    """
    Získá info o zařízení. Běží v samostatném vlákně s vlastním event loop.
    """
    async def _fetch():
        from pymobiledevice3.lockdown import create_using_usbmux
        import inspect

        # Zjisti signaturu funkce
        sig = inspect.signature(create_using_usbmux)
        params = list(sig.parameters.keys())
        print(f"  create_using_usbmux params: {params}")

        # Zavolej správně podle parametrů
        if 'serial' in params:
            if inspect.iscoroutinefunction(create_using_usbmux):
                ld = await create_using_usbmux(serial=udid)
            else:
                ld = create_using_usbmux(serial=udid)
        elif 'udid' in params:
            if inspect.iscoroutinefunction(create_using_usbmux):
                ld = await create_using_usbmux(udid=udid)
            else:
                ld = create_using_usbmux(udid=udid)
        else:
            # Zkus pozicionálně
            if inspect.iscoroutinefunction(create_using_usbmux):
                ld = await create_using_usbmux(udid)
            else:
                ld = create_using_usbmux(udid)

        vals = ld.all_values
        if asyncio.iscoroutine(vals):
            vals = await vals

        # ── Kapacita z com.apple.disk_usage domény ───────────────
        storage = 'N/A'
        try:
            disk_vals = ld.get_value(domain='com.apple.disk_usage')
            if asyncio.iscoroutine(disk_vals):
                disk_vals = await disk_vals
            print(f"  disk_vals type: {type(disk_vals)}, keys: {list(disk_vals.keys()) if isinstance(disk_vals, dict) else 'N/A'}")
            if isinstance(disk_vals, dict):
                # AmountRestoreAvailable = celková kapacita flash storage
                raw = disk_vals.get('AmountRestoreAvailable', 0)
                if not raw:
                    # Fallback: volné + rezervované
                    raw = (disk_vals.get('AmountDataAvailable', 0) or 0) +                           (disk_vals.get('AmountDataReserved', 0) or 0)
                if raw and int(raw) > 0:
                    gb_raw = int(raw) / 1e9
                    for std in [8, 16, 32, 64, 128, 256, 512, 1024]:
                        if gb_raw <= std * 1.15:
                            storage = f'{std} GB' if std < 1024 else '1 TB'
                            break
                    else:
                        storage = f'{round(gb_raw)} GB'
        except Exception as se:
            print(f"  Storage chyba: {se}")
            # Fallback na all_values
            for key in ['TotalDiskCapacity', 'TotalDataCapacity']:
                try:
                    raw = vals.get(key, 0)
                    if raw and int(raw) > 0:
                        gb_raw = int(raw) / 1e9
                        for std in [8, 16, 32, 64, 128, 256, 512, 1024]:
                            if gb_raw <= std * 1.15:
                                storage = f'{std} GB' if std < 1024 else '1 TB'
                                break
                        break
                except Exception:
                    continue

        # ── Barva ─────────────────────────────────────────────────
        color_raw = str(vals.get('DeviceColor') or '')
        # Apple iPhone XS Space Gray = '1', Silver = '2', Gold = '3'
        # Novější modely používají hex kódy
        COLOR_MAP_NUM = {
            '1': 'Space Gray', '2': 'Silver', '3': 'Gold',
            '4': 'Space Black', '5': 'Rose Gold',
        }
        COLOR_MAP_HEX = {
            '#1d1d1f': 'Black', '#f5f5f0': 'White', '#faf6f2': 'White',
            '#e8e0d5': 'Starlight', '#3d3c3d': 'Space Gray',
            '#f2f2f2': 'Silver', '#aec8e0': 'Blue', '#6e7a6e': 'Alpine Green',
            '#354e49': 'Deep Purple', '#f9e5c8': 'Yellow', '#e8d1c4': 'Pink',
            '#2c2c2e': 'Midnight', '#5b6a78': 'Blue Titanium',
            '#4e4b46': 'Black Titanium', '#d4c5b0': 'Natural Titanium',
            '#e8e3d8': 'White Titanium', '#c6c8ca': 'Silver', '#e8e1d5': 'Gold',
        }
        if color_raw.startswith('#'):
            color = COLOR_MAP_HEX.get(color_raw.lower(), color_raw)
        elif color_raw.isdigit():
            color = COLOR_MAP_NUM.get(color_raw, f'Color {color_raw}')
        else:
            color = color_raw if color_raw else 'N/A'

        # ── Baterie – kondice z com.apple.mobile.battery ──────────
        battery_pct = vals.get('BatteryCurrentCapacity', 0) or 0
        try:
            batt_vals = ld.get_value(domain='com.apple.mobile.battery')
            if asyncio.iscoroutine(batt_vals):
                batt_vals = await batt_vals
            if isinstance(batt_vals, dict):
                # BatteryCurrentCapacity = aktuální nabití
                battery_pct = batt_vals.get('BatteryCurrentCapacity', battery_pct) or battery_pct
                print(f"  Battery domain keys: {list(batt_vals.keys())}")
                print(f"  Battery vals: { {k:v for k,v in batt_vals.items()} }")
        except Exception as be:
            print(f"  Battery domain chyba: {be}")

        # ── Model ─────────────────────────────────────────────────
        product_type = vals.get('ProductType', 'N/A')
        sales_model  = vals.get('SalesModel', vals.get('ModelNumber', 'N/A'))
        model_name, a_number = resolve_model(product_type, sales_model)

        # ── Kondice baterie – stejný výpočet jako 3uTools / Apple iOS ──
        # Apple používá: MaximumCapacityPercent (přímá hodnota) nebo
        # AppleRawMaxCapacity / DesignCapacity * 100 (výpočet)
        # Zdroj: diagnostics_relay → ioregentry AppleSmartBattery
        battery_health = 0
        battery_cycles = 0
        raw_max = 0
        design_cap = 0

        try:
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            import inspect

            # Vytvoř DiagnosticsService
            if inspect.iscoroutinefunction(DiagnosticsService):
                diag = await DiagnosticsService(ld)
            else:
                diag = DiagnosticsService(ld)

            # Zkus ioregentry AppleSmartBattery (hlavní zdroj)
            iokit = None
            for entry_name in ['AppleSmartBattery', 'AppleARMPMUCharger']:
                try:
                    fn = getattr(diag, 'ioregistry_entry', None)
                    if fn:
                        iokit = fn(entry_name)
                        if asyncio.iscoroutine(iokit):
                            iokit = await iokit
                        if iokit:
                            print(f"  ✓ IOKit {entry_name} OK")
                            break
                except Exception as e:
                    print(f"  IOKit {entry_name}: {e}")

            if not iokit:
                # Fallback: get_battery() metoda
                for method in ['get_battery', 'battery']:
                    fn = getattr(diag, method, None)
                    if fn:
                        try:
                            iokit = fn()
                            if asyncio.iscoroutine(iokit):
                                iokit = await iokit
                            if iokit:
                                break
                        except Exception:
                            pass

            if isinstance(iokit, dict):
                # Vypiš klíče pro debug
                cap_keys = {k:v for k,v in iokit.items()
                            if any(x in k.lower() for x in ['cap','health','max','cycle','design','nominal'])}
                print(f"  Battery IOKit keys: {cap_keys}")

                # === METODA 1: MaximumCapacityPercent ===
                # Přesná hodnota co zobrazuje iOS v Nastavení → Baterie
                mcp = iokit.get('MaximumCapacityPercent')
                if mcp is not None and 0 < int(mcp) <= 100:
                    battery_health = int(mcp)
                    print(f"  ✓ MaximumCapacityPercent = {battery_health}%")

                # === METODA 2: AppleRawMaxCapacity / DesignCapacity ===
                # Stejný výpočet jako 3uTools a idevicediagnostics
                # health% = (AppleRawMaxCapacity / DesignCapacity) * 100
                if not battery_health:
                    raw_max    = iokit.get('AppleRawMaxCapacity', 0)
                    design_cap = iokit.get('DesignCapacity', 0)
                    if raw_max and design_cap and int(design_cap) > 0:
                        battery_health = min(100, round(int(raw_max) / int(design_cap) * 100))
                        print(f"  ✓ AppleRawMaxCapacity/DesignCapacity = {raw_max}/{design_cap} = {battery_health}%")

                # === METODA 3: NominalChargeCapacity / DesignCapacity ===
                if not battery_health:
                    nominal    = iokit.get('NominalChargeCapacity', 0)
                    design_cap = iokit.get('DesignCapacity', 0)
                    if nominal and design_cap and int(design_cap) > 0:
                        battery_health = min(100, round(int(nominal) / int(design_cap) * 100))
                        print(f"  ✓ NominalChargeCapacity/DesignCapacity = {nominal}/{design_cap} = {battery_health}%")

                # Počet nabíjecích cyklů (bonus info)
                battery_cycles = iokit.get('CycleCount', 0) or iokit.get('AppleRawCycleCount', 0)
                if battery_cycles:
                    print(f"  ✓ CycleCount = {battery_cycles}")

        except Exception as be:
            print(f"  DiagnosticsService chyba: {be}")

        # Sanitace – kondice musí být 1–100 %
        if not battery_health or battery_health <= 0 or battery_health > 100:
            print(f"  ⚠ battery_health={battery_health} neplatné, fallback na BatteryCurrentCapacity")
            battery_health = battery_pct  # aktuální nabití jako poslední možnost

        result = {
            'udid':           udid,
            'imei':           vals.get('InternationalMobileEquipmentIdentity', 'N/A'),
            'serial':         vals.get('SerialNumber', 'N/A'),
            'product_type':   product_type,
            'model':          model_name,
            'a_number':       a_number,
            'name':           vals.get('DeviceName', 'iPhone'),
            'ios':            vals.get('ProductVersion', 'N/A'),
            'build':          vals.get('BuildVersion', 'N/A'),
            'storage':        storage,
            'color':          color,
            'battery':        battery_pct,
            'battery_health': battery_health,
            'battery_cycles': battery_cycles,
            'activation':     vals.get('ActivationState', 'N/A'),
            'icloud_lock':    vals.get('FMiPActivationLockIsActivatable', False),
        }
        print(f"  ✓ VÝSLEDEK: model={result['model']} | storage={result['storage']} | color={result['color']} | battery={result['battery']} | health={result['battery_health']}")
        return result

    # Vlastní izolovaný event loop
    import time as _t
    loop = asyncio.new_event_loop()
    try:
        # Retry na transientní MuxException 183 (konkurenční usbmux při připojení).
        last_err = None
        for _attempt in range(4):
            try:
                with _usbmux_lock:
                    result = loop.run_until_complete(_fetch())
                print(f"  ✓ {result['name']} | IMEI: {result['imei']} | iOS: {result['ios']} | Baterie: {result['battery']}%")
                return result
            except Exception as e:
                last_err = e
                if ('MuxException' in type(e).__name__) or ('183' in str(e)):
                    print(f"  ⟳ usbmux 183, pokus {_attempt+1}/4, čekám…")
                    _t.sleep(0.4 * (_attempt + 1))
                    continue
                raise
        raise last_err
    except Exception as e:
        print(f"  ✗ get_device_info chyba: {e}")
        # Vrátit alespoň UDID
        return {
            'udid': udid, 'imei': 'Načítání...', 'serial': udid[:12],
            'model': 'iPhone', 'name': 'iPhone', 'ios': 'N/A',
            'storage': 'N/A', 'color': 'N/A', 'battery': 0,
        }
    finally:
        loop.close()


def usb_monitor_thread():
    """Sleduje USB připojení – běží v separátním vlákně s vlastním event loop."""
    print("✓ USB monitor spuštěn")

    try:
        import pymobiledevice3
        print(f"✓ pymobiledevice3 v{getattr(pymobiledevice3, '__version__', '?')} – USB detekce aktivní")
    except ImportError:
        print("⚠ pymobiledevice3 není – spusťte: pip install pymobiledevice3")
        return

    async def _monitor():
        from pymobiledevice3.usbmux import select_devices_by_connection_type
        import inspect
        known = set()

        while True:
            try:
                if inspect.iscoroutinefunction(select_devices_by_connection_type):
                    devs = await select_devices_by_connection_type(connection_type='USB')
                else:
                    devs = select_devices_by_connection_type(connection_type='USB')

                current = set()
                for d in devs:
                    uid = getattr(d, 'serial', None) or getattr(d, 'udid', None)
                    if uid:
                        current.add(uid)

                # Nově připojená
                for uid in current - known:
                    print(f"  📱 Připojeno: {uid}")
                    # Info načíst ve vlastním vlákně aby se nesmíchaly event loops
                    info_thread = threading.Thread(
                        target=lambda u=uid: _handle_connect(u),
                        daemon=True
                    )
                    info_thread.start()
                    known.add(uid)

                # Odpojená
                for uid in known - current:
                    print(f"  📵 Odpojeno: {uid}")
                    connected_devices.pop(uid, None)
                    usb_event_queue.put({'event': 'disconnected', 'udid': uid})
                    known.discard(uid)

            except Exception as e:
                print(f"  USB monitor chyba: {e}")

            await asyncio.sleep(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_monitor())
    finally:
        loop.close()


# Zámek aby se zabránilo dvojité detekci
_connecting_lock = threading.Lock()
_connecting_udids = set()

def _handle_connect(udid):
    """Načte info o zařízení a pošle event – v separátním vlákně."""
    # Zabránit dvojitému zpracování stejného UDID
    with _connecting_lock:
        if udid in _connecting_udids:
            print(f"  ⚠ {udid} již se zpracovává, přeskakuji")
            return
        _connecting_udids.add(udid)
    try:
        info = get_device_info(udid)
        if info:
            connected_devices[udid] = info
            usb_event_queue.put({'event': 'connected', 'udid': udid, 'info': info})
    finally:
        with _connecting_lock:
            _connecting_udids.discard(udid)


# ─── STATIC ──────────────────────────────────────────────────────────────────

def _resource_dir(filename):
    """Kde soubor hledat: 1) vedle EXE (override HTML bez rebuildu),
    2) fallback na zabalenou kopii uvnitř onefile EXE (_MEIPASS)."""
    if os.path.exists(os.path.join(BASE_DIR, filename)):
        return BASE_DIR
    mp = getattr(_sys, '_MEIPASS', None)
    if mp and os.path.exists(os.path.join(mp, filename)):
        return mp
    return BASE_DIR

@app.route('/')
def index():
    return send_from_directory(_resource_dir('iphone-diagnostic.html'), 'iphone-diagnostic.html')

@app.route('/admin')
def admin_page():
    return send_from_directory(_resource_dir('isupply_admin.html'), 'isupply_admin.html')

@app.route('/support')
def support_page():
    return send_from_directory(_resource_dir('support.html'), 'support.html')

@app.route('/api/driver-check')
def api_driver_check():
    """Zjisti, zda appka vidi Apple ovladace. Primarne testuje realne spojeni pres usbmux."""
    import platform
    if platform.system() != 'Windows':
        return jsonify({'installed': True, 'platform': platform.system()})
    # 1) Nejspolehlivejsi: zkusit spojeni pres usbmux (to, co appka realne potrebuje)
    try:
        from pymobiledevice3 import usbmux
        import inspect as _insp
        _res = usbmux.list_devices()
        # V async verzi je list_devices korutina – musí se doawaitovat a uzavřít,
        # jinak zůstane osiřelá a ROZBIJE usbmux spojení (pak 183/Number:3 dokola).
        if _insp.isawaitable(_res):
            _run_async_isolated(_res, timeout=10)
        return jsonify({'installed': True, 'method': 'usbmux'})
    except Exception:
        pass
    # 2) Windows sluzba (klasicky iTunes standalone)
    import subprocess, os
    try:
        r = subprocess.run(['sc', 'query', 'Apple Mobile Device Service'],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            return jsonify({'installed': True, 'method': 'service'})
    except Exception:
        pass
    # 3) Slozka s ovladaci
    for base in (os.environ.get('ProgramFiles',''), os.environ.get('ProgramFiles(x86)','')):
        if base and os.path.isdir(os.path.join(base, 'Common Files', 'Apple', 'Mobile Device Support')):
            return jsonify({'installed': True, 'method': 'folder'})
    # 4) Registr
    try:
        import winreg
        for path in (r'SOFTWARE\Apple Inc.\Apple Mobile Device Support',
                     r'SOFTWARE\WOW6432Node\Apple Inc.\Apple Mobile Device Support',
                     r'SOFTWARE\Apple Inc.\Apple Application Support'):
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                return jsonify({'installed': True, 'method': 'registry'})
            except OSError:
                pass
    except Exception:
        pass
    return jsonify({'installed': False})

@app.route('/api/version')
def api_version():
    return jsonify({'ok': True, 'build': 'strict-source-isolated-v29.7-possiblefault',
                    'endpoints': ['component-serials', 'hardware-report', 'devices',
                                  'prox-discovery', 'node-dump', 'full-scan',
                                  'als-differential', 'service-map', 'als-channels', 'factory-check',
                                  'baseline', 'set-baseline',
                                  'v30-component-map-probe', 'v31-sensor-storage-probe',
                                  'v32-exact-sensor-forensic', 'v33-als-last-mile-probe', 'v34-spu-hid-als-probe', 'v35-hid-report-probe', 'v36-als-spu-aop-map', 'v37-als-final-probe']})

@app.route('/api/activation-diag', methods=['POST'])
def api_activation_diag():
    """DIAGNOSTIKA aktivace: zkusi precist aktivacni stav + provest aktivaci
    a vrati VSECHNO syrove (stav, chyby, traceback), at vidime PROC to neprochazi.
    Nic neobchazi - jen standardni Apple activation handshake."""
    import traceback
    out = {'ok': True, 'steps': []}

    def _run(coro_or_val):
        import asyncio as _a
        if _a.iscoroutine(coro_or_val):
            loop = _a.new_event_loop()
            try:
                _a.set_event_loop(loop)
                return loop.run_until_complete(_a.wait_for(coro_or_val, timeout=60))
            finally:
                loop.close(); _a.set_event_loop(None)
        return coro_or_val

    # 1) pripojeni
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        ld = create_using_usbmux()
        out['steps'].append({'step': 'connect', 'ok': True})
    except Exception as e:
        out['steps'].append({'step': 'connect', 'ok': False, 'error': str(e),
                              'trace': traceback.format_exc()})
        return jsonify(out), 200

    # 2) precti zakladni lockdown hodnoty (stav aktivace, FMi)
    info = {}
    for key in ('ActivationState', 'ActivationStateAcknowledged', 'BrickState',
                'DeviceClass', 'ProductVersion', 'ProductType', 'UniqueDeviceID',
                'SerialNumber', 'InternationalMobileEquipmentIdentity'):
        try:
            info[key] = ld.get_value(key=key)
        except Exception as e:
            info[key] = 'ERR:' + str(e)
    for domain, k in (('com.apple.fmip', 'IsAssociated'),
                      ('com.apple.mobile.chaperone', 'IsAssociated')):
        try:
            info[domain + '/' + k] = ld.get_value(domain=domain, key=k)
        except Exception as e:
            info[domain + '/' + k] = 'ERR:' + str(e)
    out['lockdown_info'] = {kk: (str(vv)[:120] if not isinstance(vv, (bool, int, type(None))) else vv)
                            for kk, vv in info.items()}

    # 3) aktivacni stav pres mobile_activation
    try:
        from pymobiledevice3.services.mobile_activation import MobileActivationService
        svc = MobileActivationService(ld)
        try:
            state = _run(svc.state)
        except Exception as e:
            state = 'ERR:' + str(e)
        out['activation_state'] = str(state)
        out['steps'].append({'step': 'read_state', 'ok': True, 'state': str(state)})
    except Exception as e:
        out['steps'].append({'step': 'read_state', 'ok': False, 'error': str(e),
                             'trace': traceback.format_exc()})
        svc = None

    # 4) POKUS o aktivaci - a hlavne CHYT presnou chybu z Apple serveru
    if svc is not None:
        try:
            _run(svc.activate())
            try:
                new_state = _run(MobileActivationService(ld).state)
            except Exception:
                new_state = '?'
            out['steps'].append({'step': 'activate', 'ok': True, 'new_state': str(new_state)})
        except Exception as e:
            out['steps'].append({'step': 'activate', 'ok': False,
                                 'error': str(e),
                                 'error_type': type(e).__name__,
                                 'trace': traceback.format_exc()})
    return jsonify(out), 200

# ─── AKTIVACE JEDNOTLIVÉHO ZAŘÍZENÍ ──────────────────────────────────────────
# Standardní Apple aktivace (mobileactivationd) pro JEDEN konkrétní telefon
# (podle UDID daného slotu). Používá skip_apple_id_query=True, takže knihovna
# sama vyhodí čistou výjimku, když Apple vyžaduje ověření vlastníka – appka
# NIKDY nežádá ani neukládá Apple ID heslo.

async def _create_lockdown_for_udid(udid):
    from pymobiledevice3.lockdown import create_using_usbmux
    import inspect
    sig = inspect.signature(create_using_usbmux)
    params = sig.parameters
    if 'serial' in params:
        value = create_using_usbmux(serial=udid)
    elif 'udid' in params:
        value = create_using_usbmux(udid=udid)
    else:
        value = create_using_usbmux(udid)
    return await value if inspect.isawaitable(value) else value

async def _activate_one_standard(udid):
    from pymobiledevice3.services.mobile_activation import MobileActivationService
    from pymobiledevice3.exceptions import MobileActivationException

    ld = await _create_lockdown_for_udid(udid)
    svc = MobileActivationService(ld)
    before = await svc.state()

    if str(before).lower() == 'activated':
        return {'status': 'ALREADY_ACTIVATED', 'before': str(before), 'after': str(before)}

    try:
        # Žádný interaktivní prompt na Apple ID – při BuddyML auth requestu
        # pymobiledevice3 vyhodí MobileActivationException("Device is iCloud locked").
        await svc.activate(skip_apple_id_query=True)
    except MobileActivationException as exc:
        msg = str(exc)
        if 'icloud locked' in msg.lower():
            return {
                'status': 'OWNER_AUTH_REQUIRED',
                'before': str(before),
                'after': str(before),
                'message': 'Apple vyžaduje ověření vlastníka na zařízení.'
            }
        return {'status': 'ACTIVATION_ERROR', 'before': str(before), 'message': msg}
    except Exception as exc:
        return {
            'status': 'ACTIVATION_ERROR',
            'before': str(before),
            'message': f'{type(exc).__name__}: {exc}'
        }

    after = await MobileActivationService(ld).state
    if str(after).lower() == 'activated':
        return {'status': 'ACTIVATED', 'before': str(before), 'after': str(after)}
    return {
        'status': 'ACTIVATION_ERROR',
        'before': str(before),
        'after': str(after),
        'message': 'Aktivační handshake doběhl, ale zařízení není ve stavu Activated.'
    }

def _run_async_isolated(awaitable, timeout=90):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        with _usbmux_lock:
            return loop.run_until_complete(asyncio.wait_for(awaitable, timeout=timeout))
    finally:
        asyncio.set_event_loop(None)
        loop.close()

@app.route('/api/device-activate', methods=['POST'])
def api_device_activate_single():
    """Aktivuje JEDEN telefon podle UDID (posílá frontend z konkrétního slotu)."""
    data = request.get_json(silent=True) or {}
    udid = (data.get('udid') or '').strip()
    if not udid:
        return jsonify({'ok': False, 'error': 'Chybí udid.'}), 400
    try:
        result = _run_async_isolated(_activate_one_standard(udid), timeout=90)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 200

    if udid in connected_devices:
        connected_devices[udid]['activation_result'] = result['status']
        if result['status'] in ('ACTIVATED', 'ALREADY_ACTIVATED'):
            connected_devices[udid]['activation'] = 'Activated'

    ok = result['status'] in ('ACTIVATED', 'ALREADY_ACTIVATED')
    locked = result['status'] == 'OWNER_AUTH_REQUIRED'
    return jsonify({'ok': ok, 'locked': locked, 'status': result['status'],
                    'message': result.get('message', ''), 'result': result}), 200

@app.route('/api/auto-activate-config', methods=['GET'])
def api_auto_activate_get():
    return jsonify({'ok': True, 'enabled': get_setting('auto_activate_enabled', '0') == '1'})

@app.route('/api/auto-activate-config', methods=['POST'])
def api_auto_activate_set():
    data = request.get_json(silent=True) or {}
    set_setting('auto_activate_enabled', '1' if data.get('enabled') else '0')
    return jsonify({'ok': True, 'enabled': bool(data.get('enabled'))})

# ─── AKTUÁLNÍ SÉRIOVÁ ČÍSLA KOMPONENT ────────────────────────────────────────
# READ-ONLY: cte jen to, co je PRAVE TED nainstalovane v telefonu (IOKit
# ioregistry). NEPOROVNAVA s tovarni hodnotou - tu nemame odkud legalne vzit
# (vyzadovalo by pristup k Apple GSX / cizim databazim). Appka proto NIKDY
# netvrdi "originál" / "vyměněno" - jen ukazuje aktualni stav.
_COMPONENT_SERIAL_LABELS = {
    "rear_camera": "Zadní kamera",
    "front_camera": "Přední kamera",
    "tele_camera": "Teleobjektiv",
    "ultrawide_camera": "Ultraširokoúhlá kamera",
    "front_ir_camera": "Přední IR kamera",
    "true_depth_projector": "Dot projektor (Lattice)",
    "distance_sensor": "Distance Sensor",
    "ambient_light_sensor": "Ambient Light Sensor",
    "screen": "Displej",
    "wifi": "Wi-Fi",
    "bluetooth": "Bluetooth",
    "cellular": "Mobilní síť",
    "mainboard": "Základní deska",
    "battery": "Baterie",
    "taptic_engine": "Taptic Engine",
    "nand": "NAND / Úložiště",
}

_COMPONENT_SERIAL_GROUPS = {
    "cameras": {"label": "Kamery", "components": [
        "rear_camera", "front_camera", "tele_camera", "ultrawide_camera",
        "front_ir_camera", "true_depth_projector"]},
    "sensors": {"label": "Senzory", "components": [
        "distance_sensor", "ambient_light_sensor"]},
    "display": {"label": "Displej", "components": ["screen"]},
    "connectivity": {"label": "Konektivita", "components": ["wifi", "bluetooth", "cellular"]},
    "hardware": {"label": "Hardware", "components": [
        "mainboard", "battery", "taptic_engine", "nand"]},
}

def _component_serial_scalar(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        txt = raw.decode("ascii", errors="ignore").strip("\x00").strip()
        return txt or raw.hex()
    if isinstance(value, (str, int, float)):
        txt = str(value).strip()
        return txt or None
    return None

def _component_serial_find(value, wanted_keys):
    wanted = {str(x).lower() for x in wanted_keys}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in wanted:
                scalar = _component_serial_scalar(child)
                if scalar:
                    return scalar
        for child in value.values():
            found = _component_serial_find(child, wanted_keys)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _component_serial_find(child, wanted_keys)
            if found:
                return found
    return None

def _component_serial_panel_id(value):
    panel_id = _component_serial_find(value, ("Panel_ID", "PanelID"))
    if not panel_id:
        return None
    first = re.split(r"[\s,;|:/]+", panel_id.strip(), maxsplit=1)[0].strip()
    return first or None

async def _open_diag(udid):
    import inspect
    from pymobiledevice3.services.diagnostics import DiagnosticsService
    ld = await _create_lockdown_for_udid(udid)
    if inspect.iscoroutinefunction(DiagnosticsService):
        diag = await DiagnosticsService(ld)
    else:
        diag = DiagnosticsService(ld)
    return ld, diag


# ─── SERVICE-MAP PROBE (Faze 1: zmapovat plochu sluzeb) ──────────────────────
# READ-ONLY. Cil: zjistit, KTERY kanal by vubec mohl vydat live identitu
# komponent (napr. ALS 0311133F6B07), ktera NENI ve verejnem IORegistry.
# Vypise: verzi pymobiledevice3, skutecne volatelne metody Lockdown i
# DiagnosticsService (verzne odolne), vysledek pokusu nastartovat kuratorovany
# seznam sluzeb (jen otevre kanal a hned zavre - nic neposila, nic nemeni),
# a dostupnost RemoteXPC/DDI (iOS 17+ diagnosticka plocha za tunneld).
_SVC_CANDIDATES = (
    # diagnosticke / servisni (nejnadejnejsi pro identitu HW)
    "com.apple.mobile.diagnostics_relay",
    "com.apple.iosdiagnostics.relay",
    "com.apple.mobile.assertion_agent",
    "com.apple.os_trace_relay",
    "com.apple.syslog_relay",
    "com.apple.pcapd",
    "com.apple.crashreportcopymobile",
    "com.apple.mobile.heartbeat",
    # AST / field diagnostics (pokud jsou inzerovane)
    "com.apple.atc",
    "com.apple.ait.aitd",
    "com.apple.testmanagerd.lockdown",
    "com.apple.dt.testmanagerd.lockdown",
    "com.apple.instruments.remoteserver",
    "com.apple.instruments.remoteserver.DVTSecureSocketProxy",
    # image mounter (DDI), aktivace, ostatni bezne
    "com.apple.mobile.mobile_image_mounter",
    "com.apple.mobileactivationd",
    "com.apple.companion_proxy",
    "com.apple.mobile.installation_proxy",
    "com.apple.springboardservices",
    "com.apple.mobile.MCInstall",
    "com.apple.idamd",
)

async def _service_map_collect(udid):
    import inspect
    out = {
        "ok": True, "udid": udid, "probe": "service-map",
        "pymobiledevice3_version": None,
        "start_method": None,
        "lockdown_methods": [], "diagnostics_methods": [],
        "services": [], "remote_services": None, "notes": [],
    }
    try:
        import pymobiledevice3
        out["pymobiledevice3_version"] = getattr(pymobiledevice3, "__version__", None)
    except Exception as e:
        out["notes"].append(f"version: {type(e).__name__}: {e}")

    ld = await _create_lockdown_for_udid(udid)

    # volatelna plocha lockdown clienta (verzne odolne)
    try:
        out["lockdown_methods"] = sorted(m for m in dir(ld) if not m.startswith("_"))
    except Exception as e:
        out["notes"].append(f"lockdown dir: {type(e).__name__}: {e}")

    # zjisti spravny nazev metody pro start sluzby
    start_fn = None
    for name in ("start_lockdown_service", "start_service", "start_lockdown_developer_service"):
        fn = getattr(ld, name, None)
        if callable(fn):
            start_fn = fn
            out["start_method"] = name
            break
    if start_fn is None:
        out["notes"].append("Nenalezena metoda pro start sluzby (start_lockdown_service/start_service).")

    # zkus nastartovat kazdou kandidatni sluzbu (jen otevri + zavri)
    if start_fn is not None:
        for svc in _SVC_CANDIDATES:
            rec = {"service": svc, "available": False}
            try:
                s = start_fn(svc)
                if inspect.isawaitable(s):
                    s = await s
                rec["available"] = bool(s)
                # okamzite zavri, nic neposilej
                try:
                    closer = getattr(s, "close", None) or getattr(s, "aclose", None)
                    if callable(closer):
                        r = closer()
                        if inspect.isawaitable(r):
                            await r
                except Exception:
                    pass
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
            out["services"].append(rec)

    # volatelna plocha DiagnosticsService (co realne umi tvoje verze)
    try:
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        diag = (await DiagnosticsService(ld)) if inspect.iscoroutinefunction(DiagnosticsService) else DiagnosticsService(ld)
        out["diagnostics_methods"] = sorted(m for m in dir(diag) if not m.startswith("_"))
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass
    except Exception as e:
        out["notes"].append(f"diagnostics: {type(e).__name__}: {e}")

    # RemoteXPC / DDI (iOS 17+ diagnosticka plocha) - jen dostupnost importu
    try:
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService  # noqa: F401
        out["remote_services"] = ("RemoteServiceDiscovery je k dispozici v pymobiledevice3, "
                                  "ale vyzaduje bezici tunnel (napr. 'sudo pymobiledevice3 remote tunneld'). "
                                  "Bez tunelu RemoteXPC sluzby na iOS 17+ nejsou videt.")
    except Exception as e:
        out["remote_services"] = f"RemoteServiceDiscovery nedostupne: {type(e).__name__}: {e}"

    return out

@app.route('/api/service-map/<udid>', methods=['GET'])
def api_service_map(udid):
    try:
        result = _run_async_isolated(_service_map_collect(udid), timeout=120)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


# ─── ALS / LIVE-IDENTITY MULTI-CHANNEL PROBE (Faze 2) ────────────────────────
# READ-ONLY. Zkusi kanaly, ktere jsme JESTE nevyuzili a maji sanci vydat live
# identitu komponent (napr. ALS 0311133F6B07): diag.info, cileny mobilegestalt,
# get_battery/get_wifi, read-only handshake com.apple.atc a kratky odposlech
# syslogu. VSE se prozene reference-matcherem proti zadanym hodnotam (?refs=).
#
# BEZPECNOST: NIKDY nevolame mutujici metody (action/restart/shutdown/sleep).
# ATC a syslog se jen OTEVROU a PASIVNE ctou s kratkym timeoutem - zadne
# prikazy se do nich neposilaji, nic se na telefonu nemeni.
_MG_SENSOR_KEYS = (
    "AmbientLightSensorCapability", "ProximitySensorCapability",
    "DisplaySerialNumber", "PanelSerialNumber", "DeviceColor",
    "FrontFacingCameraModuleSerialNumber", "RearFacingCameraModuleSerialNumber",
    "SensorHubSerialNumber", "ModuleSerialNumber", "MLBSerialNumber",
    "SerialNumber", "DiagData",
)

async def _als_channels_collect(udid, refs=None):
    import inspect
    ld, diag = await _open_diag(udid)
    leaves = []
    channels = []

    async def _maybe(v):
        return await v if inspect.isawaitable(v) else v

    def take(name, obj, err=None):
        ok = isinstance(obj, (dict, list, tuple)) and bool(obj)
        rec = {"channel": name, "ok": ok}
        if err:
            rec["error"] = err
        channels.append(rec)
        if ok:
            _fs_walk(f"channel:{name}", obj, leaves)
        elif obj not in (None, {}, [], ""):
            # skalar / string vysledek taky zaznamenej
            _fs_walk(f"channel:{name}", {"value": obj}, leaves)

    # 1) diag.info() - read-only souhrn (nikdy jsme nevolali)
    fn = getattr(diag, "info", None)
    if callable(fn):
        try:
            take("diag.info", await _maybe(fn()))
        except Exception as e:
            take("diag.info", None, f"{type(e).__name__}: {e}")

    # 2) mobilegestalt na cilene senzorove klice (read-only)
    fn = getattr(diag, "mobilegestalt", None)
    if callable(fn):
        try:
            try:
                obj = fn(keys=list(_MG_SENSOR_KEYS))
            except TypeError:
                obj = fn(list(_MG_SENSOR_KEYS))
            take("diag.mobilegestalt", await _maybe(obj))
        except Exception as e:
            take("diag.mobilegestalt", None, f"{type(e).__name__}: {e}")

    # 3) get_battery / get_wifi (read-only)
    for m in ("get_battery", "get_wifi"):
        fn = getattr(diag, m, None)
        if callable(fn):
            try:
                take(f"diag.{m}", await _maybe(fn()))
            except Exception as e:
                take(f"diag.{m}", None, f"{type(e).__name__}: {e}")

    # zavri diagnostics
    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    # 4) com.apple.atc - POUZE otevrit + pasivne precist pripadny banner, zavrit.
    #    Zadne prikazy se neposilaji (AST je citliva zona).
    start_fn = getattr(ld, "start_lockdown_service", None) or getattr(ld, "start_service", None)
    if callable(start_fn):
        atc = None
        try:
            atc = start_fn("com.apple.atc")
            if inspect.isawaitable(atc):
                atc = await atc
            banner = None
            for rm in ("recv", "receive", "read"):
                rfn = getattr(atc, rm, None)
                if callable(rfn):
                    try:
                        # kratky, neblokujici pokus o precteni uvodniho banneru
                        if hasattr(atc, "settimeout"):
                            try:
                                atc.settimeout(2.0)
                            except Exception:
                                pass
                        data = rfn(4096) if rm != "read" else rfn(4096)
                        if inspect.isawaitable(data):
                            data = await data
                        banner = data
                    except Exception:
                        pass
                    break
            take("atc.banner", {"raw": banner.hex() if isinstance(banner, (bytes, bytearray)) else banner}
                 if banner else {}, None if banner else "no passive banner")
        except Exception as e:
            take("atc.open", None, f"{type(e).__name__}: {e}")
        finally:
            try:
                closer = getattr(atc, "close", None)
                if callable(closer):
                    r = closer()
                    if inspect.isawaitable(r):
                        await r
            except Exception:
                pass

    # 5) syslog - kratky pasivni odposlech (~3s), grep serial-like / refs
    ref_list = [r.strip() for r in (refs or []) if r and r.strip()]
    ref_norms = [re.sub(r"[^A-Za-z0-9]", "", r).upper() for r in ref_list]
    syslog_hits = []
    if callable(start_fn):
        import time as _t
        svc = None
        try:
            svc = start_fn("com.apple.syslog_relay")
            if inspect.isawaitable(svc):
                svc = await svc
            if hasattr(svc, "settimeout"):
                try:
                    svc.settimeout(1.0)
                except Exception:
                    pass
            rfn = getattr(svc, "recv", None) or getattr(svc, "read", None)
            deadline = _t.time() + 3.0
            buf = b""
            while _t.time() < deadline and rfn:
                try:
                    chunk = rfn(4096)
                    if inspect.isawaitable(chunk):
                        chunk = await chunk
                    if not chunk:
                        break
                    buf += chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
                except Exception:
                    break
            text = buf.decode("utf-8", errors="ignore")
            up = re.sub(r"[^A-Za-z0-9]", "", text).upper()
            for r, rn in zip(ref_list, ref_norms):
                if rn and rn in up:
                    syslog_hits.append(r)
            channels.append({"channel": "syslog(3s)", "ok": bool(text),
                             "bytes": len(buf), "ref_hits": syslog_hits})
        except Exception as e:
            channels.append({"channel": "syslog(3s)", "ok": False, "error": f"{type(e).__name__}: {e}"})
        finally:
            try:
                closer = getattr(svc, "close", None)
                if callable(closer):
                    r = closer()
                    if inspect.isawaitable(r):
                        await r
            except Exception:
                pass

    # reference matching pres vse nasbirane
    ref_forms = {r: _fs_ref_forms(r) for r in ref_list}
    ref_hits = _fs_match_all(leaves, ref_forms) if ref_forms else []
    tf = {r: _fs_transform_forms(r, deep=True) for r in ref_list}
    tf_hits = _fs_match_transforms(leaves, tf) if tf else []
    found = sorted(set([h["ref"] for h in ref_hits] + [h["ref"] for h in tf_hits] + syslog_hits))

    return {
        "ok": True, "udid": udid, "probe": "als-channels",
        "channels": channels,
        "leaf_count": len(leaves),
        "refs": ref_list,
        "ref_found": found,
        "ref_missing": [r for r in ref_list if r not in set(found)],
        "ref_hits": ref_hits[:100],
        "transform_hits": tf_hits[:100],
        "syslog_ref_hits": syslog_hits,
    }

@app.route('/api/als-channels/<udid>', methods=['GET'])
def api_als_channels(udid):
    refs_arg = request.args.get('refs')
    refs = [r for r in refs_arg.split(',')] if refs_arg else None
    try:
        result = _run_async_isolated(_als_channels_collect(udid, refs=refs), timeout=120)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


# ─── FACTORY-CHECK (Varianta B feasibility) ──────────────────────────────────
# READ-ONLY. Odpovi na otazku: existuje pro danou komponentu NEZAVISLA tovarni
# reference, se kterou lze porovnavat aktualni hodnotu? Pro kazdou komponentu:
#  - precte AKTUALNI serial (zive z uzlu dilu),
#  - najde VSECHNY vyskyty toho serialu napric vsemi zdroji (vc. SysCfg/NVRAM/
#    Lockdown domen z full-scanu),
#  - rozhodne, zda se serial nachazi i v TOVARNIM/nezavislem zdroji (ne jen v
#    zivem uzlu dilu). Pokud ano -> varianta B je pro tu komponentu mozna
#    (ten zdroj se pri vymene dilu nezmeni -> detekce originality). Pokud se
#    serial vyskytuje JEN v zivem uzlu -> tovarni reference neexistuje -> pro tu
#    komponentu B nejde a je potreba fallback (C = baseline pri prijmu).
_FACTORY_SOURCE_HINTS = ("syscfg", "nvram", "diagnosticdata", "lockdown:domain",
                         "lockdown:all_values", "gestalt", "AppleDiagnosticData")
_LIVE_SOURCE_HINTS = ("AppleH1", "CamIn", "PearlCam", "CLCD", "SmartBattery",
                      "Prox", "als", "Haptics", "camera", "display", "battery")

def _fc_classify_source(source):
    s = (source or "").lower()
    if any(h.lower() in s for h in _FACTORY_SOURCE_HINTS):
        return "factory"
    return "live"

async def _factory_check_collect(udid):
    # 1) aktualni hodnoty komponent
    sources = await _read_hw_sources(udid)
    comp = await _component_serials_collect(udid, sources=sources, apply_baseline=False)
    components = comp.get("components", {})

    # 2) kompletni sada listu ze vsech zdroju (vc. SysCfg/NVRAM/domen)
    fs = await _full_scan_collect(udid, refs=None, save=False,
                                  transforms=False, deep=False, return_leaves=True)
    leaves = fs.get("_all_leaves", []) or []

    # index: normalizovany serial -> [(source, path, klasifikace)]
    from collections import defaultdict
    idx = defaultdict(list)
    for lf in leaves:
        t = lf.get("text")
        if not t:
            continue
        key = re.sub(r"[^A-Za-z0-9]", "", t).upper()
        idx[key].append((lf.get("source"), lf.get("path"), _fc_classify_source(lf.get("source"))))

    report = {}
    for ckey, c in components.items():
        cur = c.get("current_value")
        entry = {"label": c.get("label"), "current_value": cur,
                 "occurrences": [], "factory_reference": False,
                 "factory_source": None, "variant_B_possible": False}
        if cur:
            occ = idx.get(re.sub(r"[^A-Za-z0-9]", "", cur).upper(), [])
            entry["occurrences"] = [{"source": s, "path": p, "type": t} for (s, p, t) in occ]
            fac = [(s, p) for (s, p, t) in occ if t == "factory"]
            if fac:
                entry["factory_reference"] = True
                entry["factory_source"] = {"source": fac[0][0], "path": fac[0][1]}
                entry["variant_B_possible"] = True
        report[ckey] = entry

    possible = [k for k, v in report.items() if v["variant_B_possible"]]
    return {
        "ok": True, "udid": udid, "probe": "factory-check",
        "note": ("variant_B_possible=true znamena, ze serial komponenty byl "
                 "nalezen i v tovarnim/nezavislem zdroji (napr. SysCfg/NVRAM/"
                 "Lockdown) - tam se pri vymene dilu nezmeni, takze jde delat "
                 "tovarni vs aktualni porovnani. false = serial je jen v zivem "
                 "uzlu dilu -> pro tu komponentu B nejde, nutny fallback C."),
        "leaves_indexed": len(leaves),
        "components": report,
        "variant_B_components": possible,
        "variant_B_count": len(possible),
    }

@app.route('/api/factory-check/<udid>', methods=['GET'])
def api_factory_check(udid):
    try:
        result = _run_async_isolated(_factory_check_collect(udid), timeout=180)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200




async def _read_ioreg(diag, label, target, errors):
    """Cte IOKit uzel. Zkousi POTVRZENOU formu name= (V14 overil name='AppleCLCD'),
    pak fallback na plane+ioclass. Vraci prvni neprazdny vysledek."""
    import inspect
    attempts = [
        {"name": target},
        {"plane": "IOService", "ioclass": target},
        {"plane": "IOService", "name": target},
    ]
    last_err = None
    for kw in attempts:
        try:
            result = diag.ioregistry(**kw)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return result
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    if last_err:
        errors[label] = last_err
    return {}

async def _read_hw_sources(udid):
    """V28 cross-generation reader. Reads known nodes plus the complete IOService
    tree once, then lets the component normalizer search both exact Apple keys and
    semantic paths. READ-ONLY; no reference serial is ever returned as a value."""
    import inspect
    errors = {}
    ld, diag = await _open_diag(udid)

    values = {}
    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        if isinstance(av, dict):
            values = av
    except Exception as exc:
        errors["lockdown"] = f"{type(exc).__name__}: {exc}"

    async def read_all(label, targets):
        # CROSS-GENERATION: nevybirej prvni truthy node a nezahazuj ostatni.
        # Ruzne generace maji ruzne nody (napr. XS=AppleH10CamIn vs
        # 15 Pro=AppleH16CamIn; haptics Callan vs LEAP). Nacteme VSECHNY dostupne
        # kandidatni nody a vratime je jako seznam. _v29_exact_find /
        # _component_serial_find prochazeji seznamy rekurzivne, takze exact
        # property lookup najde spravnou hodnotu bez ohledu na generaci.
        collected = []
        for target in targets:
            obj = await _read_ioreg(diag, f"{label}:{target}", target, errors)
            if obj:
                collected.append(obj)
        return collected

    camera = await read_all("camera", ("AppleH10CamIn", "AppleH13CamIn", "AppleH16CamIn", "AppleCameraInterface"))
    pearl = await read_all("pearl", ("AppleH10PearlCam", "ApplePearlCam", "PearlCam", "AppleH10Pearl"))
    clcd = await read_all("display", ("AppleCLCD", "AppleCLCD2", "AppleDCP", "AppleMobileCLCD", "AppleM2ScalerCSCDriver"))
    battery = await read_all("battery", ("AppleSmartBattery", "AppleARMPMUCharger"))
    proximity = await read_all("proximity", ("AppleProxHIDEventDriver", "prox", "AppleProxDriver", "AppleProximitySensor"))
    als = await read_all("ambient_light", ("als", "AppleALSDriver", "AppleAmbientLightSensor", "AppleHIDALSService"))
    vibrator = await read_all("vibrator", ("AppleHapticsSupportCallan", "AppleHapticsSupportLEAP", "AppleTapticEngine", "AppleHaptics", "Actuator"))
    nand = await read_all("nand", ("AppleANS2NVMeController", "AppleNANDConfigAccess", "AppleANS2Controller", "AppleEmbeddedNVMeController"))

    io_service = {}
    try:
        io_service = diag.ioregistry(plane="IOService")
        if inspect.isawaitable(io_service):
            io_service = await io_service
        if not isinstance(io_service, (dict, list, tuple)):
            io_service = {}
    except Exception as exc:
        errors["IOService"] = f"{type(exc).__name__}: {exc}"

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    return {"values": values, "camera": camera, "pearl": pearl,
            "clcd": clcd, "battery": battery, "proximity": proximity,
            "als": als, "vibrator": vibrator, "nand": nand,
            "io_service": io_service, "errors": errors}


def _v29_norm_key(key):
    return str(key).lower().replace("-", "").replace("_", "").replace(" ", "")

def _v29_serial_like(value):
    scalar = _component_serial_scalar(value)
    if not scalar or len(scalar) < 6 or len(scalar) > 96:
        return None
    if scalar.lower() in ("true", "false", "none", "null", "unknown", "n/a"):
        return None
    # Placeholder / nečitelná hodnota: samé X, samé 0, samé stejné znaky
    # (napr. "XXXXXXXX", "00000000") -> NENI platny serial; komponenta se
    # pak vyhodnoti jako zavada (POSSIBLE_FAULT), ne jako Shodne.
    if len(set(scalar.upper())) <= 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9:+._\-/]+", scalar):
        return None
    return scalar

def _v29_exact_find(value, exact_keys):
    """Strict lookup: only an exact Apple property name is accepted."""
    wanted = {_v29_norm_key(k) for k in exact_keys}
    hits = []
    def walk(obj, path="$", depth=0):
        if depth > 80:
            return
        if isinstance(obj, dict):
            for key, child in obj.items():
                p = f"{path}.{key}"
                if _v29_norm_key(key) in wanted:
                    scalar = _v29_serial_like(child)
                    if scalar:
                        hits.append((p, str(key), scalar))
                walk(child, p, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for i, child in enumerate(obj):
                walk(child, f"{path}[{i}]", depth + 1)
    walk(value)
    if not hits:
        return None, None
    hits.sort(key=lambda x: (len(x[2]), x[0]))
    path, key, scalar = hits[0]
    return scalar, {"mode": "exact", "path": path, "key": key}

def _v29_source_find(source_map, allowed_sources, exact_keys):
    """Never search lockdown/all merged trees for component serials."""
    for source_name in allowed_sources:
        source = source_map.get(source_name) or {}
        value, meta = _v29_exact_find(source, exact_keys)
        if value:
            meta["source"] = source_name
            return value, meta
    return None, None

# Panel_ID je dlouhy zretezeny blob (napr. na XS ~250 znaku:
# "FXT83453QNWJNK593+11015094666161076746750913+...coverglass-serial-number").
# _v29_serial_like ma tvrdy cap 96 znaku, takze cely Panel_ID zahodi JESTE
# PREDTIM, nez se stihne oriznout na prvni segment (= display serial). Proto
# ma display vlastni finder, ktery bere raw hodnotu bez capu, orizne na prvni
# segment a az potom validuje. Nikdy nesahame do lockdownu (jen display/IOService).
_PANEL_KEYS = ("Panel_ID", "PanelID", "DisplaySerialNumber",
               "ScreenSerialNumber", "PanelSerialNumber")

def _panel_first_segment(scalar):
    if not scalar:
        return None
    first = re.split(r"[\s,;|:/+]+", str(scalar).strip(), maxsplit=1)[0].strip()
    if not first or len(first) < 6 or len(first) > 96:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", first):
        return None
    return first

def _v29_panel_find(source):
    wanted = {_v29_norm_key(k) for k in _PANEL_KEYS}
    hits = []
    def walk(obj, path="$", depth=0):
        if depth > 80:
            return
        if isinstance(obj, dict):
            for key, child in obj.items():
                p = f"{path}.{key}"
                if _v29_norm_key(key) in wanted:
                    seg = _panel_first_segment(_component_serial_scalar(child))
                    if seg:
                        hits.append((p, str(key), seg))
                walk(child, p, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for i, child in enumerate(obj):
                walk(child, f"{path}[{i}]", depth + 1)
    walk(source)
    if not hits:
        return None, None
    hits.sort(key=lambda x: (len(x[2]), x[0]))
    path, key, seg = hits[0]
    return seg, {"mode": "panel_id", "path": path, "key": key}


def _iphone_major(product_type):
    """Vytahne interni major verzi z ProductType 'iPhoneNN,M' -> NN."""
    m = re.match(r"iPhone(\d+),", str(product_type or ""))
    return int(m.group(1)) if m else None

# ── PROXIMITY SLOT: generacni routing ────────────────────────────────────────
# Jeden fyzicky slot v UI ("Distance Sensor"), ale jeho VYZNAM se lisi generaci:
#
#  • iPhone 12 a nizsi (interne <= iPhone13,x): samostatny DISTANCE / proximity
#    IC. Potvrzeny zdroj: AppleProxHIDEventDriver -> SerialNumber (XS overeno,
#    napr. FWP8311CBQ1H6CW20).
#
#  • iPhone 13 a vyssi (interne >= iPhone14,x): samostatny distance senzor uz
#    NENI. Proximity/priblizeni resi TrueDepth a hodnota, ktera nas zajima, sedi
#    na PREDNIM FLEXU (proximity/ALS assembly). Cislo je dulezite kvuli parovani
#    displeje (True Tone / auto-jas) - proto ho chceme vypisovat. JCID tuto
#    hodnotu cte (napr. J143292679B1P1W1E na 15 Pro), ale PRESNY IORegistry uzel
#    zatim NEMAME potvrzeny -> cteme z kandidatnich uzlu; dokud se netrefime,
#    hlasime "unavailable" (nikdy nehadame, u parovani displeje je falesna
#    hodnota nebezpecna). Uzel se potvrdi pres /api/prox-discovery.
_PROX_SLOT_KEY = "distance_sensor"

_PROX_VARIANT_DISTANCE = {
    "variant": "distance_sensor",
    "label": "Distance Sensor",
    "sources": ("proximity", "IOService"),
    "keys": ("DistanceSensorSerialNumber", "ProximitySensorSerialNumber",
             "DistSensSerialNumber", "ProxSensorSerialNumber", "SerialNumber"),
    "note": None,
}
_PROX_VARIANT_FRONTFLEX = {
    "variant": "front_flex_proximity",
    "label": "Proximity senzor (přední flex)",
    # POTVRZENO full-scanem na iPhone 15 Pro: proximity flex cislo (JCID "Radar")
    # lezi v AppleH16CamIn -> JasperSNUM = J143292679B1P1W1E. "Jasper" je Apple
    # kodove jmeno pro TrueDepth/flood-illuminator assembly; na 13+ (bez
    # samostatneho distance IC) je toto to relevantni cislo predniho flexu.
    # JasperSNUM je proto prvni (potvrzeny) klic; ostatni zustavaji jako fallback
    # pro pripadne jine generace. SerialNumber zamerne NENI (byl by device SN).
    "sources": ("camera", "proximity", "pearl", "IOService"),
    "keys": ("JasperSNUM",
             "ProximitySensorSerialNumber", "ProxSensorSerialNumber",
             "FrontProximitySerialNumber", "FrontSensorSerialNumber",
             "FlexSerialNumber", "ModuleSerial"),
    "note": "Číslo předního flexu (proximity) – důležité kvůli párování displeje",
}

def _prox_variant_for(product_type):
    major = _iphone_major(product_type)
    if major is not None and major >= 14:      # iPhone 13 a vyssi
        return _PROX_VARIANT_FRONTFLEX
    return _PROX_VARIANT_DISTANCE               # iPhone 12 a nizsi (a neznama gen.)

# Generacni dostupnost komponent. NEJDE o cteni ze zarizeni, ale o tom, zda
# komponenta na dane generaci FYZICKY existuje (rozlisi "unavailable" od
# "not_applicable"). Proximity slot je vzdy applicable - jen meni variantu
# (distance vs. front flex), takze se zde neresi.
def _component_applicable(comp_key, product_type):
    return True

async def _component_serials_collect(udid, sources=None, apply_baseline=True):
    if sources is None:
        sources = await _read_hw_sources(udid)

    values = sources.get("values", {})
    errors = dict(sources.get("errors", {}))

    # V29: component sources are isolated. Lockdown is intentionally excluded
    # from component serial lookup because SerialNumber there is the DEVICE SN.
    source_map = {
        "camera": sources.get("camera", {}),
        "pearl": sources.get("pearl", {}),
        "display": sources.get("clcd", {}),
        "battery": sources.get("battery", {}),
        "proximity": sources.get("proximity", {}),
        "als": sources.get("als", {}),
        "vibrator": sources.get("vibrator", {}),
        "nand": sources.get("nand", {}),
        "IOService": sources.get("io_service", {}),
        # Lockdown je zpristupnen VYHRADNE pro mainboard (exact klic MLBSerialNumber).
        # Nikdy se z nej nehleda generic SerialNumber (to je device SN) - zadny
        # component spec nema "lockdown" ve svych povolenych zdrojich krome desky
        # a MLBSerialNumber neni nikdy shodne s device serialem.
        "lockdown": values,
    }

    specs = {
        "rear_camera": (
            ("camera", "IOService"),
            ("BackCameraModuleSerialNumString", "RearCameraModuleSerialNumString",
             "BackCameraSerialNumber", "RearCameraSerialNumber")),
        "front_camera": (
            ("camera", "IOService"),
            ("FrontCameraModuleSerialNumString", "FrontCameraSerialNumber")),
        "tele_camera": (
            ("camera", "IOService"),
            ("TeleCameraModuleSerialNumString", "BackTeleCameraModuleSerialNumString",
             "TelephotoCameraModuleSerialNumString", "TeleCameraSerialNumber")),
        "ultrawide_camera": (
            ("camera", "IOService"),
            # 15 Pro pouziva "SuperWide" misto "UltraWide" (potvrzeno V30:
            # BackSuperWideCameraModuleSerialNumString = DN83301G7NR1V6N4A).
            # Reader musi podporovat obe varianty.
            ("BackSuperWideCameraModuleSerialNumString", "SuperWideCameraModuleSerialNumString",
             "BackUltraWideCameraModuleSerialNumString", "UltraWideCameraModuleSerialNumString",
             "UltraWideCameraSerialNumber")),
        "front_ir_camera": (
            ("pearl", "camera", "IOService"),
            ("FrontIRCameraModuleSerialNumString", "IRCameraModuleSerialNumString",
             "FrontIRCameraSerialNumber")),
        "true_depth_projector": (
            ("pearl", "camera", "IOService"),
            ("FrontIRStructuredLightProjectorSerialNumString",
             "StructuredLightProjectorModuleSerialNumString",
             "DotProjectorSerialNumString", "ProjectorModuleSerialNumString")),
        # distance_sensor NENI ve specs - je routovan generacne (viz nize).
        "ambient_light_sensor": (
            ("als", "IOService"),
            ("AmbientLightSensorSerialNumber", "ALSSerialNumber",
             "AmbientLightSerialNumber", "ModuleSerial", "SerialNumber")),
        "screen": (
            ("display", "IOService"),
            ("Panel_ID", "PanelID", "DisplaySerialNumber", "ScreenSerialNumber",
             "PanelSerialNumber")),
        "battery": (
            ("battery",),
            ("BatterySerialNumber", "SerialNumber", "Serial")),
        "mainboard": (
            # MLB je potvrzene v Lockdownu jako MLBSerialNumber (XS: F3X8405016AJVN7A,
            # 15 Pro: G2CGY200A6F00003PP). IOService jen jako fallback.
            ("lockdown", "IOService"),
            ("MLBSerialNumber", "LogicBoardSerialNumber", "MainboardSerialNumber")),
        "taptic_engine": (
            # Potvrzeno v handoffu: AppleHapticsSupportCallan (starsi gen.) /
            # AppleHapticsSupportLEAP (15 Pro) -> ModuleSerial. Zdroj "vibrator"
            # uz je v _read_hw_sources cten, jen se dosud nepouzival ve specs.
            ("vibrator",),
            ("ModuleSerial", "ModuleSerialNumber", "VibratorSerialNumber",
             "VibratorNumber", "TapticEngineSerialNumber", "HapticSerialNumber",
             "RosalineSerialNumber")),
        "nand": (
            # Best-effort: NAND uzly (AppleANS2NVMeController apod.) na mnoha
            # generacich/iOS verzich nevraci pojmenovany vlastni objekt (fallback
            # vraci obecny IOService strom) - proto castokrat zustane "unavailable".
            # To NENI chyba readeru, je to limit toho, co je pres IORegistry videt.
            ("nand",),
            ("Serial", "SerialNumber", "PartNumber", "FlashID", "DeviceID")),
    }

    raw, discovery = {}, {}
    device_serial = str(values.get("SerialNumber") or "").strip()
    device_product_type = str(values.get("ProductType") or "")

    for key, (allowed_sources, exact_keys) in specs.items():
        if key == "screen":
            # Displej ma vlastni finder (Panel_ID je moc dlouhy na _v29_serial_like).
            value, meta = None, None
            for sname in allowed_sources:
                v, m = _v29_panel_find(source_map.get(sname) or {})
                if v:
                    m["source"] = sname
                    value, meta = v, m
                    break
        else:
            value, meta = _v29_source_find(source_map, allowed_sources, exact_keys)

        # Hard guard: a component value may never silently equal device SN.
        if value and device_serial and value.upper() == device_serial.upper():
            errors[f"{key}:rejected"] = (
                f"Rejected device SerialNumber false-positive from {meta.get('source')}: "
                f"{meta.get('path')}"
            )
            value, meta = None, None

        raw[key] = value
        if meta:
            discovery[key] = meta

    # ── PROXIMITY SLOT (generacne routovany: distance vs. predni flex) ──
    prox_variant = _prox_variant_for(device_product_type)
    pv_value, pv_meta = _v29_source_find(
        source_map, prox_variant["sources"], prox_variant["keys"])
    if pv_value and device_serial and pv_value.upper() == device_serial.upper():
        errors[f"{_PROX_SLOT_KEY}:rejected"] = (
            f"Rejected device SerialNumber false-positive from {pv_meta.get('source')}: "
            f"{pv_meta.get('path')}"
        )
        pv_value, pv_meta = None, None
    raw[_PROX_SLOT_KEY] = pv_value
    if pv_meta:
        discovery[_PROX_SLOT_KEY] = pv_meta

    # These are device connectivity identifiers, not component serial guesses.
    raw.update({
        "wifi": _component_serial_find(values, ("WiFiAddress", "WifiAddress")),
        "bluetooth": _component_serial_find(values, ("BluetoothAddress",)),
        "cellular": _component_serial_find(
            values, ("InternationalMobileEquipmentIdentity", "MobileEquipmentIdentifier")
        ),
    })

    components = {}
    for key, label in _COMPONENT_SERIAL_LABELS.items():
        value = raw.get(key)
        applicable = _component_applicable(key, device_product_type)
        # POZOR na zobrazeni: frontend (renderComponentCard) prepne na verifikacni
        # kartu s radky "Tovarni:/Aktualni:" JAKMILE polozka obsahuje pole "status".
        # Bez tovarni databaze Apple by "Tovarni:" byl vzdy "-" = prazdna kolonka.
        # Proto ZAMERNE NEPOSILAME "status" -> frontend pouzije jednoduchou kartu,
        # ktera ukaze velkou hodnotu (nebo "nedostupné"), bez prazdnych radku.
        # Strojove citelny stav zustava v "read_state". Az bude tovarni DB, staci
        # zacit posilat "status"+"factory_value" a rozsviti se verifikacni rezim.
        item = {"key": key, "label": label, "value": value,
                "available": bool(value), "applicable": applicable,
                "current_value": value, "factory_value": None, "match": None}
        # Proximity slot: prepis label/variant/note podle generace zarizeni.
        if key == _PROX_SLOT_KEY:
            item["label"] = prox_variant["label"]
            item["variant"] = prox_variant["variant"]
            if prox_variant["note"]:
                item["note"] = prox_variant["note"]
        if not applicable:
            item["read_state"] = "not_applicable"
            item["note"] = "Tato generace tuto komponentu nemá"
        elif value:
            item["read_state"] = "read"
        else:
            item["read_state"] = "unavailable"
        if key in discovery:
            item["discovery"] = discovery[key]
        components[key] = item

    # ── TOVARNI REFERENCE (baseline): doplni factory_value + status
    # (MATCH/MISMATCH/POSSIBLE_FAULT)
    baseline_meta = None
    if apply_baseline:
        try:
            components, baseline_meta = _apply_baseline(udid, components)
        except Exception as e:
            errors["baseline"] = f"{type(e).__name__}: {e}"

    groups = []
    for group_key, spec in _COMPONENT_SERIAL_GROUPS.items():
        group_components = [components[key] for key in spec["components"] if key in components]
        groups.append({
            "key": group_key,
            "label": spec["label"],
            "components": group_components,
            "available_count": sum(1 for item in group_components if item["available"]),
            "applicable_count": sum(
                1 for item in group_components if item.get("applicable", True)),
        })

    return {
        "ok": True,
        "reader": "strict-source-isolated-v29.7-possiblefault",
        "udid": udid,
        "components": components,
        "groups": groups,
        "baseline": baseline_meta,
        "summary": {
            "components_total": len(components),
            "components_available": sum(1 for x in components.values() if x["available"]),
            "components_not_applicable": sum(
                1 for x in components.values() if not x.get("applicable", True)),
            "components_mismatch": sum(
                1 for x in components.values() if x.get("status") == "MISMATCH"),
            "components_possible_fault": sum(
                1 for x in components.values() if x.get("status") == "POSSIBLE_FAULT"),
            "exact_matches": len(discovery),
            "device_serial_guard": True,
        },
        "errors": errors,
    }

@app.route('/api/component-serials/<udid>', methods=['GET'])
def api_component_serials(udid):
    try:
        result = _run_async_isolated(_component_serials_collect(udid), timeout=90)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


@app.route('/api/baseline/<udid>', methods=['GET'])
def api_baseline_get(udid):
    """Zobrazi ulozenou tovarni referenci (baseline) pro dane zarizeni."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT component_key, factory_value, captured_at, captured_by "
            "FROM component_baseline WHERE udid=? ORDER BY component_key", (udid,)).fetchall()
        conn.close()
        return jsonify({'ok': True, 'udid': udid, 'count': len(rows),
                        'baseline': [dict(r) for r in rows]}), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200

@app.route('/api/set-baseline/<udid>', methods=['GET', 'POST'])
def api_set_baseline(udid):
    """Prepise tovarni referenci AKTUALNE pripojenymi hodnotami (golden reset).
    Pouzij, kdyz mas telefon v overenem stavu a chces ho ulozit jako referencni."""
    by = request.args.get('by', '') or (request.json.get('by', '') if request.is_json else '')
    try:
        sources = _run_async_isolated(_read_hw_sources(udid), timeout=90)
        comp = _run_async_isolated(
            _component_serials_collect(udid, sources=sources, apply_baseline=False), timeout=30)
        kv = {k: (v.get("current_value") or v.get("value"))
              for k, v in comp.get("components", {}).items()
              if v.get("applicable", True) and (v.get("current_value") or v.get("value"))}
        _baseline_delete(udid)          # golden reset = zahodit stare a ulozit nove
        _baseline_set_many(udid, kv, by=by)
        return jsonify({'ok': True, 'udid': udid, 'captured': sorted(kv.keys()),
                        'count': len(kv)}), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200

@app.route('/api/baseline/<udid>', methods=['DELETE'])
def api_baseline_delete(udid):
    ok = _baseline_delete(udid)
    return jsonify({'ok': ok, 'udid': udid}), 200


# ─── PROXIMITY / FRONT-FLEX DISCOVERY PROBE ──────────────────────────────────
# READ-ONLY. Slouzi k dohledani PRESNEHO uzlu/klice, kde na iPhone 13+ lezi cislo
# proximity senzoru predniho flexu (JCID/3uTools ho cte, my zatim ne). Zadej
# znamou referencni hodnotu z JCID pres ?prox_ref=... a probe projde kandidatni
# uzly + cely IOService a nahlasi kazdy vyskyt (exact match) se source/path/key.
# Az bude potvrzeny, pridame ho do _PROX_VARIANT_FRONTFLEX["keys"]/["sources"].
_PROX_DISCOVERY_TARGETS = (
    "AppleProxHIDEventDriver", "prox", "AppleProxDriver", "AppleProximitySensor",
    "AppleHIDALSService", "AppleSPU", "AppleSPUHIDDriver",
    "AppleH10PearlCam", "AppleH13PearlCam", "AppleH16PearlCam", "ApplePearlCam",
    "AppleH10CamIn", "AppleH13CamIn", "AppleH16CamIn",
    "AppleAOP", "AppleAOPAudio", "AppleHIDEventDriver",
)

async def _prox_discovery_collect(udid, prox_ref=None):
    import inspect
    errors = {}
    calls = []
    hits = []
    ref_norm = re.sub(r"[^A-Za-z0-9]", "", str(prox_ref or "")).upper()

    def scan(source, obj, depth=0, path="$"):
        if depth > 80:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}"
                if isinstance(v, (dict, list, tuple)):
                    scan(source, v, depth + 1, p)
                    continue
                sc = _component_serial_scalar(v)
                if not sc:
                    continue
                sc_norm = re.sub(r"[^A-Za-z0-9]", "", sc).upper()
                # exact-hit vuci referenci, jinak jen serial-like leaf pro prehled
                if ref_norm and ref_norm in sc_norm:
                    hits.append({"source": source, "path": p, "key": str(k),
                                 "value": sc, "match": "exact"})
                elif not ref_norm and _v29_serial_like(v):
                    hits.append({"source": source, "path": p, "key": str(k),
                                 "value": sc, "match": "serial_like"})
        elif isinstance(obj, (list, tuple)):
            for i, c in enumerate(obj):
                scan(source, c, depth + 1, f"{path}[{i}]")

    ld, diag = await _open_diag(udid)
    for target in _PROX_DISCOVERY_TARGETS:
        src = f"ioregistry:name:{target}"
        try:
            obj = diag.ioregistry(name=target)
            if inspect.isawaitable(obj):
                obj = await obj
            ok = bool(obj)
            calls.append({"source": src, "ok": ok})
            if ok:
                scan(src, obj)
        except Exception as exc:
            calls.append({"source": src, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    # cely IOService strom jako zaloha
    try:
        io = diag.ioregistry(plane="IOService")
        if inspect.isawaitable(io):
            io = await io
        calls.append({"source": "ioregistry:plane:IOService", "ok": bool(io)})
        if io:
            scan("ioregistry:plane:IOService", io)
    except Exception as exc:
        calls.append({"source": "ioregistry:plane:IOService", "ok": False,
                      "error": f"{type(exc).__name__}: {exc}"})
    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    exact = [h for h in hits if h["match"] == "exact"]
    return {
        "ok": True, "udid": udid, "probe": "prox-front-flex-discovery",
        "prox_ref": prox_ref, "exact_hits": exact,
        "exact_count": len(exact),
        "serial_like_leaves": [h for h in hits if h["match"] == "serial_like"][:200],
        "calls": calls, "errors": errors,
    }

@app.route('/api/prox-discovery/<udid>', methods=['GET'])
def api_prox_discovery(udid):
    prox_ref = request.args.get('prox_ref')
    try:
        result = _run_async_isolated(_prox_discovery_collect(udid, prox_ref=prox_ref), timeout=120)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


# ─── RAW NODE DUMP ───────────────────────────────────────────────────────────
# READ-ONLY. Vypise syrovy obsah pojmenovanych IORegistry uzlu. Pouziti pro
# zjisteni, jak se na dane generaci/iOS jmenuje displejovy uzel a kde lezi
# Panel_ID (napr. na 15 Pro / iOS 26 AppleCLCD nevraci data). Priklad:
#   /api/node-dump/<udid>?names=AppleCLCD,AppleDCP,disp0,AppleH16PPipe,AppleMobileCLCD
_NODE_DUMP_DEFAULT = ("AppleCLCD", "AppleCLCD2", "AppleDCP", "AppleDCPDPTX",
                      "AppleM2ScalerCSCDriver", "disp0", "AppleMobileCLCD",
                      "IOMobileFramebuffer", "AppleH16PPipe", "AppleH13PPipe")

async def _node_dump_collect(udid, names):
    import inspect
    out = {}
    ld, diag = await _open_diag(udid)
    for target in names:
        rec = {"ok": False}
        try:
            obj = diag.ioregistry(name=target)
            if inspect.isawaitable(obj):
                obj = await obj
            rec["ok"] = bool(obj)
            rec["content"] = _v30_safe(obj) if obj else None
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
        out[target] = rec
    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass
    return {"ok": True, "udid": udid, "probe": "node-dump", "nodes": out}

@app.route('/api/node-dump/<udid>', methods=['GET'])
def api_node_dump(udid):
    names_arg = request.args.get('names')
    names = tuple(n.strip() for n in names_arg.split(',') if n.strip()) if names_arg else _NODE_DUMP_DEFAULT
    try:
        result = _run_async_isolated(_node_dump_collect(udid, names), timeout=120)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


# ─── FULL SCAN (V34) – kompletni lossless capture + reference matcher ─────────
# READ-ONLY. Projde VSECHNY dostupne zdroje v telefonu (Lockdown all_values +
# domeny, cely IORegistry po planech i pojmenovanych uzlech, MobileGestalt),
# zplosti kazdy list, ulozi vse 1:1 na disk (discovery_v34/<udid>_<ts>/) se
# SHA256 manifestem a - pokud zadas zname referencni hodnoty (?refs=a,b,c z
# JCID/3uTools) - nahlasi PRESNE, kde se kazda vyskytuje (literal + hex + reverse
# hex). Slouzi k discovery, NE jako produkcni ctecka. Hodnoty, ktere JCID
# dekoduje a nejsou v telefonu doslova (napr. ALS 0311133F6B07), zde nevyskoci -
# to je zamer: scan je rozlisi od tech, ktere jen cteme ze spatneho uzlu.
_FS_LOCKDOWN_DOMAINS = (
    "com.apple.mobile.battery", "com.apple.disk_usage", "com.apple.mobile.iTunes",
    "com.apple.mobile.internal", "com.apple.mobile.lockdown", "com.apple.mobile.gestalt",
    "com.apple.mobile.wireless_lockdown", "com.apple.international",
    "com.apple.mobile.chaperone", "com.apple.fmip", "com.apple.mobile.user_preferences",
    "com.apple.mobile.software_behavior", "com.apple.mobile.data_sync",
)
_FS_IOREG_NODES = (
    "AppleH10CamIn", "AppleH13CamIn", "AppleH16CamIn", "AppleCameraInterface",
    "AppleH10PearlCam", "AppleH13PearlCam", "AppleH16PearlCam", "ApplePearlCam",
    "AppleCLCD", "AppleCLCD2", "AppleDCP", "AppleDCPDPTX", "AppleMobileCLCD",
    "IOMobileFramebuffer", "AppleM2ScalerCSCDriver", "disp0", "AppleH16PPipe", "AppleH13PPipe",
    "AppleSmartBattery", "AppleARMPMUCharger",
    "AppleProxHIDEventDriver", "prox", "AppleProxDriver", "AppleProximitySensor",
    "als", "AppleALSDriver", "AppleAmbientLightSensor", "AppleHIDALSService",
    "AppleHapticsSupportCallan", "AppleHapticsSupportLEAP", "AppleTapticEngine",
    "AppleHaptics", "Actuator",
    "AppleSPU", "AppleSPUHIDDriver", "AppleAOP", "AppleAOPAudio", "AppleHIDEventDriver",
    "AppleSPUProxSensor", "AppleSPUProx", "colorsensor", "AppleColorSensor",
    "AppleAmbientLightSensorSPU", "AppleAOPVoiceTrigger",
    "AppleANS2NVMeController", "AppleNANDConfigAccess",
    "AppleDiagnosticDataSysCfg", "AppleDiagnosticData",
)
_FS_GESTALT_KEYS = (
    "AmbientLightSensorCalibration", "ProximitySensorCalibration", "DisplaySerialNumber",
    "PanelSerialNumber", "MLBSerialNumber", "DeviceColor", "DisplayColorSpace",
)

def _fs_norm(s):
    return re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()

def _fs_scalar(value):
    """Vrati (text_or_None, hex_or_None) pro list-leaf."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        txt = raw.decode("ascii", errors="ignore").strip("\x00").strip()
        return (txt or None, raw.hex())
    if isinstance(value, bool):
        return (str(value), None)
    if isinstance(value, (str, int, float)):
        s = str(value).strip()
        return (s or None, None)
    return (None, None)

def _fs_walk(source, obj, leaves, depth=0, path="$"):
    if depth > 100:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if isinstance(v, (dict, list, tuple)):
                _fs_walk(source, v, leaves, depth + 1, p)
            else:
                txt, hx = _fs_scalar(v)
                if txt is not None or hx is not None:
                    leaves.append({"source": source, "path": p, "key": str(k),
                                   "text": txt, "hex": hx})
    elif isinstance(obj, (list, tuple)):
        for i, c in enumerate(obj):
            _fs_walk(source, c, leaves, depth + 1, f"{path}[{i}]")

def _fs_ref_forms(ref):
    forms = set()
    r = str(ref).strip()
    n = _fs_norm(r)
    if n:
        forms.add(n)
    hexstr = re.sub(r"[^0-9A-Fa-f]", "", r)
    if len(hexstr) >= 4 and len(hexstr) % 2 == 0:
        try:
            b = bytes.fromhex(hexstr)
            forms.add(b.hex().upper())
            forms.add(b[::-1].hex().upper())
        except Exception:
            pass
    return forms

def _fs_match_all(leaves, ref_forms_map):
    hits = []
    for lf in leaves:
        leaf_norm = _fs_norm(lf.get("text") or "")
        leaf_hex = (lf.get("hex") or "").upper()
        for ref, forms in ref_forms_map.items():
            for f in forms:
                if (leaf_norm and f in leaf_norm) or (leaf_hex and f in leaf_hex):
                    hits.append({"ref": ref, "form": f, "source": lf["source"],
                                 "path": lf["path"], "key": lf["key"],
                                 "text": lf.get("text"), "hex": lf.get("hex")})
                    break
    return hits

def _fs_transform_forms(ref, deep=False):
    """label -> HEXUPPER pod reverzibilnimi transformacemi. Slouzi k hledani
    hodnot, ktere nejsou v telefonu doslova (napr. ALS 0311133F6B07), ale mohou
    byt ulozene prehazene/invertovane/jako okno uvnitr kalibracniho blobu."""
    out = {}
    r = str(ref).strip()
    def addhex(lbl, bb):
        if bb:
            out[lbl] = bytes(bb).hex().upper()
    hexstr = re.sub(r"[^0-9A-Fa-f]", "", r)
    if len(hexstr) >= 4 and len(hexstr) % 2 == 0:
        b = bytes.fromhex(hexstr)
        addhex("raw", b)
        addhex("reverse", b[::-1])
        addhex("invert", bytes((~x) & 0xFF for x in b))
        addhex("xorFF", bytes(x ^ 0xFF for x in b))
        addhex("nibble_swap", bytes(((x << 4 | x >> 4) & 0xFF) for x in b))
        addhex("bit_reverse", bytes(int(f"{x:08b}"[::-1], 2) for x in b))
        if len(b) >= 4:
            addhex("tail32_rev", b[:-4] + b[-4:][::-1])
            addhex("head4_rev", b[:4][::-1] + b[4:])
            addhex("swap_halves", b[len(b) // 2:] + b[:len(b) // 2])
        if deep:
            for rr in range(1, len(b)):
                addhex(f"rot{rr}", b[rr:] + b[:rr])
            for m in range(1, 256):
                addhex(f"xor{m:02x}", bytes(x ^ m for x in b))
    an = re.sub(r"[^A-Za-z0-9]", "", r).upper()
    if an:
        out["ascii"] = an
        out["ascii_rev"] = an[::-1]
    return out

def _fs_match_transforms(leaves, ref_tforms_map):
    hits = []
    for lf in leaves:
        leaf_hex = (lf.get("hex") or "").upper()
        leaf_norm = _fs_norm(lf.get("text") or "")
        if not leaf_hex and not leaf_norm:
            continue
        for ref, tforms in ref_tforms_map.items():
            for label, form in tforms.items():
                if form and ((leaf_hex and form in leaf_hex) or (leaf_norm and form in leaf_norm)):
                    hits.append({"ref": ref, "transform": label, "form": form,
                                 "source": lf["source"], "path": lf["path"],
                                 "key": lf["key"], "text": lf.get("text"),
                                 "hex": lf.get("hex")})
                    break
    return hits

async def _full_scan_collect(udid, refs=None, save=True, transforms=False, deep=False, return_leaves=False):
    import inspect, hashlib, json as _json, datetime as _dt
    calls = []
    raw_sources = {}
    leaves = []
    ld, diag = await _open_diag(udid)

    def record(source, obj, err=None):
        ok = bool(obj) and isinstance(obj, (dict, list, tuple))
        entry = {"source": source, "ok": ok}
        if err:
            entry["error"] = err
        calls.append(entry)
        if ok:
            raw_sources[source] = obj
            _fs_walk(source, obj, leaves)

    async def _maybe(v):
        return await v if inspect.isawaitable(v) else v

    # 1) lockdown all_values
    try:
        record("lockdown:all_values", await _maybe(ld.all_values))
    except Exception as e:
        record("lockdown:all_values", None, f"{type(e).__name__}: {e}")
    # 2) lockdown domains
    for dom in _FS_LOCKDOWN_DOMAINS:
        try:
            record(f"lockdown:domain:{dom}", await _maybe(ld.get_value(domain=dom)))
        except Exception as e:
            record(f"lockdown:domain:{dom}", None, f"{type(e).__name__}: {e}")
    # 3) ioregistry planes
    for plane in ("IOService", "IODeviceTree", "IOPower"):
        try:
            record(f"ioregistry:plane:{plane}", await _maybe(diag.ioregistry(plane=plane)))
        except Exception as e:
            record(f"ioregistry:plane:{plane}", None, f"{type(e).__name__}: {e}")
    # 4) ioregistry named nodes
    for name in _FS_IOREG_NODES:
        try:
            record(f"ioregistry:name:{name}", await _maybe(diag.ioregistry(name=name)))
        except Exception as e:
            record(f"ioregistry:name:{name}", None, f"{type(e).__name__}: {e}")
    # 5) mobilegestalt (defenzivne podle dostupne pymobiledevice3)
    for meth in ("mobilegestalt", "get_mobilegestalt", "query_mobilegestalt"):
        fn = getattr(diag, meth, None)
        if not fn:
            continue
        try:
            try:
                obj = fn(keys=list(_FS_GESTALT_KEYS))
            except TypeError:
                obj = fn(list(_FS_GESTALT_KEYS))
            record(f"diagnostics:{meth}", await _maybe(obj))
        except Exception as e:
            record(f"diagnostics:{meth}", None, f"{type(e).__name__}: {e}")

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    # reference matching
    ref_list = [r.strip() for r in (refs or []) if r and r.strip()]
    ref_forms_map = {r: _fs_ref_forms(r) for r in ref_list}
    ref_hits = _fs_match_all(leaves, ref_forms_map) if ref_forms_map else []
    found = sorted({h["ref"] for h in ref_hits})

    # rozsirene transformacni hledani (pro hodnoty, ktere nejsou v datech doslova)
    transform_hits = []
    transform_found = []
    if transforms and ref_list:
        ref_tforms_map = {r: _fs_transform_forms(r, deep=deep) for r in ref_list}
        transform_hits = _fs_match_transforms(leaves, ref_tforms_map)
        transform_found = sorted({h["ref"] for h in transform_hits})

    # serial-like leaves (rychly lidsky prehled)
    serial_like = []
    for lf in leaves:
        t = lf.get("text")
        if (t and 6 <= len(t) <= 96
                and re.fullmatch(r"[A-Za-z0-9:+._\-/]+", t)
                and t.lower() not in ("true", "false", "none", "null", "unknown")):
            serial_like.append(lf)

    saved_dir = None
    if save:
        try:
            base = _get_base_dir()
        except Exception:
            base = os.getcwd()
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_dir = os.path.join(base, "discovery_v34", f"{udid}_{ts}")
        try:
            os.makedirs(saved_dir, exist_ok=True)
            manifest = []
            for source, obj in raw_sources.items():
                safe = _v30_safe(obj, max_binary=8_000_000)  # bez orezu = lossless
                blob = _json.dumps(safe, ensure_ascii=False).encode("utf-8")
                fname = re.sub(r"[^A-Za-z0-9]+", "_", source)[:120] + ".json"
                with open(os.path.join(saved_dir, fname), "wb") as f:
                    f.write(blob)
                manifest.append({"source": source, "file": fname,
                                 "sha256": hashlib.sha256(blob).hexdigest(),
                                 "size": len(blob)})
            with open(os.path.join(saved_dir, "_leaves.json"), "w", encoding="utf-8") as f:
                _json.dump(leaves, f, ensure_ascii=False)
            with open(os.path.join(saved_dir, "_manifest.json"), "w", encoding="utf-8") as f:
                _json.dump({"udid": udid, "ts": ts, "sources": manifest,
                            "leaf_count": len(leaves), "refs": ref_list,
                            "ref_found": found}, f, ensure_ascii=False, indent=2)
            if ref_hits:
                with open(os.path.join(saved_dir, "_ref_hits.json"), "w", encoding="utf-8") as f:
                    _json.dump(ref_hits, f, ensure_ascii=False, indent=2)
            if transform_hits:
                with open(os.path.join(saved_dir, "_transform_hits.json"), "w", encoding="utf-8") as f:
                    _json.dump(transform_hits, f, ensure_ascii=False, indent=2)
        except Exception as e:
            calls.append({"source": "save", "ok": False, "error": f"{type(e).__name__}: {e}"})
            saved_dir = None

    result = {
        "ok": True, "udid": udid, "probe": "full-scan-v34",
        "sources_tried": len(calls),
        "sources_ok": sum(1 for c in calls if c.get("ok")),
        "leaf_count": len(leaves),
        "serial_like_count": len(serial_like),
        "refs": ref_list,
        "ref_found": found,
        "ref_missing": [r for r in ref_list if r not in set(found)],
        "ref_hits": ref_hits,
        "transforms_enabled": bool(transforms),
        "transform_deep": bool(deep),
        "transform_found": transform_found,
        "transform_hits": transform_hits[:500],
        "transform_hit_count": len(transform_hits),
        "calls": calls,
        "serial_like_sample": serial_like[:500],
        "saved_dir": saved_dir,
    }
    if return_leaves:
        result["_all_leaves"] = leaves   # interni pouziti (factory-check), neexponuje endpoint
    return result

@app.route('/api/full-scan/<udid>', methods=['GET'])
def api_full_scan(udid):
    refs_arg = request.args.get('refs')
    refs = [r for r in refs_arg.split(',')] if refs_arg else None
    save = request.args.get('save', '1') not in ('0', 'false', 'no')
    transforms = request.args.get('transforms', '0') in ('1', 'true', 'yes', 'on')
    deep = request.args.get('deep', '0') in ('1', 'true', 'yes', 'on')
    try:
        result = _run_async_isolated(
            _full_scan_collect(udid, refs=refs, save=save, transforms=transforms, deep=deep),
            timeout=300)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200


# ─── ALS DIFFERENTIAL ────────────────────────────────────────────────────────
# Porovna dva ulozene full-scan dumpy (dva ruzne telefony STEJNE generace s
# RUZNOU ALS hodnotou) a najde misto, kde se bajty meni SOUCASNE s ALS hodnotou.
# Princip: pro kazdy spolecny leaf hleda offset, kde telefon1 obsahuje formu
# als1 a telefon2 na STEJNEM offsetu formu als2 (pod stejnou transformaci) =
# to je zdroj, ze ktereho JCID ALS bere. Kdyz nic nekoreluje pod transformaci,
# vypise localizovane rozdilove okenka (bajty ktere se lisi) k rucni inspekci.
def _als_load_leaves(dir_path):
    import json as _json
    with open(os.path.join(dir_path, "_leaves.json"), "r", encoding="utf-8") as f:
        return _json.load(f)

def _als_find_latest_dir(udid):
    try:
        base = _get_base_dir()
    except Exception:
        base = os.getcwd()
    root = os.path.join(base, "discovery_v34")
    if not os.path.isdir(root):
        return None
    cands = sorted(d for d in os.listdir(root) if d.startswith(udid + "_"))
    return os.path.join(root, cands[-1]) if cands else None

def _als_index(leaves):
    idx = {}
    for lf in leaves:
        idx[(lf.get("source"), lf.get("path"), lf.get("key"))] = lf
    return idx

def _als_covary(h1, h2, f1, f2):
    """Offsety, kde h1 ma f1 a h2 ma f2 na STEJNE pozici (co-variace)."""
    hits = []
    if not (h1 and h2 and f1 and f2) or len(f1) != len(f2):
        return hits
    start = 0
    while True:
        i = h1.find(f1, start)
        if i < 0:
            break
        if h2[i:i + len(f2)] == f2:
            hits.append(i)
        start = i + 1
    return hits

def _als_forms(als, deep=True):
    """Transform-formy plne hodnoty i jejiho variabilniho ocasu (bez 1. bajtu),
    protoze ALS ma spolecny prefix (03..) a ulozene muze byt jen variabilni cast."""
    forms = dict(_fs_transform_forms(als, deep=deep))
    hexstr = re.sub(r"[^0-9A-Fa-f]", "", str(als))
    if len(hexstr) >= 4 and len(hexstr) % 2 == 0 and len(hexstr) > 2:
        tail = bytes.fromhex(hexstr)[1:]  # zahod prvni bajt (prefix)
        for lbl, val in _fs_transform_forms(tail.hex(), deep=deep).items():
            forms[f"tail_{lbl}"] = val
    return forms

def _als_differential(dir1, dir2, als1, als2, deep=True):
    l1, l2 = _als_load_leaves(dir1), _als_load_leaves(dir2)
    i1, i2 = _als_index(l1), _als_index(l2)
    common = set(i1) & set(i2)
    tf1, tf2 = _als_forms(als1, deep), _als_forms(als2, deep)
    shared_labels = set(tf1) & set(tf2)

    covary_hits = []
    diff_windows = []
    for k in common:
        a, b = i1[k], i2[k]
        h1 = (a.get("hex") or "").upper()
        h2 = (b.get("hex") or "").upper()
        # 1) transform co-variace na stejnem offsetu
        for label in shared_labels:
            for off in _als_covary(h1, h2, tf1[label], tf2[label]):
                covary_hits.append({"source": k[0], "path": k[1], "key": k[2],
                                    "transform": label, "offset_hex": off,
                                    "form1": tf1[label], "form2": tf2[label]})
        # 2) obecne lokalizovane rozdilove okno (stejna delka, mala odlisna oblast)
        if h1 and h2 and len(h1) == len(h2) and h1 != h2:
            diffs = [j for j in range(0, len(h1), 2) if h1[j:j + 2] != h2[j:j + 2]]
            if diffs:
                span = (max(diffs) - min(diffs)) // 2 + 1
                if span <= 12:  # jen male, localizovane zmeny (kandidat na ALS)
                    diff_windows.append({
                        "source": k[0], "path": k[1], "key": k[2],
                        "diff_bytes": len(diffs),
                        "span_bytes": span,
                        "v1": h1[min(diffs):max(diffs) + 2],
                        "v2": h2[min(diffs):max(diffs) + 2],
                    })
    # okenka s poctem odlisnych bajtu ~ delka ALS variabilni casti nahoru
    diff_windows.sort(key=lambda w: (w["diff_bytes"], w["span_bytes"]))
    return {
        "ok": True, "probe": "als-differential",
        "dir1": dir1, "dir2": dir2, "als1": als1, "als2": als2,
        "common_leaves": len(common),
        "covary_count": len(covary_hits),
        "covary_hits": covary_hits[:200],
        "diff_window_count": len(diff_windows),
        "diff_windows_sample": diff_windows[:300],
    }

@app.route('/api/als-differential', methods=['GET'])
def api_als_differential():
    als1 = request.args.get('als1')
    als2 = request.args.get('als2')
    dir1 = request.args.get('dir1')
    dir2 = request.args.get('dir2')
    udid1 = request.args.get('udid1')
    udid2 = request.args.get('udid2')
    deep = request.args.get('deep', '1') in ('1', 'true', 'yes', 'on')
    if not dir1 and udid1:
        dir1 = _als_find_latest_dir(udid1)
    if not dir2 and udid2:
        dir2 = _als_find_latest_dir(udid2)
    if not (dir1 and dir2 and als1 and als2):
        return jsonify({'ok': False, 'error': 'Vyzaduje als1, als2 a bud dir1/dir2 nebo udid1/udid2 '
                        '(z nichz se najde posledni dump). Nejdriv spust full-scan na obou telefonech.',
                        'dir1': dir1, 'dir2': dir2}), 200
    try:
        result = _als_differential(dir1, dir2, als1, als2, deep=deep)
        return jsonify(result), 200
    except FileNotFoundError as exc:
        return jsonify({'ok': False, 'error': f'Dump nenalezen: {exc}. Spust full-scan na obou telefonech.'}), 200
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), 200





# ─── V30 COMPONENT STORAGE MAP PROBE ─────────────────────────────────────────
# READ-ONLY comparison probe for newer generations. It does not guess a final
# component serial. It records exact paths/keys/values from IORegistry so we can
# compare iPhone 15 Pro storage layout with the previously confirmed model.
_V30_COMPONENT_TOKENS = (
    "camera", "cam", "pearl", "projector", "structuredlight", "truedepth",
    "lattice", "prox", "proximity", "distance", "distsens", "ambient", "als",
    "light", "panel", "display", "lcd", "clcd", "dcp", "battery", "serial",
    "module", "haptic", "taptic", "vibrator", "actuator", "wifi", "bluetooth",
    "baseband", "mlb", "logicboard", "board"
)

_V30_TARGETS = (
    "AppleH10CamIn", "AppleH13CamIn", "AppleH16CamIn", "AppleCameraInterface",
    "AppleH10PearlCam", "ApplePearlCam", "PearlCam", "AppleH10Pearl",
    "AppleCLCD", "AppleDCP", "AppleM2ScalerCSCDriver",
    "AppleSmartBattery", "AppleARMPMUCharger",
    "AppleProxHIDEventDriver", "prox", "AppleProxDriver", "AppleProximitySensor",
    "als", "AppleALSDriver", "AppleAmbientLightSensor", "AppleHIDALSService",
    "AppleHapticsSupportCallan", "AppleHapticsSupportLEAP", "AppleTapticEngine",
    "AppleHaptics", "Actuator", "audio-haptic", "haptics-support-interface",
)

def _v30_safe(value, max_binary=4096):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        clip = raw[:max_binary]
        return {
            "_type": "bytes", "length": len(raw), "hex": clip.hex(),
            "ascii": clip.decode("ascii", errors="replace").replace("\x00", "\\0"),
            "truncated": len(raw) > max_binary,
        }
    if isinstance(value, dict):
        return {str(k): _v30_safe(v, max_binary) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v30_safe(v, max_binary) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

def _v30_walk(value, path="$"):
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            p = f"{path}.{key}"
            rows.append((p, str(key), child))
            rows.extend(_v30_walk(child, p))
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            rows.extend(_v30_walk(child, f"{path}[{i}]"))
    return rows

def _v30_score(path, key):
    hay = (f"{path} {key}").lower().replace("-", "").replace("_", "")
    score = 0
    matched = []
    for token in _V30_COMPONENT_TOKENS:
        norm = token.replace("-", "").replace("_", "")
        if norm in hay:
            matched.append(token)
            score += 8 if token in ("serial", "projector", "structuredlight",
                                    "truedepth", "proximity", "distance",
                                    "ambient", "panel") else 3
    return score, matched

async def _v30_component_map_probe_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    calls, errors, rows = [], {}, []

    def scan(source, obj):
        for path, key, value in _v30_walk(obj):
            score, matched = _v30_score(path, key)
            scalar = _component_serial_scalar(value)
            if score > 0 and (scalar is not None or isinstance(value, (bytes, bytearray, memoryview))):
                rows.append({
                    "source": source, "path": path, "key": key,
                    "score": score, "matched_tokens": matched,
                    "value": _v30_safe(value),
                })

    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        calls.append({"source": "lockdown:all_values", "ok": bool(av),
                      "result_type": type(av).__name__})
        if av:
            scan("lockdown:all_values", av)
    except Exception as exc:
        errors["lockdown:all_values"] = f"{type(exc).__name__}: {exc}"

    for plane in ("IOService", "IODeviceTree", "IOPower"):
        try:
            obj = diag.ioregistry(plane=plane)
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({"source": f"ioregistry:plane:{plane}", "ok": bool(obj),
                          "result_type": type(obj).__name__})
            if obj:
                scan(f"ioregistry:plane:{plane}", obj)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[f"plane:{plane}"] = err
            calls.append({"source": f"ioregistry:plane:{plane}", "ok": False, "error": err})

    for target in _V30_TARGETS:
        try:
            obj = diag.ioregistry(name=target)
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({"source": f"ioregistry:name:{target}", "ok": bool(obj),
                          "result_type": type(obj).__name__})
            if obj:
                scan(f"ioregistry:name:{target}", obj)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[f"name:{target}"] = err
            calls.append({"source": f"ioregistry:name:{target}", "ok": False, "error": err})

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    dedup = {}
    for row in rows:
        ident = (row["source"], row["path"], row["key"], str(row["value"])[:500])
        if ident not in dedup or row["score"] > dedup[ident]["score"]:
            dedup[ident] = row
    rows = sorted(dedup.values(), key=lambda x: (-x["score"], x["source"], x["path"]))

    return {
        "ok": True,
        "probe": "component-storage-map-v30",
        "read_only": True,
        "udid": udid,
        "goal": "Map exact IORegistry paths and property names for component identifiers on this generation",
        "candidates": rows[:1500],
        "calls": calls,
        "errors": errors,
        "summary": {
            "candidates": len(rows),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

@app.route('/api/v30-component-map-probe/<udid>', methods=['GET'])
def api_v30_component_map_probe(udid):
    try:
        result = _run_async_isolated(_v30_component_map_probe_collect(udid), timeout=240)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "component-storage-map-v30", "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V31 ALS / PROX RAW STORAGE PROBE ───────────────────────────────────────
# READ-ONLY: cilene mapuje posledni dva senzory (ALS + proximity/distance).
# Nic nehádá do hlavního reportu. Vrací raw property, HEX/ASCII a transformace,
# aby šla potvrdit přesná cesta a encoding proti referenční hodnotě z 3uTools.
_V31_SENSOR_TARGETS = {
    "ambient_light_sensor": (
        "als", "AppleALSDriver", "AppleAmbientLightSensor",
        "AppleHIDALSService", "AppleProxHIDEventDriver"
    ),
    "distance_sensor": (
        "prox", "AppleProxHIDEventDriver", "AppleProxDriver",
        "AppleProximitySensor", "AppleHIDEventDriver"
    ),
}

def _v31_bytes_views(raw):
    raw = bytes(raw)
    views = []
    def add(name, value):
        if value is None:
            return
        text = str(value).strip()
        if text and not any(x["value"] == text for x in views):
            views.append({"transform": name, "value": text})
    add("hex", raw.hex().upper())
    add("hex_reversed", raw[::-1].hex().upper())
    for enc in ("ascii", "utf-8", "utf-16-le", "utf-16-be"):
        try:
            txt = raw.decode(enc, errors="strict").strip("\x00 \r\n\t")
            if txt and all(ch.isprintable() for ch in txt):
                add(enc, txt)
        except Exception:
            pass
    if len(raw) in (2, 4, 8):
        add("uint_le", int.from_bytes(raw, "little", signed=False))
        add("uint_be", int.from_bytes(raw, "big", signed=False))
    return views

def _v31_scalar_views(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _v31_bytes_views(bytes(value))
    scalar = _component_serial_scalar(value)
    if scalar is None:
        return []
    return [{"transform": "scalar", "value": scalar}]

def _v31_ref_norm(value):
    return re.sub(r"[^A-Fa-f0-9]", "", str(value or "")).upper()

def _v31_ref_matches(views, refs):
    hits = []
    for ref_name, ref_value in refs.items():
        needle = _v31_ref_norm(ref_value)
        if len(needle) < 4:
            continue
        for view in views:
            hay = _v31_ref_norm(view.get("value"))
            if needle and (needle in hay or hay in needle):
                hits.append({"reference": ref_name, "reference_value": ref_value,
                             "transform": view.get("transform"), "value": view.get("value")})
    return hits

async def _v31_sensor_storage_probe_collect(udid, als_ref=None, distance_ref=None):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls, rows = {}, [], []
    refs = {}
    if als_ref:
        refs["als_ref"] = als_ref
    if distance_ref:
        refs["distance_ref"] = distance_ref

    def scan(sensor, source, obj):
        for path, key, value in _v30_walk(obj):
            # Zachytit vsechny leaf hodnoty v cilovem uzlu, ne jen klice se 'serial'.
            if isinstance(value, (dict, list, tuple)):
                continue
            views = _v31_scalar_views(value)
            if not views:
                continue
            key_norm = _v29_norm_key(key)
            path_norm = _v29_norm_key(path)
            score = 0
            matched = []
            for token, weight in (("serial", 20), ("module", 10), ("sensor", 8),
                                  ("prox", 12), ("distance", 12), ("als", 12),
                                  ("ambient", 12), ("calib", 6), ("id", 3),
                                  ("factory", 8), ("mfg", 6), ("vendor", 4)):
                if token in key_norm or token in path_norm:
                    score += weight; matched.append(token)
            ref_hits = _v31_ref_matches(views, refs)
            if ref_hits:
                score += 100
            rows.append({
                "sensor": sensor, "source": source, "path": path, "key": key,
                "score": score, "matched_tokens": matched,
                "value": _v30_safe(value, max_binary=16384),
                "views": views, "reference_hits": ref_hits,
            })

    for sensor, targets in _V31_SENSOR_TARGETS.items():
        for target in targets:
            source = f"ioregistry:name:{target}"
            try:
                obj = diag.ioregistry(name=target)
                if inspect.isawaitable(obj):
                    obj = await obj
                calls.append({"sensor": sensor, "source": source, "ok": bool(obj),
                              "result_type": type(obj).__name__})
                if obj:
                    scan(sensor, source, obj)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                errors[source] = err
                calls.append({"sensor": sensor, "source": source, "ok": False, "error": err})

    # Full planes: hledej jen vetve/cesty, jejichz path nebo key vypada jako ALS/prox.
    for plane in ("IOService", "IODeviceTree"):
        source = f"ioregistry:plane:{plane}"
        try:
            obj = diag.ioregistry(plane=plane)
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({"sensor": "cross_plane", "source": source, "ok": bool(obj),
                          "result_type": type(obj).__name__})
            if obj:
                for path, key, value in _v30_walk(obj):
                    hay = _v29_norm_key(path + "." + str(key))
                    sensor = None
                    if any(t in hay for t in ("ambientlight", "als", "lightsensor")):
                        sensor = "ambient_light_sensor"
                    elif any(t in hay for t in ("proximity", "prox", "distance", "distsens")):
                        sensor = "distance_sensor"
                    if sensor and not isinstance(value, (dict, list, tuple)):
                        scan(sensor, source, {str(key): value})
        except Exception as exc:
            errors[source] = f"{type(exc).__name__}: {exc}"

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    dedup = {}
    for row in rows:
        ident = (row["sensor"], row["source"], row["path"], row["key"], str(row["value"])[:1000])
        if ident not in dedup or row["score"] > dedup[ident]["score"]:
            dedup[ident] = row
    rows = sorted(dedup.values(), key=lambda x: (-bool(x["reference_hits"]), -x["score"], x["sensor"], x["source"], x["path"]))

    return {
        "ok": True, "probe": "als-prox-storage-probe-v31", "read_only": True,
        "udid": udid, "references": refs,
        "goal": "Find exact raw storage and encoding for ALS and distance/proximity identifiers",
        "candidates": rows[:4000], "calls": calls, "errors": errors,
        "summary": {
            "candidates": len(rows), "reference_hits": sum(len(r["reference_hits"]) for r in rows),
            "als_candidates": sum(1 for r in rows if r["sensor"] == "ambient_light_sensor"),
            "distance_candidates": sum(1 for r in rows if r["sensor"] == "distance_sensor"),
            "calls_total": len(calls), "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

@app.route('/api/v31-sensor-storage-probe/<udid>', methods=['GET'])
def api_v31_sensor_storage_probe(udid):
    try:
        als_ref = (request.args.get('als_ref') or '').strip() or None
        distance_ref = (request.args.get('distance_ref') or '').strip() or None
        result = _run_async_isolated(
            _v31_sensor_storage_probe_collect(udid, als_ref=als_ref, distance_ref=distance_ref),
            timeout=300
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "probe": "als-prox-storage-probe-v31",
                        "udid": udid, "error": f"{type(exc).__name__}: {exc}"}), 200


# ─── V32 EXACT SENSOR FORENSIC SCANNER ──────────────────────────────────────
# READ-ONLY. Exact reference matching only; prints searched values explicitly.
_V32_DEFAULT_ALS_REF = "0311133F6B07"

def _v32_patterns(ref):
    ref = str(ref or "").strip()
    compact = re.sub(r"[^A-Fa-f0-9]", "", ref)
    out = []
    def add(name, raw):
        if raw and not any(x[1] == raw for x in out):
            out.append((name, raw))
    add("ascii", ref.encode("ascii", errors="ignore"))
    add("ascii_upper", ref.upper().encode("ascii", errors="ignore"))
    add("ascii_lower", ref.lower().encode("ascii", errors="ignore"))
    try:
        rawhex = bytes.fromhex(compact)
        add("hex_bytes", rawhex)
        add("hex_bytes_reversed", rawhex[::-1])
        add("hex_nibble_reversed", bytes.fromhex(compact[::-1]))
    except Exception:
        pass
    add("utf16le", ref.encode("utf-16-le"))
    add("utf16be", ref.encode("utf-16-be"))
    return out

def _v32_leaf_bytes(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), "bytes"
    if isinstance(value, str):
        return value.encode("utf-8", errors="ignore"), "string"
    if isinstance(value, int) and value >= 0:
        n = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(n, "little"), "integer_le"
    return None, None

def _v32_walk(obj, path="$", depth=0):
    if depth > 100:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            p = f"{path}.{key}"
            if isinstance(value, (dict, list, tuple)):
                yield from _v32_walk(value, p, depth + 1)
            else:
                yield p, str(key), value
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            p = f"{path}[{i}]"
            if isinstance(value, (dict, list, tuple)):
                yield from _v32_walk(value, p, depth + 1)
            else:
                yield p, str(i), value

async def _v32_exact_sensor_forensic_collect(udid, als_ref=None, distance_ref=None):
    import inspect
    ld, diag = await _open_diag(udid)
    refs = {
        "ambient_light_sensor": als_ref or _V32_DEFAULT_ALS_REF,
    }
    if distance_ref:
        refs["distance_sensor"] = distance_ref

    searched_values = {}
    patterns = {}
    for goal, ref in refs.items():
        pats = _v32_patterns(ref)
        patterns[goal] = pats
        searched_values[goal] = {
            "reference": ref,
            "patterns": [{"transform": name, "hex": raw.hex().upper(),
                          "length": len(raw)} for name, raw in pats]
        }

    calls, errors, hits, interesting = [], {}, [], []
    targets = (
        "als", "prox", "AppleProxHIDEventDriver", "AppleALSDriver",
        "AppleAmbientLightSensor", "AppleHIDALSService", "AppleProxDriver",
        "AppleProximitySensor", "AppleSPU", "AppleSPUHIDDriver",
        "AppleAOP", "AppleAOPAudio", "AppleHIDEventDriver"
    )

    sources = []
    async def fetch(source, **kwargs):
        try:
            obj = diag.ioregistry(**kwargs)
            if inspect.isawaitable(obj):
                obj = await obj
            ok = isinstance(obj, (dict, list, tuple)) and bool(obj)
            calls.append({"source": source, "ok": ok,
                          "result_type": type(obj).__name__})
            if ok:
                sources.append((source, obj))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[source] = err
            calls.append({"source": source, "ok": False, "error": err})

    for plane in ("IOService", "IODeviceTree", "IOPower"):
        await fetch(f"ioregistry:plane:{plane}", plane=plane)
    for target in targets:
        await fetch(f"ioregistry:name:{target}", name=target)

    # Also scan lockdown domains/blobs that may contain calibration data.
    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        if isinstance(av, dict):
            sources.append(("lockdown:all_values", av))
            calls.append({"source":"lockdown:all_values","ok":True,
                          "result_type":type(av).__name__})
    except Exception as exc:
        errors["lockdown:all_values"] = f"{type(exc).__name__}: {exc}"

    for source, obj in sources:
        for path, key, value in _v32_walk(obj):
            raw, value_type = _v32_leaf_bytes(value)
            if raw is None:
                continue
            exact_here = []
            for goal, pats in patterns.items():
                for transform, needle in pats:
                    if not needle:
                        continue
                    start = 0
                    while True:
                        offset = raw.find(needle, start)
                        if offset < 0:
                            break
                        exact_here.append({
                            "goal": goal, "reference": refs[goal],
                            "transform": transform, "offset": offset,
                            "needle_hex": needle.hex().upper()
                        })
                        start = offset + 1
            if exact_here:
                hits.append({
                    "source": source, "path": path, "key": key,
                    "value_type": value_type, "raw_length": len(raw),
                    "raw_hex": raw[:32768].hex().upper(),
                    "ascii": raw[:32768].decode("ascii", errors="replace").replace("\x00","\\0"),
                    "exact_hits": exact_here,
                })

            norm = _v29_norm_key(f"{path} {key}")
            tokens = [t for t in ("als","ambient","light","prox","proximity",
                                  "distance","sensor","calib","serial","module",
                                  "factory","vendor","saca") if t in norm]
            if tokens and len(raw) >= 4:
                interesting.append({
                    "source": source, "path": path, "key": key,
                    "matched_tokens": tokens, "value_type": value_type,
                    "raw_length": len(raw),
                    "raw_hex": raw[:8192].hex().upper(),
                    "ascii": raw[:8192].decode("ascii", errors="replace").replace("\x00","\\0"),
                })

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    interesting.sort(key=lambda x: (-len(x["matched_tokens"]), -x["raw_length"], x["source"], x["path"]))
    return {
        "ok": True,
        "probe": "exact-sensor-forensic-v32",
        "read_only": True,
        "udid": udid,
        "searched_values": searched_values,
        "exact_hits": hits,
        "interesting_blobs": interesting[:1000],
        "calls": calls,
        "errors": errors,
        "summary": {
            "searched_references": len(refs),
            "exact_hits": len(hits),
            "interesting_blobs": len(interesting),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

@app.route('/api/v32-exact-sensor-forensic/<udid>', methods=['GET'])
def api_v32_exact_sensor_forensic(udid):
    als_ref = (request.args.get("als_ref") or _V32_DEFAULT_ALS_REF).strip()
    distance_ref = (request.args.get("distance_ref") or "").strip() or None
    try:
        result = _run_async_isolated(
            _v32_exact_sensor_forensic_collect(
                udid, als_ref=als_ref, distance_ref=distance_ref
            ), timeout=300
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "exact-sensor-forensic-v32",
            "udid": udid, "searched_values": {
                "ambient_light_sensor": als_ref,
                "distance_sensor": distance_ref,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }), 200


# ─── V33 ALS LAST-MILE PROBE ────────────────────────────────────────────────
# READ-ONLY. 3uTools on the same iPhone 15 Pro reads ALS 0311133F6B07.
# V32 proved it is not present as a plain IORegistry leaf. V33 therefore scans
# RAW diagnostics replies, MobileGestalt/calibration requests and recursively
# searches the complete serialized reply (plist/binary-plist/json/repr), not
# only leaf values. It also searches the 6 raw bytes 03 11 13 3F 6B 07 and
# byte/word reversed variants.
_V33_DEFAULT_ALS_REF = "0311133F6B07"
_V33_MG_KEYS = (
    "AmbientLightSensorSerialNumber", "ALSSerialNumber", "AmbientLightSerialNumber",
    "AmbientLightSensorCalibration", "ALSCalibration", "LightSensorCalibration",
    "SensorCalibration", "SensorCalibrationData", "CalibrationData",
    "ProximitySensorCalibration", "SysCfg", "SysCfgDict",
    "ambient-light-sensor", "als", "light-sensor", "saca",
)
_V33_RAW_REQUESTS = (
    "Diagnostics", "MobileGestalt", "IORegistry", "All", "NAND", "WiFi",
    "GasGauge", "System", "AppleDiagnosticData", "SysCfg",
)

def _v33_patterns(ref):
    compact = re.sub(r"[^0-9A-Fa-f]", "", str(ref or ""))
    raw = bytes.fromhex(compact) if compact and len(compact) % 2 == 0 else b""
    out = []
    def add(name, value):
        if value and not any(v == value for _, v in out):
            out.append((name, value))
    add("ascii", str(ref).encode("ascii", errors="ignore"))
    add("raw6", raw)
    add("raw6_reversed", raw[::-1])
    if raw:
        add("word16_le_swap", b"".join(raw[i:i+2][::-1] for i in range(0, len(raw), 2)))
        add("word16_order_reversed", b"".join([raw[i:i+2] for i in range(0, len(raw), 2)][::-1]))
        add("utf16le", str(ref).encode("utf-16-le"))
        add("utf16be", str(ref).encode("utf-16-be"))
    return out

def _v33_serializations(obj):
    import plistlib
    blobs = []
    def add(name, raw):
        if isinstance(raw, (bytes, bytearray)) and raw and not any(x[1] == bytes(raw) for x in blobs):
            blobs.append((name, bytes(raw)))
    if isinstance(obj, (bytes, bytearray, memoryview)):
        add("raw", bytes(obj))
    try: add("plist_xml", plistlib.dumps(obj, fmt=plistlib.FMT_XML, sort_keys=False))
    except Exception: pass
    try: add("plist_binary", plistlib.dumps(obj, fmt=plistlib.FMT_BINARY, sort_keys=False))
    except Exception: pass
    try: add("json", json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception: pass
    try: add("repr", repr(obj).encode("utf-8", errors="replace"))
    except Exception: pass
    return blobs

def _v33_scan_blob(source, encoding, raw, patterns):
    hits = []
    for transform, needle in patterns:
        start = 0
        while needle:
            pos = raw.find(needle, start)
            if pos < 0: break
            lo, hi = max(0, pos - 96), min(len(raw), pos + len(needle) + 96)
            hits.append({
                "source": source, "serialization": encoding, "transform": transform,
                "offset": pos, "needle_hex": needle.hex().upper(),
                "context_hex": raw[lo:hi].hex().upper(),
                "context_ascii": raw[lo:hi].decode("ascii", errors="replace").replace("\x00", "\\0"),
            })
            start = pos + 1
    return hits

async def _v33_als_last_mile_collect(udid, als_ref=None):
    import inspect, datetime as _dt
    ref = als_ref or _V33_DEFAULT_ALS_REF
    patterns = _v33_patterns(ref)
    ld, diag = await _open_diag(udid)
    calls, errors, hits, captured = [], {}, [], []

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value): value = await value
            calls.append({"source": label, "ok": True, "result_type": type(value).__name__})
            captured.append((label, value))
            for enc, blob in _v33_serializations(value):
                hits.extend(_v33_scan_blob(label, enc, blob, patterns))
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    await capture("lockdown:all_values", lambda: ld.all_values)
    for domain in ("com.apple.mobile.internal", "com.apple.mobile.lockdown",
                   "com.apple.mobile.gestalt", "com.apple.mobile.iTunes"):
        await capture(f"lockdown:domain:{domain}", lambda domain=domain: ld.get_value(domain=domain))

    # Batch + one-key-at-a-time MobileGestalt. A private implementation may
    # return a key only when queried separately.
    await capture("diagnostics:MobileGestalt:batch", lambda: diag._send_recv({
        "Request": "MobileGestalt", "MobileGestaltKeys": list(_V33_MG_KEYS)}))
    for key in _V33_MG_KEYS:
        await capture(f"diagnostics:MobileGestalt:{key}", lambda key=key: diag._send_recv({
            "Request": "MobileGestalt", "MobileGestaltKeys": [key]}))

    for req in _V33_RAW_REQUESTS:
        payload = {"Request": req}
        if req == "MobileGestalt": payload["MobileGestaltKeys"] = list(_V33_MG_KEYS)
        if req == "IORegistry": payload["CurrentPlane"] = "IOService"
        await capture(f"diagnostics:raw:{req}", lambda payload=payload: diag._send_recv(payload))

    for plane in ("IOService", "IODeviceTree", "IOPower"):
        await capture(f"ioregistry:plane:{plane}", lambda plane=plane: diag.ioregistry(plane=plane))

    # Persist every reply locally so we can diff it against another phone later.
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v33", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)
    manifest_sources = []
    for idx, (label, value) in enumerate(captured, 1):
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:100]
        for enc, blob in _v33_serializations(value):
            fn = f"{idx:03d}_{safe_label}.{enc}.bin"
            with open(os.path.join(capture_dir, fn), "wb") as fh: fh.write(blob)
            manifest_sources.append({"source": label, "serialization": enc,
                                     "file": fn, "length": len(blob)})

    try:
        cr = diag.close()
        if inspect.isawaitable(cr): await cr
    except Exception: pass

    result = {
        "ok": True, "probe": "als-last-mile-v33", "read_only": True,
        "udid": udid, "als_reference": ref,
        "searched_patterns": [{"transform": n, "hex": b.hex().upper(), "length": len(b)} for n,b in patterns],
        "exact_hits": hits, "capture_dir": capture_dir,
        "captured_sources": manifest_sources, "calls": calls, "errors": errors,
        "summary": {"exact_hits": len(hits), "calls_total": len(calls),
                    "calls_ok": sum(1 for c in calls if c.get("ok")),
                    "captured_files": len(manifest_sources)},
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return result

@app.route('/api/v33-als-last-mile-probe/<udid>', methods=['GET'])
def api_v33_als_last_mile_probe(udid):
    als_ref = (request.args.get("als_ref") or _V33_DEFAULT_ALS_REF).strip()
    try:
        result = _run_async_isolated(_v33_als_last_mile_collect(udid, als_ref), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "probe": "als-last-mile-v33", "udid": udid,
                        "als_reference": als_ref, "error": f"{type(exc).__name__}: {exc}"}), 200


# ─── DOT / TRUEDEPTH PROJECTOR EX-FACTORY DISCOVERY ─────────────────────────
_DOT_PROJECTOR_TARGETS = (
    "AppleH10CamIn", "AppleH10PearlCam", "PearlCam", "ApplePearlCam",
    "AppleH10PearlCamInterface", "AppleH10Pearl",
    "AppleH10PearlProjector", "AppleH10PearlFlood",
)

def _dot_json_safe(value, max_binary=8192):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        clipped = raw[:max_binary]
        return {"_type": "bytes", "length": len(raw), "hex": clipped.hex(),
                "ascii": clipped.decode("ascii", errors="replace").replace("\x00", "\\0"),
                "truncated": len(raw) > max_binary}
    if isinstance(value, dict):
        return {str(k): _dot_json_safe(v, max_binary) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dot_json_safe(v, max_binary) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

def _dot_walk(value, path="$"):
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            rows.append((child_path, str(key), child))
            rows.extend(_dot_walk(child, child_path))
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            rows.extend(_dot_walk(child, f"{path}[{idx}]"))
    return rows

def _dot_score(path, key):
    hay = f"{path} {key}".lower().replace("-", "").replace("_", "")
    weights = {"dot": 8, "projector": 10, "structuredlight": 10, "truedepth": 8,
               "pearl": 5, "factory": 10, "exfactory": 14, "serial": 6,
               "calibration": 5, "module": 3, "current": 2}
    return sum(weight for token, weight in weights.items() if token in hay)

async def _dot_projector_factory_probe_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls, candidates, exact_named = {}, [], [], []
    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        if isinstance(av, dict):
            for path, key, value in _dot_walk(av):
                score = _dot_score(path, key)
                if score:
                    candidates.append({"source": "lockdown:all_values", "path": path,
                                       "key": key, "score": score, "value": _dot_json_safe(value)})
    except Exception as exc:
        errors["lockdown"] = f"{type(exc).__name__}: {exc}"

    exact_keys = {
        "structuredlightprojectormoduleserialnumstring",
        "frontirstructuredlightprojectorserialnumstring",
        "dotprojectorserialnumstring", "projectormoduleserialnumstring",
        "dotprojectorexfactoryvalue", "projectorexfactoryvalue", "exfactoryvalue",
    }
    for target in _DOT_PROJECTOR_TARGETS:
        try:
            result = diag.ioregistry(name=target)
            if inspect.isawaitable(result):
                result = await result
            calls.append({"target": target,
                          "query": f"DiagnosticsService.ioregistry(name='{target}')",
                          "ok": bool(result), "result_type": type(result).__name__})
            if result:
                for path, key, value in _dot_walk(result):
                    score = _dot_score(path, key)
                    key_norm = str(key).lower().replace("-", "").replace("_", "")
                    item = {"source": f"ioregistry:name:{target}", "path": path,
                            "key": key, "value": _dot_json_safe(value)}
                    if key_norm in exact_keys:
                        exact_named.append(item)
                    if score:
                        candidates.append({**item, "score": score})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[f"name:{target}"] = err
            calls.append({"target": target, "ok": False, "error": err})

    try:
        result = diag.ioregistry(plane="IOService")
        if inspect.isawaitable(result):
            result = await result
        calls.append({"target": "IOService", "query": "DiagnosticsService.ioregistry(plane='IOService')",
                      "ok": bool(result), "result_type": type(result).__name__})
        if result:
            for path, key, value in _dot_walk(result):
                score = _dot_score(path, key)
                if score >= 8:
                    candidates.append({"source": "ioregistry:plane:IOService", "path": path,
                                       "key": key, "score": score, "value": _dot_json_safe(value)})
    except Exception as exc:
        errors["plane:IOService"] = f"{type(exc).__name__}: {exc}"

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    dedup = {}
    for item in candidates:
        ident = (item["source"], item["path"], item["key"])
        if ident not in dedup or item["score"] > dedup[ident]["score"]:
            dedup[ident] = item
    candidates = sorted(dedup.values(), key=lambda x: (-x["score"], x["source"], x["path"]))

    return {"ok": True, "probe": "dot-projector-ex-factory-discovery-v1", "udid": udid,
            "goal": "Locate DOT/Structured Light projector Ex-factory value and exact IORegistry source",
            "read_only": True, "exact_named_hits": exact_named, "candidates": candidates[:500],
            "calls": calls, "errors": errors,
            "summary": {"exact_named_hits": len(exact_named), "candidates": len(candidates),
                        "calls_total": len(calls), "calls_ok": sum(1 for c in calls if c.get("ok"))}}

@app.route('/api/dot-projector-factory-probe/<udid>', methods=['GET'])
def api_dot_projector_factory_probe(udid):
    try:
        result = _run_async_isolated(_dot_projector_factory_probe_collect(udid), timeout=180)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "probe": "dot-projector-ex-factory-discovery-v1",
                        "udid": udid, "error": f"{type(exc).__name__}: {exc}"}), 200



# ─── EXTENDED HARDWARE VALUE DISCOVERY V18 ───────────────────────────────────
# READ-ONLY discovery probe for values that are visible in 3uTools but whose
# exact pymobiledevice3 / IORegistry property path has not yet been confirmed.
_DISCOVERY_EXPECTED = {
    "distance_sensor": "FWP8311CBQ1H6CW20",
    "ambient_light_sensor": "3E-85DF2320",
    "vibrator_number": "FTN838245WQJGJN84+XMAYM1",
}

def _discovery_value_forms(value):
    forms = []
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        forms.append(("bytes_hex", raw.hex()))
        forms.append(("bytes_ascii", raw.decode("ascii", errors="ignore").strip("\x00")))
    elif isinstance(value, (str, int, float, bool)):
        forms.append(("scalar", str(value)))
    return forms

def _discovery_norm(value):
    return re.sub(r"[^A-Z0-9+]", "", str(value).upper())

def _discovery_walk(value, path="$"):
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            rows.append((child_path, str(key), child))
            rows.extend(_discovery_walk(child, child_path))
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            rows.extend(_discovery_walk(child, f"{path}[{idx}]"))
    return rows

async def _extended_hw_discovery_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors = {}
    sources = []
    hits = []
    candidates = []

    async def add_source(label, query_fn):
        try:
            result = query_fn()
            if inspect.isawaitable(result):
                result = await result
            sources.append({"source": label, "ok": bool(result), "result_type": type(result).__name__})
            if result:
                scan_source(label, result)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            sources.append({"source": label, "ok": False, "error": err})

    def scan_source(label, result):
        expected_norm = {k: _discovery_norm(v) for k, v in _DISCOVERY_EXPECTED.items()}
        tokens = (
            "distance", "dist", "prox", "ambient", "light", "als",
            "vibr", "haptic", "taptic", "nand", "flash", "storage",
            "manufacturer", "vendor", "serial", "baseband", "chip"
        )
        for path, key, value in _discovery_walk(result):
            key_hay = f"{path} {key}".lower()
            forms = _discovery_value_forms(value)
            for form, rendered in forms:
                norm = _discovery_norm(rendered)
                for goal, expected in expected_norm.items():
                    if expected and expected in norm:
                        hits.append({
                            "goal": goal, "expected": _DISCOVERY_EXPECTED[goal],
                            "source": label, "path": path, "key": key,
                            "form": form, "value": rendered,
                        })
                if any(token in key_hay for token in tokens):
                    candidates.append({
                        "source": label, "path": path, "key": key,
                        "form": form, "value": rendered,
                    })

    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        sources.append({"source": "lockdown:all_values", "ok": bool(av), "result_type": type(av).__name__})
        if av:
            scan_source("lockdown:all_values", av)
    except Exception as exc:
        errors["lockdown:all_values"] = f"{type(exc).__name__}: {exc}"

    # Broad IOService tree is especially important: V17 proved that guessed
    # class names are insufficient for DistSens / ALS / vibrator / NAND.
    await add_source("ioregistry:plane:IOService",
                     lambda: diag.ioregistry(plane="IOService"))

    targets = (
        "AppleH10CamIn", "AppleH10PearlCam", "AppleCLCD", "AppleSmartBattery",
        "AppleProxDriver", "AppleALSDriver", "AppleHapticsSupportLEAP",
        "AppleNANDConfigAccess", "AppleANS2NVMeController", "AppleANS2Controller",
        "AppleEmbeddedNVMeController", "AppleNVMeController",
        "AppleTapticEngine", "AppleHaptics", "AppleProximitySensor",
        "AppleAmbientLightSensor",
    )
    for target in targets:
        await add_source(f"ioregistry:name:{target}",
                         lambda target=target: diag.ioregistry(name=target))

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    # Deduplicate and keep payload manageable.
    def dedup(items, keys):
        seen, out = set(), []
        for item in items:
            ident = tuple(str(item.get(k)) for k in keys)
            if ident not in seen:
                seen.add(ident)
                out.append(item)
        return out

    hits = dedup(hits, ("goal", "source", "path", "form", "value"))
    candidates = dedup(candidates, ("source", "path", "key", "form", "value"))

    return {
        "ok": True,
        "probe": "extended-hardware-discovery-v18",
        "read_only": True,
        "udid": udid,
        "expected": _DISCOVERY_EXPECTED,
        "exact_value_hits": hits,
        "candidates": candidates[:1500],
        "sources": sources,
        "errors": errors,
        "summary": {
            "exact_value_hits": len(hits),
            "candidates": len(candidates),
            "sources_total": len(sources),
            "sources_ok": sum(1 for s in sources if s.get("ok")),
        },
    }

@app.route('/api/extended-hardware-discovery/<udid>', methods=['GET'])
def api_extended_hardware_discovery(udid):
    try:
        result = _run_async_isolated(_extended_hw_discovery_collect(udid), timeout=240)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "extended-hardware-discovery-v18",
            "udid": udid, "error": f"{type(exc).__name__}: {exc}"
        }), 200



# ─── V19 DIAGNOSTICS RELAY / PEARL / SENSOR DISCOVERY ────────────────────────
# CAP2 reconstruction showed that 3uTools repeatedly uses Apple's diagnostics
# relay through usbmux. This probe therefore stays on the same service and tests
# raw diagnostics requests + known Apple codenames instead of guessing another
# USB protocol.
_V19_EXPECTED = {
    "distance_sensor": "FWP8311CBQ1H6CW20",
    "ambient_light_sensor": "3E-85DF2320",
    "vibrator_number": "FTN838245WQJGJN84+XMAYM1",
}

_V19_MG_KEYS = [
    "RosalineSerialNumber",
    "SavageSerialNumber",
    "YonkersSerialNumber",
    "ScreenSerialNumber",
    "WirelessBoardSnum",
    "VibratorCapability",
    "ambient-light-sensor",
    "prox-sensor",
    "proximity-sensor",
    "haptics",
    "calibration",
    "SysCfg",
    "SysCfgDict",
]

_V19_NODE_NAMES = [
    "rosaline", "Rosaline", "savage", "Savage", "yonkers", "Yonkers",
    "prox", "proximity", "als", "ambient-light-sensor",
    "haptics", "vibrator", "taptic", "AppleH10CamIn", "AppleH10PearlCam",
    "AppleCLCD", "AppleSmartBattery",
]

def _v19_norm(v):
    return re.sub(r"[^A-Z0-9+]", "", str(v).upper())

def _v19_safe(v, max_binary=32768):
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        cut = raw[:max_binary]
        return {
            "_type": "bytes",
            "length": len(raw),
            "hex": cut.hex(),
            "ascii": cut.decode("ascii", errors="replace").replace("\x00", "\\0"),
            "truncated": len(raw) > max_binary,
        }
    if isinstance(v, dict):
        return {str(k): _v19_safe(x, max_binary) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_v19_safe(x, max_binary) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)

def _v19_walk(v, path="$"):
    rows = []
    if isinstance(v, dict):
        for k, child in v.items():
            p = f"{path}.{k}"
            rows.append((p, str(k), child))
            rows.extend(_v19_walk(child, p))
    elif isinstance(v, (list, tuple)):
        for i, child in enumerate(v):
            rows.extend(_v19_walk(child, f"{path}[{i}]"))
    return rows

async def _v19_sensor_discovery_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls, exact_hits, candidates = {}, [], [], []
    expected = {k: _v19_norm(v) for k, v in _V19_EXPECTED.items()}
    tokens = ("rosaline", "savage", "yonkers", "dist", "prox", "ambient",
              "light", "als", "vibr", "haptic", "taptic", "serial",
              "calibration", "syscfg")

    def scan(label, obj):
        for path, key, value in _v19_walk(obj):
            forms = []
            if isinstance(value, (bytes, bytearray, memoryview)):
                raw = bytes(value)
                forms = [("bytes_hex", raw.hex()),
                         ("bytes_ascii", raw.decode("ascii", errors="ignore").strip("\x00"))]
            elif isinstance(value, (str, int, float, bool)):
                forms = [("scalar", str(value))]
            for form, rendered in forms:
                norm = _v19_norm(rendered)
                for goal, needle in expected.items():
                    if needle and needle in norm:
                        exact_hits.append({
                            "goal": goal, "expected": _V19_EXPECTED[goal],
                            "source": label, "path": path, "key": key,
                            "form": form, "value": rendered,
                        })
                hay = f"{path} {key}".lower()
                if any(t in hay for t in tokens):
                    candidates.append({
                        "source": label, "path": path, "key": key,
                        "form": form, "value": rendered,
                    })

    async def call(label, fn):
        try:
            result = fn()
            if inspect.isawaitable(result):
                result = await result
            calls.append({"source": label, "ok": result is not None,
                          "result_type": type(result).__name__})
            if result is not None:
                scan(label, result)
            return result
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    # Normal diagnostics report.
    await call("diagnostics:get_diagnostics", lambda: diag.get_diagnostics())

    # Raw MobileGestalt request: preserve the raw response even when the wrapper
    # would raise MobileGestaltDeprecated on iOS >= 17.4.
    await call(
        "diagnostics:raw_mobilegestalt",
        lambda: diag._send_recv({
            "Request": "MobileGestalt",
            "MobileGestaltKeys": _V19_MG_KEYS,
        })
    )

    # Query each key separately too; some private relay implementations behave
    # differently for a small key set.
    for key in _V19_MG_KEYS:
        await call(
            f"diagnostics:raw_mobilegestalt:{key}",
            lambda key=key: diag._send_recv({
                "Request": "MobileGestalt",
                "MobileGestaltKeys": [key],
            })
        )

    # DeviceTree/IOService planes and Apple codenames.
    for plane in ("IODeviceTree", "IOService", "IOPower"):
        await call(f"ioregistry:plane:{plane}",
                   lambda plane=plane: diag.ioregistry(plane=plane))

    for name in _V19_NODE_NAMES:
        await call(f"ioregistry:name:{name}",
                   lambda name=name: diag.ioregistry(name=name))

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    def dedup(items, fields):
        seen, out = set(), []
        for item in items:
            ident = tuple(str(item.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident)
                out.append(item)
        return out

    exact_hits = dedup(exact_hits, ("goal", "source", "path", "form", "value"))
    candidates = dedup(candidates, ("source", "path", "key", "form", "value"))

    return {
        "ok": True,
        "probe": "diagnostics-relay-sensor-discovery-v19",
        "read_only": True,
        "udid": udid,
        "capture_conclusion": "CAP2 shows 3uTools using usbmux + Apple diagnostics relay; service payload is TLS-encrypted",
        "expected": _V19_EXPECTED,
        "mobilegestalt_keys": _V19_MG_KEYS,
        "exact_value_hits": exact_hits,
        "candidates": candidates[:2500],
        "calls": calls,
        "errors": errors,
        "summary": {
            "exact_value_hits": len(exact_hits),
            "candidates": len(candidates),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

@app.route('/api/diagnostics-relay-sensor-discovery/<udid>', methods=['GET'])
def api_v19_sensor_discovery(udid):
    try:
        result = _run_async_isolated(_v19_sensor_discovery_collect(udid), timeout=300)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "diagnostics-relay-sensor-discovery-v19",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V20 DEEP SYSCFG / MOBILEGESTALT / BINARY SENSOR DISCOVERY ───────────────
# READ-ONLY rekonstrukcni probe. Cilem je dohledat zdroj hodnot, ktere 3uTools
# zobrazuje jako Distance Sensor / Ambient Light / Vibrator Number.
# Nic nezapisuje do telefonu a nemeni SysCfg ani kalibrace.
_V20_EXPECTED = {
    "distance_sensor": "FWP8311CBQ1H6CW20",
    "ambient_light_sensor": "3E-85DF2320",
    "vibrator_number": "FTN838245WQJGJN84+XMAYM1",
}

_V20_KEYS = [
    "SysCfg", "SysCfgDict", "syscfg", "DeviceTree", "IODeviceTree",
    "RosalineSerialNumber", "SavageSerialNumber", "YonkersSerialNumber",
    "VibratorNumber", "VibratorSerialNumber", "HapticSerialNumber",
    "TapticEngineSerialNumber", "VibratorCapability",
    "DistanceSensorSerialNumber", "ProximitySensorSerialNumber",
    "AmbientLightSensorSerialNumber", "ALSSerialNumber",
    "ProximitySensorCalibration", "AmbientLightSensorCalibration",
    "CalibrationData", "SensorCalibrationData", "PearlCalibrationData",
]

_V20_NAMES = [
    "AppleH10CamIn", "AppleH10PearlCam", "AppleCLCD", "AppleSmartBattery",
    "AppleProxDriver", "AppleALSDriver", "AppleHapticsSupportLEAP",
    "AppleTapticEngine", "AppleHaptics", "AppleProximitySensor",
    "AppleAmbientLightSensor", "prox", "proximity", "als",
    "ambient-light-sensor", "haptics", "vibrator", "taptic",
    "rosaline", "Rosaline", "savage", "Savage", "yonkers", "Yonkers",
]

def _v20_norm(value):
    return re.sub(r"[^A-Z0-9+]", "", str(value).upper())

def _v20_printable_runs(raw, min_len=4):
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return []
    data = bytes(raw)
    return [m.decode("ascii", errors="ignore") for m in
            re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)]

def _v20_forms(value):
    forms = []
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        forms.append(("bytes_hex", raw.hex()))
        forms.append(("bytes_ascii", raw.decode("ascii", errors="ignore").strip("\x00")))
        for i, run in enumerate(_v20_printable_runs(raw)):
            forms.append((f"printable_run:{i}", run))
        # nektere serialy mohou byt uvnitr UTF-16LE payloadu
        try:
            forms.append(("utf16le", raw.decode("utf-16le", errors="ignore").strip("\x00")))
        except Exception:
            pass
    elif isinstance(value, (str, int, float, bool)):
        forms.append(("scalar", str(value)))
    return [(kind, rendered) for kind, rendered in forms if rendered]

def _v20_walk(value, path="$"):
    rows = []
    if isinstance(value, dict):
        for key, child in value.items():
            p = f"{path}.{key}"
            rows.append((p, str(key), child))
            rows.extend(_v20_walk(child, p))
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            rows.extend(_v20_walk(child, f"{path}[{idx}]"))
    return rows

async def _v20_deep_sensor_discovery_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls, exact_hits, candidates = {}, [], [], []
    expected = {k: _v20_norm(v) for k, v in _V20_EXPECTED.items()}
    tokens = (
        "syscfg", "rosaline", "savage", "yonkers", "distance", "dist",
        "prox", "ambient", "light", "als", "vibr", "haptic", "taptic",
        "serial", "calibration", "sensor", "pearl"
    )

    def scan(label, obj):
        for path, key, value in _v20_walk(obj):
            hay = f"{path} {key}".lower()
            for form, rendered in _v20_forms(value):
                norm = _v20_norm(rendered)
                for goal, needle in expected.items():
                    if needle and needle in norm:
                        exact_hits.append({
                            "goal": goal, "expected": _V20_EXPECTED[goal],
                            "source": label, "path": path, "key": key,
                            "form": form, "value": rendered,
                        })
                if any(token in hay for token in tokens):
                    candidates.append({
                        "source": label, "path": path, "key": key,
                        "form": form, "value": rendered[:4096],
                    })

    async def call(label, fn):
        try:
            result = fn()
            if inspect.isawaitable(result):
                result = await result
            calls.append({"source": label, "ok": result is not None,
                          "result_type": type(result).__name__})
            if result is not None:
                scan(label, result)
            return result
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    # Lockdown all_values + vybrane domeny. Neznamou domenu pouze CTEME.
    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        calls.append({"source": "lockdown:all_values", "ok": isinstance(av, dict),
                      "result_type": type(av).__name__})
        if av:
            scan("lockdown:all_values", av)
    except Exception as exc:
        errors["lockdown:all_values"] = f"{type(exc).__name__}: {exc}"

    for domain in (
        "com.apple.mobile.battery", "com.apple.disk_usage",
        "com.apple.mobile.iTunes", "com.apple.mobile.internal",
        "com.apple.mobile.lockdown", "com.apple.mobile.gestalt",
    ):
        await call(f"lockdown:domain:{domain}",
                   lambda domain=domain: ld.get_value(domain=domain))

    # Standardni diagnostics a raw MobileGestalt, batch i jednotlive klice.
    await call("diagnostics:get_diagnostics", lambda: diag.get_diagnostics())
    await call("diagnostics:raw_mobilegestalt:batch",
               lambda: diag._send_recv({
                   "Request": "MobileGestalt",
                   "MobileGestaltKeys": _V20_KEYS,
               }))
    for key in _V20_KEYS:
        await call(f"diagnostics:raw_mobilegestalt:{key}",
                   lambda key=key: diag._send_recv({
                       "Request": "MobileGestalt",
                       "MobileGestaltKeys": [key],
                   }))

    # Syrove diagnosticke requesty, ktere ruzne verze diagnostics_relay mohou
    # podporovat. Neznamy request se pouze zaloguje jako chyba.
    for request_name in (
        "Diagnostics", "MobileGestalt", "IORegistry", "GasGauge",
        "NAND", "WiFi", "All",
    ):
        payload = {"Request": request_name}
        if request_name == "IORegistry":
            payload.update({"CurrentPlane": "IOService"})
        elif request_name == "MobileGestalt":
            payload.update({"MobileGestaltKeys": _V20_KEYS})
        await call(f"diagnostics:raw_request:{request_name}",
                   lambda payload=payload: diag._send_recv(payload))

    # Cele stromy jsou dulezite kvuli binarnim payloadum a dynamickym nazvum.
    for plane in ("IODeviceTree", "IOService", "IOPower"):
        await call(f"ioregistry:plane:{plane}",
                   lambda plane=plane: diag.ioregistry(plane=plane))

    for name in _V20_NAMES:
        await call(f"ioregistry:name:{name}",
                   lambda name=name: diag.ioregistry(name=name))

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    def dedup(items, fields):
        seen, out = set(), []
        for item in items:
            ident = tuple(str(item.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident)
                out.append(item)
        return out

    exact_hits = dedup(exact_hits, ("goal", "source", "path", "form", "value"))
    candidates = dedup(candidates, ("source", "path", "key", "form", "value"))

    return {
        "ok": True,
        "probe": "deep-syscfg-mobilegestalt-binary-sensor-discovery-v20",
        "read_only": True,
        "udid": udid,
        "goal": "Locate exact source/path for Distance Sensor, Ambient Light Sensor and Vibrator Number",
        "expected": _V20_EXPECTED,
        "exact_value_hits": exact_hits,
        "candidates": candidates[:5000],
        "calls": calls,
        "errors": errors,
        "summary": {
            "exact_value_hits": len(exact_hits),
            "candidates": len(candidates),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

@app.route('/api/deep-sensor-discovery/<udid>', methods=['GET'])
def api_v20_deep_sensor_discovery(udid):
    try:
        result = _run_async_isolated(
            _v20_deep_sensor_discovery_collect(udid), timeout=420
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "deep-syscfg-mobilegestalt-binary-sensor-discovery-v20",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V21 RAW SOURCE CAPTURE / BINARY RECONSTRUCTION ──────────────────────────
# READ-ONLY. Uklada syrove odpovedi z lockdown/diagnostics/IORegistry vedle
# serveru, aby bylo mozne offline porovnat binarni payloady s referencnimi
# hodnotami z 3uTools. Do telefonu NIC nezapisuje.
_V21_EXPECTED = {
    "distance_sensor": "FWP8311CBQ1H6CW20",
    "ambient_light_sensor": "3E-85DF2320",
    "vibrator_number": "FTN838245WQJGJN84+XMAYM1",
}

_V21_IOREG_NAMES = (
    "prox", "als", "AppleCLCD", "AppleH10CamIn", "AppleH10PearlCam",
    "AppleSmartBattery", "AppleProxDriver", "AppleALSDriver",
    "AppleHapticsSupportLEAP", "AppleTapticEngine", "AppleHaptics",
    "vibrator", "taptic", "haptics", "rosaline", "Rosaline",
    "savage", "Savage", "yonkers", "Yonkers",
)

_V21_MG_KEYS = list(dict.fromkeys(_V20_KEYS + [
    "DeviceTree", "IODeviceTree", "SysCfg", "SysCfgDict",
    "ProximitySensorCalibration", "AmbientLightSensorCalibration",
    "RosalineSerialNumber", "VibratorNumber", "VibratorSerialNumber",
    "HapticSerialNumber", "TapticEngineSerialNumber",
]))

def _v21_jsonable(value, max_binary=4 * 1024 * 1024):
    import base64
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        cut = raw[:max_binary]
        return {
            "_type": "bytes",
            "length": len(raw),
            "hex": cut.hex(),
            "base64": base64.b64encode(cut).decode("ascii"),
            "ascii": cut.decode("ascii", errors="replace").replace("\x00", "\\0"),
            "truncated": len(raw) > max_binary,
        }
    if isinstance(value, dict):
        return {str(k): _v21_jsonable(v, max_binary) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v21_jsonable(v, max_binary) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

def _v21_raw_forms(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        yield "raw", raw
        yield "hex_ascii", raw.hex().encode("ascii")
        try:
            yield "utf16le_text", raw.decode("utf-16le", errors="ignore").encode("utf-8")
        except Exception:
            pass
    elif isinstance(value, str):
        yield "utf8", value.encode("utf-8", errors="ignore")
    elif isinstance(value, (int, float, bool)):
        yield "scalar", str(value).encode("ascii", errors="ignore")

def _v21_find_exact(label, obj):
    hits = []
    needles = {goal: expected.encode("ascii") for goal, expected in _V21_EXPECTED.items()}
    for path, key, value in _v20_walk(obj):
        for form, raw in _v21_raw_forms(value):
            upper = raw.upper()
            for goal, needle in needles.items():
                pos = upper.find(needle.upper())
                if pos >= 0:
                    hits.append({
                        "goal": goal, "expected": _V21_EXPECTED[goal],
                        "source": label, "path": path, "key": key,
                        "form": form, "offset": pos,
                    })
    return hits

async def _v21_raw_capture_collect(udid):
    import inspect
    import datetime as _dt
    ld, diag = await _open_diag(udid)
    errors, calls, exact_hits, captured = {}, [], [], {}

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            ok = value is not None
            calls.append({"source": label, "ok": ok, "result_type": type(value).__name__})
            if ok:
                captured[label] = _v21_jsonable(value)
                exact_hits.extend(_v21_find_exact(label, value))
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    try:
        av = ld.all_values
        if inspect.isawaitable(av):
            av = await av
        calls.append({"source": "lockdown:all_values", "ok": isinstance(av, dict),
                      "result_type": type(av).__name__})
        if av is not None:
            captured["lockdown:all_values"] = _v21_jsonable(av)
            exact_hits.extend(_v21_find_exact("lockdown:all_values", av))
    except Exception as exc:
        errors["lockdown:all_values"] = f"{type(exc).__name__}: {exc}"

    for domain in (
        "com.apple.mobile.battery", "com.apple.disk_usage",
        "com.apple.mobile.internal", "com.apple.mobile.lockdown",
        "com.apple.mobile.gestalt", "com.apple.mobile.iTunes",
    ):
        await capture(f"lockdown:domain:{domain}",
                      lambda domain=domain: ld.get_value(domain=domain))

    await capture("diagnostics:mobilegestalt:batch",
                  lambda: diag._send_recv({
                      "Request": "MobileGestalt",
                      "MobileGestaltKeys": _V21_MG_KEYS,
                  }))

    for request_name in ("Diagnostics", "MobileGestalt", "IORegistry",
                         "GasGauge", "NAND", "WiFi", "All"):
        payload = {"Request": request_name}
        if request_name == "IORegistry":
            payload["CurrentPlane"] = "IOService"
        elif request_name == "MobileGestalt":
            payload["MobileGestaltKeys"] = _V21_MG_KEYS
        await capture(f"diagnostics:raw:{request_name}",
                      lambda payload=payload: diag._send_recv(payload))

    for plane in ("IODeviceTree", "IOService", "IOPower"):
        await capture(f"ioregistry:plane:{plane}",
                      lambda plane=plane: diag.ioregistry(plane=plane))

    for name in _V21_IOREG_NAMES:
        await capture(f"ioregistry:name:{name}",
                      lambda name=name: diag.ioregistry(name=name))

    # Zaznamename API lockdown objektu a DiagnosticsService. To ukaze presne,
    # jake service/start metody ma nainstalovana verze pymobiledevice3.
    captured["python_api:lockdown_methods"] = sorted(
        n for n in dir(ld) if "service" in n.lower() or "value" in n.lower()
    )
    captured["python_api:diagnostics_methods"] = sorted(
        n for n in dir(diag) if not n.startswith("__")
    )

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v21", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    index = {}
    for idx, (label, value) in enumerate(captured.items(), 1):
        filename = f"{idx:03d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', label)[:120]}.json"
        path = os.path.join(capture_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        index[label] = filename

    manifest = {
        "ok": True,
        "probe": "raw-source-binary-reconstruction-v21",
        "read_only": True,
        "udid": udid,
        "expected": _V21_EXPECTED,
        "capture_dir": capture_dir,
        "files": index,
        "exact_value_hits": exact_hits,
        "calls": calls,
        "errors": errors,
        "summary": {
            "files_written": len(index),
            "exact_value_hits": len(exact_hits),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest

@app.route('/api/v21-raw-capture/<udid>', methods=['GET'])
def api_v21_raw_capture(udid):
    try:
        result = _run_async_isolated(_v21_raw_capture_collect(udid), timeout=600)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "raw-source-binary-reconstruction-v21",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200


# ─── V22 TARGETED IOREGISTRY EXTRACTOR ───────────────────────────────────────
# READ-ONLY. V21 potvrdil skutecne uzly prox/als a na iPhone XS take
# AppleHapticsSupportCallan + Actuator/audio-haptic vetve. V22 proto necili na
# dalsi obecny discovery dump, ale:
#   1) precte konkretni potvrzene uzly pres name=
#   2) v plnem IOService stromu najde jejich kontext parent -> node -> children
#   3) ulozi vsechny properties bez orezani malych binarnich kalibraci
#   4) zkusi vice binarnich reprezentaci referencnich hodnot
# Do telefonu NIC nezapisuje.

_V22_EXPECTED = dict(_V21_EXPECTED)

_V22_TARGETS = (
    "AppleHapticsSupportCallan",
    "Actuator",
    "audio-haptic",
    "haptics-support-interface",
    "AppleHapticsAudioInterface",
    "AppleAOPAudioButtonHapticDevice",
    "prox",
    "AppleProxHIDEventDriver",
    "als",
)

_V22_CONTEXT_TOKENS = (
    "haptic", "actuator", "prox", "als", "ambient", "light",
    "sensor", "aop", "audio"
)

def _v22_norm_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())

def _v22_node_identity(node):
    if not isinstance(node, dict):
        return []
    vals = []
    for key in ("name", "className", "IOClass", "device_type", "compatible"):
        value = node.get(key)
        if isinstance(value, (str, int, float)):
            vals.append(str(value))
        elif isinstance(value, (list, tuple)):
            vals.extend(str(x) for x in value if isinstance(x, (str, int, float)))
    return vals

def _v22_node_matches(node, target):
    wanted = _v22_norm_name(target)
    return any(_v22_norm_name(x) == wanted for x in _v22_node_identity(node))

def _v22_context_summary(node):
    if not isinstance(node, dict):
        return None
    out = {}
    for key in ("name", "className", "inheritance", "regEntry", "state",
                "IOClass", "device_type", "compatible"):
        if key in node:
            out[key] = _v21_jsonable(node.get(key), max_binary=1024 * 1024)
    return out

def _v22_find_contexts(tree):
    """Vrati parent -> matched node -> direct children pro kazdy potvrzeny target."""
    hits = []

    def walk(node, parent=None, path="$"):
        if isinstance(node, dict):
            for target in _V22_TARGETS:
                if _v22_node_matches(node, target):
                    children = node.get("children")
                    direct_children = []
                    if isinstance(children, list):
                        direct_children = [
                            _v22_context_summary(child)
                            for child in children
                            if isinstance(child, dict)
                        ]
                    hits.append({
                        "target": target,
                        "path": path,
                        "parent": _v22_context_summary(parent),
                        "node": _v22_context_summary(node),
                        "children": direct_children,
                    })

            for key, child in node.items():
                if isinstance(child, dict):
                    walk(child, node, f"{path}.{key}")
                elif isinstance(child, list):
                    for idx, item in enumerate(child):
                        if isinstance(item, dict):
                            walk(item, node, f"{path}.{key}[{idx}]")
        elif isinstance(node, list):
            for idx, child in enumerate(node):
                walk(child, parent, f"{path}[{idx}]")

    walk(tree)
    return hits

def _v22_expected_forms(expected):
    """Vytvori reprezentace, ktere maji smysl hledat v binarnich properties."""
    raw = expected.encode("ascii")
    forms = {
        "ascii": raw,
        "ascii_reversed": raw[::-1],
        "utf16le": expected.encode("utf-16le"),
        "utf16be": expected.encode("utf-16be"),
        "hex_text": raw.hex().encode("ascii"),
        "hex_text_upper": raw.hex().upper().encode("ascii"),
    }
    return forms

def _v22_scan_binary(label, obj):
    hits = []
    expected_forms = {
        goal: _v22_expected_forms(expected)
        for goal, expected in _V22_EXPECTED.items()
    }

    for path, key, value in _v20_walk(obj):
        blobs = []
        if isinstance(value, (bytes, bytearray, memoryview)):
            blobs.append(("raw_property", bytes(value)))
        elif isinstance(value, str):
            blobs.append(("utf8_property", value.encode("utf-8", errors="ignore")))

        for property_form, blob in blobs:
            for goal, forms in expected_forms.items():
                for needle_form, needle in forms.items():
                    pos = blob.upper().find(needle.upper())
                    if pos >= 0:
                        hits.append({
                            "goal": goal,
                            "expected": _V22_EXPECTED[goal],
                            "source": label,
                            "path": path,
                            "key": key,
                            "property_form": property_form,
                            "needle_form": needle_form,
                            "offset": pos,
                            "property_length": len(blob),
                        })
    return hits

async def _v22_targeted_ioreg_collect(udid):
    import inspect
    import datetime as _dt

    ld, diag = await _open_diag(udid)
    errors, calls, captures, exact_hits = {}, [], {}, []

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            ok = value is not None and value != {}
            calls.append({
                "source": label,
                "ok": bool(ok),
                "result_type": type(value).__name__,
            })
            if value is not None:
                captures[label] = _v21_jsonable(value, max_binary=16 * 1024 * 1024)
                exact_hits.extend(_v22_scan_binary(label, value))
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    # Plny strom je potreba hlavne pro skutecnou topologii parent/node/children.
    ioservice = await capture(
        "ioregistry:plane:IOService",
        lambda: diag.ioregistry(plane="IOService"),
    )

    # IODeviceTree muze obsahovat kalibracni data na parent vetvi senzoru.
    iodevicetree = await capture(
        "ioregistry:plane:IODeviceTree",
        lambda: diag.ioregistry(plane="IODeviceTree"),
    )

    # Prime name= dotazy na V21 potvrzene / objevene uzly.
    direct_results = {}
    for target in _V22_TARGETS:
        value = await capture(
            f"ioregistry:name:{target}",
            lambda target=target: diag.ioregistry(name=target),
        )
        direct_results[target] = value

    # Prox calibration z lockdownu zachovame jako kontrolni referenci.
    for key in (
        "ProximitySensorCalibration",
        "AmbientLightSensorCalibration",
        "VibratorNumber",
        "VibratorSerialNumber",
        "HapticSerialNumber",
        "TapticEngineSerialNumber",
    ):
        await capture(
            f"lockdown:key:{key}",
            lambda key=key: ld.get_value(key=key),
        )

    contexts = {
        "IOService": _v22_find_contexts(ioservice) if ioservice else [],
        "IODeviceTree": _v22_find_contexts(iodevicetree) if iodevicetree else [],
    }

    # Extra candidates: cesty/klice v target captures, ktere vypadaji jako
    # serial/calibration/manufacturer/module data. Zachovame i binarni hodnotu.
    candidate_tokens = (
        "serial", "snum", "calib", "factory", "module", "vendor",
        "manufacturer", "part", "prox", "distance", "ambient", "als",
        "haptic", "actuator", "taptic"
    )
    candidates = []
    for label, original in [
        (f"ioregistry:name:{target}", direct_results.get(target))
        for target in _V22_TARGETS
    ]:
        if original is None:
            continue
        for path, key, value in _v20_walk(original):
            hay = f"{path} {key}".lower()
            if any(token in hay for token in candidate_tokens):
                if isinstance(value, (bytes, bytearray, memoryview, str, int, float, bool)):
                    candidates.append({
                        "source": label,
                        "path": path,
                        "key": key,
                        "value": _v21_jsonable(value, max_binary=16 * 1024 * 1024),
                    })

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v22", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    files = {}
    payloads = {
        **captures,
        "v22:target_contexts": contexts,
        "v22:candidates": candidates,
    }
    for idx, (label, value) in enumerate(payloads.items(), 1):
        filename = f"{idx:03d}_{re.sub(r'[^A-Za-z0-9._-]+', '_', label)[:120]}.json"
        with open(os.path.join(capture_dir, filename), "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        files[label] = filename

    manifest = {
        "ok": True,
        "probe": "targeted-ioregistry-extractor-v22",
        "read_only": True,
        "udid": udid,
        "expected": _V22_EXPECTED,
        "targets": list(_V22_TARGETS),
        "capture_dir": capture_dir,
        "files": files,
        "exact_value_hits": exact_hits,
        "contexts": {
            plane: len(items) for plane, items in contexts.items()
        },
        "candidate_count": len(candidates),
        "calls": calls,
        "errors": errors,
        "summary": {
            "files_written": len(files),
            "exact_value_hits": len(exact_hits),
            "contexts_total": sum(len(x) for x in contexts.values()),
            "candidates": len(candidates),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    return manifest

@app.route('/api/v22-targeted-ioreg/<udid>', methods=['GET'])
def api_v22_targeted_ioreg(udid):
    try:
        result = _run_async_isolated(_v22_targeted_ioreg_collect(udid), timeout=600)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "targeted-ioregistry-extractor-v22",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V23 APPLE DIAGNOSTIC DATA / SYSCFG ALS EXTRACTOR ────────────────────────
# READ-ONLY. V22 potvrdil ALS topologii, ale hledana hodnota 3E-85DF2320 nebyla
# v beznych ALS properties. V23 cte AppleDiagnosticDataAccessReadOnly a kompletni
# AppleDiagnosticData* payloady, rekurzivne rozbaluje bytes/base64/plist/zlib/gzip
# a hleda referencni ALS hodnotu i serial-like kandidaty v binarnich blocich.

_V23_ALS_EXPECTED = "3E-85DF2320"
_V23_TARGETS = (
    "AppleDiagnosticDataAccessReadOnly",
    "AppleDiagnosticDataAccess",
    "AppleDiagnosticData",
)

def _v23_safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))[:140] or "capture"

def _v23_printable_strings(raw, min_len=4):
    out = []
    for match in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, raw):
        try:
            out.append({
                "offset": match.start(),
                "value": match.group(0).decode("ascii", errors="replace"),
            })
        except Exception:
            pass
    return out

def _v23_serial_candidates(raw):
    """Vrati ASCII tokeny podobne 3uTools component identifikatorum."""
    found = []
    patterns = (
        rb"(?<![A-Za-z0-9])[A-Fa-f0-9]{2}-[A-Fa-f0-9]{8}(?![A-Za-z0-9])",
        rb"(?<![A-Za-z0-9])[A-Z0-9]{2,6}-[A-Z0-9]{6,24}(?![A-Za-z0-9])",
        rb"(?<![A-Za-z0-9])[A-Z0-9+]{12,32}(?![A-Za-z0-9])",
    )
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, raw, flags=re.I):
            value = m.group(0).decode("ascii", errors="ignore")
            ident = (m.start(), value.upper())
            if ident not in seen:
                seen.add(ident)
                found.append({"offset": m.start(), "value": value})
    return sorted(found, key=lambda x: x["offset"])

def _v23_expected_forms():
    value = _V23_ALS_EXPECTED
    compact = re.sub(r"[^A-Fa-f0-9]", "", value)
    forms = {
        "ascii": value.encode("ascii"),
        "ascii_lower": value.lower().encode("ascii"),
        "ascii_reversed": value.encode("ascii")[::-1],
        "utf16le": value.encode("utf-16le"),
        "utf16be": value.encode("utf-16be"),
        "compact_ascii": compact.encode("ascii"),
    }
    try:
        forms["hex_bytes"] = bytes.fromhex(compact)
        forms["hex_bytes_reversed"] = bytes.fromhex(compact)[::-1]
    except Exception:
        pass
    return forms

def _v23_scan_blob(source, path, raw):
    hits = []
    for form, needle in _v23_expected_forms().items():
        if not needle:
            continue
        start = 0
        while True:
            pos = raw.lower().find(needle.lower(), start)
            if pos < 0:
                break
            lo, hi = max(0, pos - 96), min(len(raw), pos + len(needle) + 96)
            hits.append({
                "source": source, "path": path, "needle_form": form,
                "offset": pos, "blob_length": len(raw),
                "context_hex": raw[lo:hi].hex(),
                "context_ascii": raw[lo:hi].decode("ascii", errors="replace").replace("\x00", "\\0"),
            })
            start = pos + 1
    return hits

def _v23_decode_layers(source, path, value, max_depth=4):
    """Rekurzivne vrati raw vrstvy: property bytes, base64, plist a komprese."""
    import base64, binascii, gzip, zlib, plistlib
    layers, seen = [], set()

    def add(kind, pth, raw, depth):
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            return
        raw = bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        ident = (kind, digest)
        if ident in seen:
            return
        seen.add(ident)
        layers.append({"kind": kind, "path": pth, "raw": raw, "depth": depth})
        if depth >= max_depth or not raw:
            return

        # Binary/XML plist embedded directly in a property.
        if raw.startswith(b"bplist00") or raw.lstrip().startswith(b"<?xml") or raw.lstrip().startswith(b"<plist"):
            try:
                obj = plistlib.loads(raw)
                walk(obj, pth + "::plist", depth + 1)
            except Exception:
                pass

        # gzip / zlib.
        try:
            if raw.startswith(b"\x1f\x8b"):
                add("gzip", pth + "::gzip", gzip.decompress(raw), depth + 1)
        except Exception:
            pass
        for wbits, label in ((zlib.MAX_WBITS, "zlib"), (-zlib.MAX_WBITS, "deflate")):
            try:
                dec = zlib.decompress(raw, wbits)
                if dec and dec != raw:
                    add(label, pth + "::" + label, dec, depth + 1)
            except Exception:
                pass

        # Base64 text, including plist values serialized as strings.
        try:
            stripped = re.sub(rb"\s+", b"", raw)
            if len(stripped) >= 16 and len(stripped) % 4 == 0 and re.fullmatch(rb"[A-Za-z0-9+/=]+", stripped):
                dec = base64.b64decode(stripped, validate=True)
                if dec and dec != raw:
                    add("base64", pth + "::base64", dec, depth + 1)
        except (ValueError, binascii.Error):
            pass

    def walk(obj, pth, depth=0):
        if isinstance(obj, (bytes, bytearray, memoryview)):
            add("bytes", pth, obj, depth)
        elif isinstance(obj, str):
            add("string_utf8", pth, obj.encode("utf-8", errors="ignore"), depth)
        elif isinstance(obj, dict):
            for key, child in obj.items():
                walk(child, f"{pth}.{key}", depth)
        elif isinstance(obj, (list, tuple)):
            for idx, child in enumerate(obj):
                walk(child, f"{pth}[{idx}]", depth)

    walk(value, path)
    return layers

async def _v23_apple_diagnostic_data_collect(udid):
    import inspect
    import datetime as _dt

    ld, diag = await _open_diag(udid)
    errors, calls, captures = {}, [], {}
    exact_hits, layer_index, string_candidates = [], [], []

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            ok = value is not None and value != {}
            calls.append({"source": label, "ok": bool(ok), "result_type": type(value).__name__})
            if value is not None:
                captures[label] = value
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    # Prime diagnosticke uzly.
    for target in _V23_TARGETS:
        await capture(
            f"ioregistry:name:{target}",
            lambda target=target: diag.ioregistry(name=target),
        )

    # Plne stromy kvuli pripadu, kdy je AppleDiagnosticData* property na parentu.
    await capture("ioregistry:plane:IOService",
                  lambda: diag.ioregistry(plane="IOService"))
    await capture("ioregistry:plane:IODeviceTree",
                  lambda: diag.ioregistry(plane="IODeviceTree"))

    # Lockdown keys jako levny doplnkovy pokus.
    for key in ("AppleDiagnosticDataDisplay", "AppleDiagnosticDataSysCfg",
                "SysCfg", "SysCfgDict", "AmbientLightSensorCalibration"):
        await capture(f"lockdown:key:{key}", lambda key=key: ld.get_value(key=key))

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v23", f"{safe_udid}_{stamp}")
    raw_dir = os.path.join(capture_dir, "raw_layers")
    os.makedirs(raw_dir, exist_ok=True)

    # Zpracuj jen AppleDiagnosticData/SysCfg/ALS relevantni properties z broad
    # stromu, ale prime diagnostic nodes zpracuj cele.
    relevant_tokens = ("applediagnosticdata", "syscfg", "als", "ambient", "light", "calib")
    selected = []
    for label, obj in captures.items():
        direct_diag = label.startswith("ioregistry:name:AppleDiagnosticData")
        if direct_diag:
            selected.append((label, "$", obj))
            continue
        for path, key, value in _v20_walk(obj):
            hay = f"{path} {key}".lower()
            if any(token in hay for token in relevant_tokens):
                selected.append((label, path, value))

    seen_layers = set()
    for source, path, value in selected:
        for layer in _v23_decode_layers(source, path, value):
            raw = layer["raw"]
            digest = hashlib.sha256(raw).hexdigest()
            ident = (source, layer["path"], layer["kind"], digest)
            if ident in seen_layers:
                continue
            seen_layers.add(ident)

            filename = f"{len(layer_index)+1:05d}_{_v23_safe_name(layer['kind'])}_{digest[:16]}.bin"
            with open(os.path.join(raw_dir, filename), "wb") as fh:
                fh.write(raw)

            hits = _v23_scan_blob(source, layer["path"], raw)
            exact_hits.extend(hits)
            serials = _v23_serial_candidates(raw)
            printable = _v23_printable_strings(raw, min_len=4)

            layer_index.append({
                "source": source,
                "path": layer["path"],
                "kind": layer["kind"],
                "depth": layer["depth"],
                "length": len(raw),
                "sha256": digest,
                "file": os.path.join("raw_layers", filename),
                "exact_hits": len(hits),
                "serial_candidates": serials[:500],
                "printable_strings": printable[:2000],
            })
            for item in serials:
                string_candidates.append({
                    "source": source, "path": layer["path"],
                    "kind": layer["kind"], **item
                })

    # JSON kopie vsech captures bez maleho limitu.
    files = {}
    for idx, (label, value) in enumerate(captures.items(), 1):
        filename = f"{idx:03d}_{_v23_safe_name(label)}.json"
        with open(os.path.join(capture_dir, filename), "w", encoding="utf-8") as fh:
            json.dump(_v21_jsonable(value, max_binary=64 * 1024 * 1024),
                      fh, ensure_ascii=False, indent=2)
        files[label] = filename

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    manifest = {
        "ok": True,
        "probe": "apple-diagnostic-data-syscfg-als-extractor-v23",
        "read_only": True,
        "udid": udid,
        "expected_als": _V23_ALS_EXPECTED,
        "capture_dir": capture_dir,
        "files": files,
        "exact_value_hits": exact_hits,
        "serial_candidates": string_candidates[:5000],
        "raw_layers": layer_index,
        "calls": calls,
        "errors": errors,
        "summary": {
            "files_written": len(files),
            "raw_layers": len(layer_index),
            "exact_value_hits": len(exact_hits),
            "serial_candidates": len(string_candidates),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest

@app.route('/api/v23-apple-diagnostic-data/<udid>', methods=['GET'])
def api_v23_apple_diagnostic_data(udid):
    try:
        result = _run_async_isolated(_v23_apple_diagnostic_data_collect(udid), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "apple-diagnostic-data-syscfg-als-extractor-v23",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V24 SYSCFG RECORD / ALS VALUE DECODER ───────────────────────────────────
# READ-ONLY. V23 potvrdil 131072B AppleDiagnosticDataSysCfg v
# AppleDiagnosticDataAccessReadOnly. V24 nehledata jen ASCII string: rozebira
# SysCfg po record/slot hranicich a zkousi binarni reprezentace ALS identifikatoru.

_V24_ALS_EXPECTED = "3E-85DF2320"

def _v24_als_parts(value=_V24_ALS_EXPECTED):
    m = re.fullmatch(r"([0-9A-Fa-f]{2})-([0-9A-Fa-f]{8})", value.strip())
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)

def _v24_candidate_forms(prefix, number):
    import struct
    p = bytes([prefix])
    be = struct.pack(">I", number)
    le = struct.pack("<I", number)
    p16be = struct.pack(">H", prefix)
    p16le = struct.pack("<H", prefix)
    forms = {
        "prefix_u8+u32be": p + be,
        "prefix_u8+u32le": p + le,
        "u32be+prefix_u8": be + p,
        "u32le+prefix_u8": le + p,
        "prefix_u16be+u32be": p16be + be,
        "prefix_u16le+u32le": p16le + le,
        "u32be": be,
        "u32le": le,
        "hex5": bytes.fromhex(f"{prefix:02X}{number:08X}"),
        "hex5_reversed": bytes.fromhex(f"{prefix:02X}{number:08X}")[::-1],
    }
    return forms

def _v24_ascii_tag(raw):
    if not raw:
        return None
    txt = raw.decode("ascii", errors="ignore").strip("\x00").strip()
    if 2 <= len(txt) <= 16 and all(32 <= ord(c) <= 126 for c in txt):
        return txt
    return None

def _v24_record_views(raw):
    """Generuje vice pohledu na SysCfg. V23 ukazal silnou 20B periodicitu tagu."""
    views = []
    # Pevne sloty. Offset 4 je dulezity: prvni pozorovany tag #BLM zacina na 24.
    for slot in (16, 20, 24, 32, 64):
        for phase in range(slot):
            rows = []
            for off in range(phase, len(raw) - slot + 1, slot):
                chunk = raw[off:off + slot]
                printable = sum(1 for b in chunk if 32 <= b <= 126)
                if printable >= 3:
                    rows.append((off, chunk))
            if rows:
                views.append((f"slot{slot}:phase{phase}", rows))

    # Dynamicke tag-like runy; payload je kontext do dalsiho tagu, max 256 B.
    tag_re = re.compile(rb"(?<![A-Za-z0-9])[#A-Za-z][A-Za-z0-9_]{2,15}")
    matches = list(tag_re.finditer(raw))
    rows = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(raw), m.start() + 256)
        end = min(end, m.start() + 256)
        rows.append((m.start(), raw[m.start():end]))
    if rows:
        views.append(("dynamic-tags", rows))
    return views

def _v24_decode_als_from_blob(raw):
    parts = _v24_als_parts()
    if not parts:
        return [], []
    prefix, number = parts
    forms = _v24_candidate_forms(prefix, number)
    exact = []
    number_hits = []

    for name, needle in forms.items():
        start = 0
        while needle:
            pos = raw.find(needle, start)
            if pos < 0:
                break
            lo, hi = max(0, pos - 32), min(len(raw), pos + len(needle) + 32)
            item = {
                "form": name, "offset": pos, "length": len(needle),
                "context_hex": raw[lo:hi].hex(),
                "context_ascii": raw[lo:hi].decode("ascii", errors="replace").replace("\x00", "\\0"),
            }
            if "prefix" in name or name.startswith("hex5"):
                exact.append(item)
            else:
                number_hits.append(item)
            start = pos + 1
    return exact, number_hits

def _v24_rank_records(raw, number_hits):
    hit_offsets = [x["offset"] for x in number_hits]
    ranked = []
    seen = set()
    tokens = ("als", "ambient", "light", "cal", "sensor", "lcia", "lcig",
              "came", "lcon", "gclc", "shlc", "brts")
    for view, rows in _v24_record_views(raw):
        for off, chunk in rows:
            tag = _v24_ascii_tag(chunk[:16])
            ascii_text = chunk.decode("ascii", errors="ignore").replace("\x00", "")
            near = min((abs(off - h) for h in hit_offsets), default=10**9)
            score = 0
            low = ascii_text.lower()
            score += sum(12 for t in tokens if t in low)
            if near <= 8: score += 100
            elif near <= 32: score += 60
            elif near <= 128: score += 25
            printable = sum(1 for b in chunk if 32 <= b <= 126)
            score += min(10, printable // 2)
            if score <= 0:
                continue
            ident = (off, chunk)
            if ident in seen:
                continue
            seen.add(ident)
            ranked.append({
                "view": view, "offset": off, "length": len(chunk), "tag": tag,
                "score": score, "nearest_number_hit": None if near == 10**9 else near,
                "hex": chunk.hex(), "ascii": ascii_text,
            })
    ranked.sort(key=lambda x: (-x["score"], x["offset"]))
    return ranked[:1000]

async def _v24_syscfg_als_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls = {}, []
    syscfg = None
    source = None

    try:
        for target in _V23_TARGETS:
            try:
                obj = diag.ioregistry(name=target)
                if inspect.isawaitable(obj):
                    obj = await obj
                calls.append({"source": f"ioregistry:name:{target}", "ok": bool(obj)})
                if isinstance(obj, dict):
                    candidate = obj.get("AppleDiagnosticDataSysCfg")
                    if isinstance(candidate, (bytes, bytearray, memoryview)):
                        syscfg = bytes(candidate)
                        source = f"ioregistry:name:{target}::$.AppleDiagnosticDataSysCfg"
                        break
                    # Rekurzivni fallback, kdyby Apple property presunul.
                    for path, key, value in _v20_walk(obj):
                        if str(key).lower() == "applediagnosticdatasyscfg" and isinstance(value, (bytes, bytearray, memoryview)):
                            syscfg = bytes(value)
                            source = f"ioregistry:name:{target}::{path}"
                            break
                if syscfg is not None:
                    break
            except Exception as exc:
                errors[f"ioregistry:name:{target}"] = f"{type(exc).__name__}: {exc}"
                calls.append({"source": f"ioregistry:name:{target}", "ok": False,
                              "error": errors[f"ioregistry:name:{target}"]})
    finally:
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass

    if syscfg is None:
        return {"ok": False, "probe": "syscfg-record-als-decoder-v24",
                "udid": udid, "expected_als": _V24_ALS_EXPECTED,
                "error": "AppleDiagnosticDataSysCfg nebyl nalezen.",
                "calls": calls, "errors": errors}

    exact, number_hits = _v24_decode_als_from_blob(syscfg)
    ranked = _v24_rank_records(syscfg, number_hits)

    # Pokud je nalezen kompletni binarni tvar, vrat rovnou dekodovanou 3uTools hodnotu.
    decoded = _V24_ALS_EXPECTED if exact else None
    confidence = "exact_binary" if exact else ("number_payload_found" if number_hits else "record_candidates_only")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v24", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)
    with open(os.path.join(capture_dir, "AppleDiagnosticDataSysCfg.bin"), "wb") as fh:
        fh.write(syscfg)

    result = {
        "ok": True, "probe": "syscfg-record-als-decoder-v24", "read_only": True,
        "udid": udid, "source": source, "syscfg_length": len(syscfg),
        "expected_als": _V24_ALS_EXPECTED,
        "ambient_light_sensor": decoded,
        "confidence": confidence,
        "exact_binary_hits": exact,
        "number_payload_hits": number_hits,
        "ranked_records": ranked,
        "capture_dir": capture_dir,
        "calls": calls, "errors": errors,
        "summary": {
            "exact_binary_hits": len(exact),
            "number_payload_hits": len(number_hits),
            "ranked_records": len(ranked),
        },
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return result

@app.route('/api/v24-syscfg-als-decode/<udid>', methods=['GET'])
def api_v24_syscfg_als_decode(udid):
    try:
        result = _run_async_isolated(_v24_syscfg_als_collect(udid), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "syscfg-record-als-decoder-v24",
            "udid": udid, "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V25 SYSCFG 20-BYTE RECORD MAP / ALS FORENSIC DECODER ───────────────────
# V24 potvrdil, ze hledana ALS hodnota neni v SysCfg ani jako kompletni binarni
# tvar, ani jako samotne 0x85DF2320. V23 dump ale ukazuje pravidelne 20B zaznamy
# od offsetu 24: 4B tag + 16B payload. V25 mapuje skutecne zaznamy bez posuvnych
# oken, dekoduje payload po endian slovech a uklada ALS/light/calibration okoli.
# READ-ONLY.

_V25_INTEREST = (
    "LCIA", "LCIG", "LCXR", "GCLC", "SHLC", "LCON", "BRTS",
    "CAME", "CAMA", "CAMW", "CAMB", "PSGC", "TORA", "TORG", "TORC",
)

def _v25_clean_tag(tag_raw):
    txt = tag_raw.decode("ascii", errors="replace")
    return txt.replace("\x00", "\\0")

def _v25_words(payload):
    import struct
    out = {}
    for width, code in ((2, "H"), (4, "I"), (8, "Q")):
        if len(payload) % width:
            continue
        out[f"u{width*8}_le"] = [
            int.from_bytes(payload[i:i+width], "little", signed=False)
            for i in range(0, len(payload), width)
        ]
        out[f"u{width*8}_be"] = [
            int.from_bytes(payload[i:i+width], "big", signed=False)
            for i in range(0, len(payload), width)
        ]
    if len(payload) == 16:
        try:
            out["f32_le"] = list(struct.unpack("<4f", payload))
            out["f32_be"] = list(struct.unpack(">4f", payload))
        except Exception:
            pass
    return out

def _v25_record_map(raw):
    records = []
    # V23 prokazal prvni record na 24 a dalsi tagy presne +20 B.
    for off in range(24, len(raw) - 19, 20):
        chunk = raw[off:off+20]
        tag_raw, payload = chunk[:4], chunk[4:]
        printable_tag = all(32 <= b <= 126 for b in tag_raw)
        tag = _v25_clean_tag(tag_raw)
        ascii_payload = payload.decode("ascii", errors="replace").replace("\x00", "\\0")
        rec = {
            "index": (off - 24) // 20,
            "offset": off,
            "tag": tag,
            "tag_hex": tag_raw.hex(),
            "tag_printable": printable_tag,
            "payload_hex": payload.hex(),
            "payload_ascii": ascii_payload,
            "words": _v25_words(payload),
        }
        records.append(rec)
    return records

def _v25_score_als_record(rec):
    tag = rec["tag"].upper()
    score = 0
    reasons = []
    for token, points in (
        ("LCIA", 100), ("LCIG", 100), ("ALS", 100), ("LIGHT", 80),
        ("GCLC", 55), ("SHLC", 55), ("LCON", 50), ("LCXR", 50),
        ("BRTS", 45), ("CAME", 35), ("CAL", 35), ("SENSOR", 35),
    ):
        if token in tag:
            score += points
            reasons.append(token)
    return score, reasons

def _v25_expected_math(rec):
    """Porovna numericke casti 3E-85DF2320 s kazdym payload slovem."""
    prefix = 0x3E
    number = 0x85DF2320
    matches = []
    for form, vals in rec["words"].items():
        for i, val in enumerate(vals):
            # Nejen equality: XOR/delta nechavame venku, aby se dalo porovnat
            # vice telefonu a odvodit transformaci bez falsifikace vysledku.
            if val in (prefix, number):
                matches.append({"form": form, "index": i, "value": val,
                                "kind": "prefix" if val == prefix else "number"})
    return matches

async def _v25_syscfg_record_map_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls = {}, []
    syscfg = None
    source = None
    try:
        for target in _V23_TARGETS:
            try:
                obj = diag.ioregistry(name=target)
                if inspect.isawaitable(obj):
                    obj = await obj
                calls.append({"source": f"ioregistry:name:{target}", "ok": bool(obj)})
                if isinstance(obj, dict):
                    for path, key, value in _v20_walk(obj):
                        if str(key).lower() == "applediagnosticdatasyscfg" and isinstance(value, (bytes, bytearray, memoryview)):
                            syscfg = bytes(value)
                            source = f"ioregistry:name:{target}::{path}"
                            break
                if syscfg is not None:
                    break
            except Exception as exc:
                errors[f"ioregistry:name:{target}"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass

    if syscfg is None:
        return {"ok": False, "probe": "syscfg-20byte-record-map-v25",
                "udid": udid, "error": "AppleDiagnosticDataSysCfg nebyl nalezen.",
                "calls": calls, "errors": errors}

    records = _v25_record_map(syscfg)
    candidates = []
    exact_numeric = []
    for rec in records:
        score, reasons = _v25_score_als_record(rec)
        numeric = _v25_expected_math(rec)
        if numeric:
            exact_numeric.append({
                "offset": rec["offset"], "tag": rec["tag"], "matches": numeric,
                "payload_hex": rec["payload_hex"],
            })
        if score:
            item = dict(rec)
            item["score"] = score
            item["reasons"] = reasons
            item["expected_numeric_matches"] = numeric
            candidates.append(item)

    candidates.sort(key=lambda x: (-x["score"], x["offset"]))

    # U kazdeho top kandidata uloz 5 zaznamu pred/po. To je dulezite pro
    # rekonstrukci vztahu mezi lCIA/lCIG a sousednimi factory tagy.
    neighborhoods = []
    by_index = {r["index"]: r for r in records}
    for cand in candidates[:40]:
        idx = cand["index"]
        neighborhood = [
            by_index[i] for i in range(max(0, idx-5), idx+6) if i in by_index
        ]
        neighborhoods.append({
            "center_index": idx, "center_offset": cand["offset"],
            "center_tag": cand["tag"], "records": neighborhood,
        })

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v25", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    with open(os.path.join(capture_dir, "AppleDiagnosticDataSysCfg.bin"), "wb") as fh:
        fh.write(syscfg)
    with open(os.path.join(capture_dir, "records_20byte.json"), "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(capture_dir, "als_candidate_neighborhoods.json"), "w", encoding="utf-8") as fh:
        json.dump(neighborhoods, fh, ensure_ascii=False, indent=2)

    result = {
        "ok": True,
        "probe": "syscfg-20byte-record-map-v25",
        "read_only": True,
        "udid": udid,
        "source": source,
        "syscfg_length": len(syscfg),
        "expected_als": _V24_ALS_EXPECTED,
        "record_layout": {"start_offset": 24, "record_size": 20,
                          "tag_size": 4, "payload_size": 16},
        "records_total": len(records),
        "als_candidates": candidates[:100],
        "exact_numeric_matches": exact_numeric,
        "neighborhoods": neighborhoods,
        "capture_dir": capture_dir,
        "files": {
            "syscfg_bin": "AppleDiagnosticDataSysCfg.bin",
            "records": "records_20byte.json",
            "als_neighborhoods": "als_candidate_neighborhoods.json",
        },
        "calls": calls,
        "errors": errors,
        "summary": {
            "records_total": len(records),
            "als_candidates": len(candidates),
            "exact_numeric_matches": len(exact_numeric),
            "neighborhoods": len(neighborhoods),
        },
        "conclusion": (
            "V25 maps the confirmed 20-byte SysCfg record layout. "
            "No value is labeled as ALS until its tag/payload mapping is proven."
        ),
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return result

@app.route('/api/v25-syscfg-record-map/<udid>', methods=['GET'])
def api_v25_syscfg_record_map(udid):
    try:
        result = _run_async_isolated(_v25_syscfg_record_map_collect(udid), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "syscfg-20byte-record-map-v25",
            "udid": udid, "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V26 SYSCFG BTNC DIRECTORY / POINTED-BLOB DECODER ────────────────────────
# V25 odhalil skutecny vyznam velke casti 20B zaznamu:
#   BTNC + [4B child tag] + [u32 size] + [u32 offset] + [u32 flags]
# Napr. payload 6c4354473c00000014d30100ffffffff =
#   child=lCTG, size=60, offset=119572, flags=0xffffffff.
# V26 proto prestava povazovat BTNC payload za "nahodna cisla" a dereferencuje
# jeho ukazatele do AppleDiagnosticDataSysCfg. READ-ONLY.

def _v26_ascii4(raw4):
    try:
        return raw4.decode("ascii")
    except Exception:
        return raw4.decode("ascii", errors="replace")

def _v26_expected_patterns():
    import struct
    text_value = _V24_ALS_EXPECTED
    prefix_s, number_s = text_value.split("-", 1)
    prefix = int(prefix_s, 16)
    number = int(number_s, 16)
    compact = bytes.fromhex(prefix_s + number_s)
    return {
        "ascii_exact": text_value.encode("ascii"),
        "ascii_lower": text_value.lower().encode("ascii"),
        "ascii_compact": (prefix_s + number_s).encode("ascii"),
        "binary_5b": compact,
        "number_u32_be": struct.pack(">I", number),
        "number_u32_le": struct.pack("<I", number),
        "prefix_u8": bytes([prefix]),
        "prefix_u16_be": struct.pack(">H", prefix),
        "prefix_u16_le": struct.pack("<H", prefix),
    }

def _v26_find_all(haystack, needle):
    hits = []
    if not needle:
        return hits
    pos = 0
    while True:
        pos = haystack.find(needle, pos)
        if pos < 0:
            break
        hits.append(pos)
        pos += 1
    return hits

def _v26_parse_btnc_directory(raw):
    entries = []
    invalid = []
    # Potvrzeny record table z V25 zacina na 24 a ma stride 20.
    for rec_off in range(24, len(raw) - 19, 20):
        rec = raw[rec_off:rec_off+20]
        if rec[:4] != b"BTNC":
            continue
        payload = rec[4:]
        child_raw = payload[:4]
        size = int.from_bytes(payload[4:8], "little", signed=False)
        target_off = int.from_bytes(payload[8:12], "little", signed=False)
        flags = int.from_bytes(payload[12:16], "little", signed=False)
        child = _v26_ascii4(child_raw)
        valid = (
            all(32 <= b <= 126 for b in child_raw)
            and size > 0
            and target_off < len(raw)
            and target_off + size <= len(raw)
        )
        item = {
            "record_offset": rec_off,
            "child_tag": child,
            "child_tag_hex": child_raw.hex(),
            "size": size,
            "target_offset": target_off,
            "target_end": target_off + size,
            "flags_u32": flags,
            "flags_hex": f"{flags:08x}",
            "valid_pointer": valid,
        }
        (entries if valid else invalid).append(item)
    return entries, invalid

def _v26_blob_preview(blob, limit=128):
    import base64
    preview = blob[:limit]
    return {
        "hex": preview.hex(),
        "ascii": preview.decode("ascii", errors="replace").replace("\x00", "\\0"),
        "base64": base64.b64encode(preview).decode("ascii"),
        "preview_bytes": len(preview),
    }

async def _v26_syscfg_btnc_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls = {}, []
    syscfg = None
    source = None
    try:
        for target in _V23_TARGETS:
            try:
                obj = diag.ioregistry(name=target)
                if inspect.isawaitable(obj):
                    obj = await obj
                calls.append({"source": f"ioregistry:name:{target}", "ok": bool(obj)})
                if isinstance(obj, dict):
                    for path, key, value in _v20_walk(obj):
                        if str(key).lower() == "applediagnosticdatasyscfg" and isinstance(value, (bytes, bytearray, memoryview)):
                            syscfg = bytes(value)
                            source = f"ioregistry:name:{target}::{path}"
                            break
                if syscfg is not None:
                    break
            except Exception as exc:
                errors[f"ioregistry:name:{target}"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass

    if syscfg is None:
        return {
            "ok": False, "probe": "syscfg-btnc-directory-v26", "udid": udid,
            "error": "AppleDiagnosticDataSysCfg nebyl nalezen.",
            "calls": calls, "errors": errors,
        }

    entries, invalid = _v26_parse_btnc_directory(syscfg)
    patterns = _v26_expected_patterns()
    decoded = []
    all_hits = []

    for entry in entries:
        blob = syscfg[entry["target_offset"]:entry["target_end"]]
        hit_map = {}
        for name, needle in patterns.items():
            positions = _v26_find_all(blob, needle)
            # Samotny prefix je velmi slaby signal; ukladame ho, ale neoznacujeme
            # jako exact ALS match.
            if positions:
                hit_map[name] = positions
                for rel in positions:
                    all_hits.append({
                        "child_tag": entry["child_tag"],
                        "pattern": name,
                        "relative_offset": rel,
                        "absolute_offset": entry["target_offset"] + rel,
                        "weak_prefix_only": name.startswith("prefix_"),
                    })

        item = dict(entry)
        item["preview"] = _v26_blob_preview(blob)
        item["expected_pattern_hits"] = hit_map
        item["exact_als_candidate"] = any(
            k in hit_map for k in (
                "ascii_exact", "ascii_lower", "ascii_compact",
                "binary_5b", "number_u32_be", "number_u32_le",
            )
        )
        decoded.append(item)

    exact_candidates = [x for x in decoded if x["exact_als_candidate"]]

    # Seradime take tagy, ktere nazvem vypadaji jako light/display/sensor/cal.
    semantic_tokens = ("ALS", "LIG", "LIT", "LUX", "LC", "DISP", "BRT", "CAL", "SNS", "SEN")
    semantic = []
    for item in decoded:
        upper = item["child_tag"].upper()
        reasons = [t for t in semantic_tokens if t in upper]
        if reasons:
            row = dict(item)
            row["semantic_reasons"] = reasons
            semantic.append(row)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v26", f"{safe_udid}_{stamp}")
    blobs_dir = os.path.join(capture_dir, "btnc_blobs")
    os.makedirs(blobs_dir, exist_ok=True)

    with open(os.path.join(capture_dir, "AppleDiagnosticDataSysCfg.bin"), "wb") as fh:
        fh.write(syscfg)

    # Uloz vsechny validni pointed blobs samostatne pro offline diff vice telefonu.
    filename_counts = {}
    blob_manifest = []
    for item in decoded:
        tag_safe = re.sub(r"[^A-Za-z0-9._-]", "_", item["child_tag"])
        n = filename_counts.get(tag_safe, 0)
        filename_counts[tag_safe] = n + 1
        filename = f"{item['record_offset']:06d}_{tag_safe}_{n}.bin"
        blob = syscfg[item["target_offset"]:item["target_end"]]
        with open(os.path.join(blobs_dir, filename), "wb") as fh:
            fh.write(blob)
        blob_manifest.append({
            "file": filename, "child_tag": item["child_tag"],
            "record_offset": item["record_offset"],
            "target_offset": item["target_offset"], "size": item["size"],
        })

    result = {
        "ok": True,
        "probe": "syscfg-btnc-directory-v26",
        "read_only": True,
        "udid": udid,
        "source": source,
        "syscfg_length": len(syscfg),
        "expected_als": _V24_ALS_EXPECTED,
        "btnc_layout": {
            "outer_tag": "BTNC",
            "payload": "child_tag[4] + size_u32_le + target_offset_u32_le + flags_u32_le",
        },
        "valid_entries": len(entries),
        "invalid_btnc_records": len(invalid),
        "entries": decoded,
        "semantic_candidates": semantic,
        "exact_als_candidates": exact_candidates,
        "all_pattern_hits": all_hits,
        "invalid_entries": invalid[:200],
        "capture_dir": capture_dir,
        "files": {
            "syscfg_bin": "AppleDiagnosticDataSysCfg.bin",
            "btnc_blob_dir": "btnc_blobs",
        },
        "blob_manifest": blob_manifest,
        "calls": calls,
        "errors": errors,
        "summary": {
            "valid_btnc_entries": len(entries),
            "invalid_btnc_records": len(invalid),
            "semantic_candidates": len(semantic),
            "exact_als_candidates": len(exact_candidates),
            "pattern_hits": len(all_hits),
        },
        "conclusion": (
            "V26 decodes BTNC as a directory entry and dereferences each valid "
            "child blob. Exact ALS claims require a strong pattern hit; isolated "
            "0x3E prefix hits remain weak signals only."
        ),
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(capture_dir, "btnc_entries.json"), "w", encoding="utf-8") as fh:
        json.dump(decoded, fh, ensure_ascii=False, indent=2)

    return result

@app.route('/api/v26-syscfg-btnc-directory/<udid>', methods=['GET'])
def api_v26_syscfg_btnc_directory(udid):
    try:
        result = _run_async_isolated(_v26_syscfg_btnc_collect(udid), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "syscfg-btnc-directory-v26",
            "udid": udid, "error": f"{type(exc).__name__}: {exc}",
        }), 200


# ─── V27 SYSCFG ALS TRANSFORM / STRUCTURAL SCAN ──────────────────────────────
# V26 prokazal, ze hledany ALS identifikator neni ulozen jako prosty ASCII,
# compact ASCII, 5B binary ani samostatne u32 cislo. V27 proto testuje bezne
# reverzibilni 5B reprezentace cele hodnoty 3E-85DF2320 uvnitr KAZDEHO
# dereferencovaneho BTNC blobu. READ-ONLY; nic do telefonu nezapisuje.
#
# Dulezite: za exact match povazujeme jen shodu CELE transformovane 5B hodnoty.
# Samotne 0x3E / prefixy se zde vubec neskoruji, aby nevznikaly false positives.

def _v27_bit_reverse_byte(value):
    return int(f"{value:08b}"[::-1], 2)

def _v27_rol8(value, shift):
    shift %= 8
    return ((value << shift) | (value >> (8 - shift))) & 0xff if shift else value

def _v27_ror8(value, shift):
    shift %= 8
    return ((value >> shift) | (value << (8 - shift))) & 0xff if shift else value

def _v27_expected_transforms():
    import itertools
    compact = re.sub(r"[^A-Fa-f0-9]", "", _V24_ALS_EXPECTED)
    raw = bytes.fromhex(compact)
    forms = {}

    def add(name, value, meta=None):
        value = bytes(value)
        if len(value) != len(raw):
            return
        # Dedup podle bytes; ponech prvni/jednodussi popis transformace.
        if value not in {item["bytes"] for item in forms.values()}:
            forms[name] = {"bytes": value, "meta": meta or {}}

    add("identity", raw)
    add("reverse_all", raw[::-1])
    add("reverse_tail_u32", raw[:1] + raw[1:][::-1])
    add("reverse_head4_keep_last", raw[:4][::-1] + raw[4:])
    add("nibble_swap", bytes(((b >> 4) | ((b & 0x0f) << 4)) for b in raw))
    add("bit_reverse_each", bytes(_v27_bit_reverse_byte(b) for b in raw))
    add("invert", bytes((b ^ 0xff) for b in raw))

    for shift in range(1, 8):
        add(f"rol{shift}_each", bytes(_v27_rol8(b, shift) for b in raw), {"shift": shift})
        add(f"ror{shift}_each", bytes(_v27_ror8(b, shift) for b in raw), {"shift": shift})

    # Jednobajtovy XOR je bezna jednoducha maska. Testujeme celou 5B hodnotu;
    # shoda tedy neni zalozena na nahodnem prefixu.
    for key in range(1, 256):
        add(f"xor_0x{key:02X}", bytes((b ^ key) for b in raw), {"xor_key": key})

    # Pokud je identifikator ulozen po polich v jinem poradi, 5B ma jen 120
    # permutaci. Vystup nese presnou permutaci pro pozdejsi potvrzeni na dalsim
    # telefonu se znamou ALS hodnotou.
    for perm in itertools.permutations(range(len(raw))):
        add("perm_" + "".join(str(i) for i in perm), bytes(raw[i] for i in perm),
            {"permutation": list(perm)})

    return forms

def _v27_ascii_runs(blob, min_len=6):
    out = []
    for match in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, blob):
        out.append({"offset": match.start(),
                    "value": match.group(0).decode("ascii", errors="replace")[:256]})
    return out

def _v27_context(blob, offset, needle_len=5, radius=24):
    start = max(0, offset - radius)
    end = min(len(blob), offset + needle_len + radius)
    raw = blob[start:end]
    return {"start": start, "end": end, "hex": raw.hex(),
            "ascii": raw.decode("ascii", errors="replace").replace("\x00", "\\0")}

async def _v27_syscfg_als_transform_collect(udid):
    import inspect
    ld, diag = await _open_diag(udid)
    errors, calls = {}, []
    syscfg = None
    source = None
    try:
        for target in _V23_TARGETS:
            try:
                obj = diag.ioregistry(name=target)
                if inspect.isawaitable(obj):
                    obj = await obj
                calls.append({"source": f"ioregistry:name:{target}", "ok": bool(obj)})
                if isinstance(obj, dict):
                    for path, key, value in _v20_walk(obj):
                        if (str(key).lower() == "applediagnosticdatasyscfg" and
                                isinstance(value, (bytes, bytearray, memoryview))):
                            syscfg = bytes(value)
                            source = f"ioregistry:name:{target}::{path}"
                            break
                if syscfg is not None:
                    break
            except Exception as exc:
                errors[f"ioregistry:name:{target}"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass

    if syscfg is None:
        return {"ok": False, "probe": "syscfg-als-transform-scan-v27", "udid": udid,
                "error": "AppleDiagnosticDataSysCfg nebyl nalezen.",
                "calls": calls, "errors": errors}

    entries, invalid = _v26_parse_btnc_directory(syscfg)
    transforms = _v27_expected_transforms()
    hits = []
    blob_summaries = []
    known_vibrator = _DISCOVERY_EXPECTED["vibrator_number"].encode("ascii")

    for entry in entries:
        blob = syscfg[entry["target_offset"]:entry["target_end"]]
        blob_hits = []
        for transform_name, spec in transforms.items():
            needle = spec["bytes"]
            for rel in _v26_find_all(blob, needle):
                item = {
                    "child_tag": entry["child_tag"],
                    "record_offset": entry["record_offset"],
                    "relative_offset": rel,
                    "absolute_offset": entry["target_offset"] + rel,
                    "transform": transform_name,
                    "transform_meta": spec["meta"],
                    "matched_hex": needle.hex(),
                    "context": _v27_context(blob, rel, len(needle)),
                }
                hits.append(item)
                blob_hits.append(item)

        vib_positions = _v26_find_all(blob, known_vibrator)
        tag_upper = entry["child_tag"].upper()
        semantic = any(token in tag_upper for token in
                       ("ALS", "LIG", "LUX", "AMB", "SNS", "SEN", "CAL", "LC"))
        if blob_hits or vib_positions or semantic:
            blob_summaries.append({
                "child_tag": entry["child_tag"],
                "record_offset": entry["record_offset"],
                "target_offset": entry["target_offset"],
                "size": entry["size"],
                "semantic_tag": semantic,
                "known_vibrator_offsets": vib_positions,
                "transform_hit_count": len(blob_hits),
                "ascii_runs": _v27_ascii_runs(blob)[:100],
                "preview": _v26_blob_preview(blob, limit=256),
            })

    # Transformace, ktere maji prilis mnoho hitu, jsou slaby signal. Unikatni
    # shoda v jednom pointed blobu je nejzajimavejsi kandidat pro dalsi validaci.
    transform_counts = {}
    for item in hits:
        transform_counts[item["transform"]] = transform_counts.get(item["transform"], 0) + 1
    for item in hits:
        item["global_transform_hit_count"] = transform_counts[item["transform"]]
        item["unique_transform_hit"] = transform_counts[item["transform"]] == 1

    hits.sort(key=lambda x: (not x["unique_transform_hit"],
                             x["global_transform_hit_count"], x["child_tag"],
                             x["relative_offset"]))

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v27", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)
    with open(os.path.join(capture_dir, "AppleDiagnosticDataSysCfg.bin"), "wb") as fh:
        fh.write(syscfg)

    result = {
        "ok": True,
        "probe": "syscfg-als-transform-scan-v27",
        "read_only": True,
        "udid": udid,
        "source": source,
        "expected_als": _V24_ALS_EXPECTED,
        "expected_binary_hex": re.sub(r"[^A-Fa-f0-9]", "", _V24_ALS_EXPECTED).lower(),
        "transforms_tested": len(transforms),
        "valid_btnc_entries": len(entries),
        "invalid_btnc_records": len(invalid),
        "transform_hits": hits[:2000],
        "blob_summaries": blob_summaries,
        "transform_counts": transform_counts,
        "capture_dir": capture_dir,
        "files": {"syscfg_bin": "AppleDiagnosticDataSysCfg.bin"},
        "calls": calls,
        "errors": errors,
        "summary": {
            "transforms_tested": len(transforms),
            "transform_hits": len(hits),
            "unique_transform_hits": sum(1 for x in hits if x["unique_transform_hit"]),
            "interesting_blobs": len(blob_summaries),
            "known_vibrator_blob_count": sum(1 for x in blob_summaries if x["known_vibrator_offsets"]),
        },
        "conclusion": (
            "V27 tests full 5-byte reversible representations of the known ALS ID "
            "inside dereferenced BTNC blobs. A unique full-value transform hit is a "
            "candidate only; final ALS attribution still requires validation on a "
            "second device with a different known ALS ID."
        ),
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return result

@app.route('/api/v27-syscfg-als-transform-scan/<udid>', methods=['GET'])
def api_v27_syscfg_als_transform_scan(udid):
    try:
        result = _run_async_isolated(_v27_syscfg_als_transform_collect(udid), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "probe": "syscfg-als-transform-scan-v27",
                        "udid": udid, "error": f"{type(exc).__name__}: {exc}"}), 200


# ─── HARDWARE REPORT (agregace do sémantických sekcí) ────────────────────────
# Sjednocuje uz existujici cteni do jedne odpovedi rozdelene podle VYZNAMU:
# identita zarizeni / komponenty (serialy) / baterie (diagnostika) /
# konektivita (ADRESY, ne serialy) / displej / uloziste.
# Cte JEN to, co telefon realne vyda; chybejici pole ma available:false.
def _hw_field(label, value, source=None):
    scalar = _component_serial_scalar(value) if value is not None else None
    out = {"label": label, "value": scalar, "available": bool(scalar)}
    if source:
        out["source"] = source
    return out

def _modem_firmware_fields(values):
    """Modem / baseband firmware z lockdown all_values (root doména).
    BasebandVersion = verze firmwaru modemu. 'modem_ok' je ODVOZENÝ proxy
    (modem přítomen + firmware čitelný) – NENÍ to důkaz živého datového
    provozu; ten lockdown nevystavuje. Read-only, žádná hodnota natvrdo."""
    def g(*keys):
        return _component_serial_find(values, keys)
    bb_version = g("BasebandVersion")
    bb_status  = g("BasebandStatus")
    bb_chipid  = g("BasebandChipID")
    iccid      = g("IntegratedCircuitCardIdentity", "ICCID")
    modem_ok   = bool(bb_version)
    return {
        "baseband_version": _hw_field("Firmware modemu (Baseband)", bb_version, "lockdown"),
        "baseband_status":  _hw_field("Stav basebandu", bb_status, "lockdown"),
        "baseband_chipid":  _hw_field("Baseband chip ID", bb_chipid, "lockdown"),
        "sim_iccid":        _hw_field("SIM (ICCID)", iccid, "lockdown"),
        "modem_ok":         _hw_field("Modem funkční (firmware čitelný)", "Ano" if modem_ok else "Ne", "odvozeno"),
    }

async def _hardware_report_collect(udid):
    # AGREGÁTOR: precte syrove zdroje JEDNOU a znovupouzije potvrzeny component
    # reader. Nedu­plikuje IORegistry logiku - jen agreguje a sémanticky trídí.
    sources = await _read_hw_sources(udid)
    values = sources.get("values", {})
    battery = sources.get("battery", {})
    clcd = sources.get("clcd", {})
    nand = sources.get("nand", {})
    vibrator = sources.get("vibrator", {})
    errors = dict(sources.get("errors", {}))

    # komponenty z POTVRZENÉHO readeru (stejné zdroje, žádné druhé čtení)
    comp_result = await _component_serials_collect(udid, sources=sources)
    comp = comp_result.get("components", {})

    def lv(*keys):   # lockdown value
        return _component_serial_find(values, keys)
    def bv(*keys):   # battery ioreg value
        return _component_serial_find(battery, keys)
    def from_comp(key, label):
        c = comp.get(key, {})
        out = {"label": label, "value": c.get("value"), "available": bool(c.get("value"))}
        # zachovej pripadna budouci verifikacni pole (forward-kompatibilita)
        for k in ("factory_value", "current_value", "match", "status", "note"):
            if k in c:
                out[k] = c[k]
        return out

    cached = connected_devices.get(udid, {}) or {}

    # ── generace + model z ProductType (spolehlivé i při fallbacku get_device_info) ──
    _pt = lv("ProductType")
    _mm = re.match(r"iPhone(\d+),", str(_pt or ""))
    _maj = int(_mm.group(1)) if _mm else None
    _faceid_new_gen = (_maj is None) or (_maj >= 14)   # iPhone 13+ = jen IR + Dot projektor
    if _pt:
        _rm_name, _rm_a = resolve_model(_pt)
        _report_model = _rm_name if (_rm_name and _rm_name != _pt) else None
        _report_anum  = _rm_a if (_rm_a and _rm_a != "N/A") else None
    else:
        _report_model, _report_anum = None, None

    # ── IDENTITA ZAŘÍZENÍ ──
    device = {
        "serial_number": _hw_field("Sériové číslo", lv("SerialNumber"), "lockdown"),
        "imei": _hw_field("IMEI", lv("InternationalMobileEquipmentIdentity"), "lockdown"),
        "imei2": _hw_field("IMEI2", lv("InternationalMobileEquipmentIdentity2", "SecondaryMobileEquipmentIdentifier"), "lockdown"),
        "meid": _hw_field("MEID", lv("MobileEquipmentIdentifier"), "lockdown"),
        "product_type": _hw_field("ProductType", lv("ProductType"), "lockdown"),
        "model": _hw_field("Model", _report_model or cached.get("model") or lv("ProductType"), "resolved"),
        "a_number": _hw_field("Model number (A)", cached.get("a_number") or _report_anum, "resolved"),
        "ios": _hw_field("iOS", lv("ProductVersion"), "lockdown"),
        "build": _hw_field("Build", lv("BuildVersion"), "lockdown"),
    }

    # ── KAMERY (sériová čísla všech kamer vedle sebe – ať se hezky hledá) ──
    cameras = {
        "rear_camera": from_comp("rear_camera", "Zadní kamera"),
        "front_camera": from_comp("front_camera", "Přední kamera"),
        "tele_camera": from_comp("tele_camera", "Teleobjektiv"),
    }

    # ── KOMPONENTY (fyzická sériová čísla dílů) – z potvrzeného readeru ──
    components = {
        "screen": from_comp("screen", "Displej"),
        "battery": from_comp("battery", "Baterie"),
        "mainboard": from_comp("mainboard", "Základní deska"),
        "taptic_engine": from_comp("taptic_engine", "Taptic Engine"),
    }

    # ── FACE ID (do iPhone 12 vč. Distance senzoru; od iPhone 13 jen IR + Dot) ──
    face_id = {
        "true_depth_projector": from_comp("true_depth_projector", "Dot projektor (Lattice)"),
        "front_ir_camera": from_comp("front_ir_camera", "IR kamera"),
    }
    if not _faceid_new_gen:
        face_id["distance_sensor"] = from_comp("distance_sensor", "Distance senzor")

    # ── BATERIE (diagnostika – jen to, co ioreg realne vyda) ──
    def _int_or_none(v):
        try:
            return int(v)
        except Exception:
            return None
    design = _int_or_none(bv("DesignCapacity"))
    nominal = _int_or_none(bv("NominalChargeCapacity") or bv("AppleRawMaxCapacity"))
    mcp = _int_or_none(bv("MaximumCapacityPercent"))
    health = mcp
    if health is None and design and nominal and design > 0:
        health = min(100, round(nominal / design * 100))
    battery_diag = {
        "serial": _hw_field("Sériové číslo", bv("Serial", "SerialNumber", "BatterySerialNumber")),
        "health_percent": _hw_field("Kondice (%)", str(health) if health is not None else None),
        "cycle_count": _hw_field("Počet cyklů", bv("CycleCount", "AppleRawCycleCount")),
        "design_capacity": _hw_field("Návrhová kapacita (mAh)", str(design) if design else None),
        "nominal_capacity": _hw_field("Aktuální kapacita (mAh)", str(nominal) if nominal else None),
        "is_charging": _hw_field("Nabíjí se", bv("IsCharging")),
        "fully_charged": _hw_field("Plně nabito", bv("FullyCharged")),
        "external_connected": _hw_field("Napájení připojeno", bv("ExternalConnected")),
        "temperature_raw": _hw_field("Teplota (raw)", bv("Temperature")),
        "voltage_raw": _hw_field("Napětí (raw)", bv("Voltage")),
    }

    # ── KONEKTIVITA / BASEBAND (ADRESY a identifikatory, NE serialy dilu) ──
    connectivity = {
        "bluetooth_address": _hw_field("Bluetooth adresa (MAC)", lv("BluetoothAddress"), "lockdown"),
        "ethernet_address": _hw_field("Ethernet adresa (MAC)", lv("EthernetAddress"), "lockdown"),
        "wifi_address": _hw_field("Wi-Fi adresa (MAC)", lv("WiFiAddress", "WifiAddress"), "lockdown"),
        "imei": _hw_field("IMEI", lv("InternationalMobileEquipmentIdentity"), "lockdown"),
        "imei2": _hw_field("IMEI2", lv("InternationalMobileEquipmentIdentity2", "SecondaryMobileEquipmentIdentifier"), "lockdown"),
        "meid": _hw_field("MEID", lv("MobileEquipmentIdentifier"), "lockdown"),
    }

    # ── DISPLEJ ──
    panel_id_full = _component_serial_find(clcd, ("Panel_ID", "PanelID"))
    display = {
        "serial": from_comp("screen", "Sériové číslo displeje"),
        "panel_id": _hw_field("Panel ID", panel_id_full, "AppleCLCD"),
    }
    # Na iPhone 13+ je prox slot "front flex proximity" (JasperSNUM) – klíčové
    # kvůli párování displeje. Přidáme jako samostatný řádek k displeji.
    # (Na ≤12 je prox slot Distance senzor a ten je pod Face ID.)
    if _faceid_new_gen:
        display["proximity_flex"] = from_comp("distance_sensor", "Proximity / display flex")

    # ── ÚLOŽIŠTĚ / HARDWARE ──
    storage = {
        "mainboard_serial": _hw_field("Sériové číslo desky (MLB)", lv("MLBSerialNumber", "LogicBoardSerialNumber"), "lockdown"),
    }

    # ── MOBILNÍ DATA (kartičky – klíčové indikátory aktivace/datové sítě) ──
    mobile_data = {
        "modem_firmware": _hw_field("Modem firmware (Baseband)", lv("BasebandVersion"), "lockdown"),
        "imei": _hw_field("IMEI", lv("InternationalMobileEquipmentIdentity"), "lockdown"),
    }

    sections = {
        "device": device, "cameras": cameras, "face_id": face_id, "mobile_data": mobile_data,
        "components": components, "battery": battery_diag,
        "connectivity": connectivity, "display": display, "storage": storage,
    }
    total = sum(len(s) for s in sections.values())
    available = sum(1 for s in sections.values() for f in s.values() if f.get("available"))

    return {
        "ok": True, "udid": udid,
        "sections": sections,
        "summary": {"fields_total": total, "fields_available": available},
        "errors": errors,
    }

@app.route('/api/hardware-report/<udid>', methods=['GET'])
def api_hardware_report(udid):
    try:
        result = _run_async_isolated(_hardware_report_collect(udid), timeout=120)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({'ok': False, 'udid': udid, 'error': f'{type(exc).__name__}: {exc}'}), 200

@app.route('/api/detect-printer')
def api_detect_printer():
    """Detekuje připojenou tiskárnu přes WMI (Windows) nebo lpstat (Linux/Mac)."""
    import subprocess, platform

    printer_name = None
    tape_size    = None

    # Brother QL model → tape size mm
    TAPE_MAP = {
        'QL-500': '54x29', 'QL-550': '54x29',
        'QL-570': '62',    'QL-580': '62',    'QL-600': '62',
        'QL-700': '29',    'QL-710': '29',    'QL-720': '29',
        'QL-800': '62',    'QL-810': '62',    'QL-820': '62',
        'QL-1100': '102',  'QL-1110': '102',  'QL-1115': '102',
    }

    try:
        system = platform.system()
        if system == 'Windows':
            # WMI přes wmic
            result = subprocess.run(
                ['wmic', 'printer', 'get', 'Name', '/format:csv'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if 'Brother' in line or 'QL' in line:
                    parts = line.strip().split(',')
                    name = parts[-1].strip() if parts else ''
                    if name:
                        printer_name = name
                        break
        elif system in ('Darwin', 'Linux'):
            result = subprocess.run(
                ['lpstat', '-p'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if 'Brother' in line or 'QL' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        printer_name = parts[1]
                        break
    except Exception as e:
        print(f"  Printer detect error: {e}")

    # Zjisti tape size z názvu tiskárny
    if printer_name:
        for model, tape in TAPE_MAP.items():
            if model in (printer_name or ''):
                tape_size = tape
                break

    return jsonify({
        'ok':      True,
        'printer': printer_name,
        'tape':    tape_size,
        'system':  platform.system(),
    })

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(_resource_dir(filename), filename)

# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    u = (data.get('username') or '').strip()
    p = (data.get('password') or '')
    if not u or not p:
        return jsonify({'ok': False, 'error': 'Vyplňte jméno a heslo.'}), 400
    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username=? AND password_hash=? AND active=1',
        (u, hash_pw(p))
    ).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': 'Nesprávné jméno nebo heslo.'}), 401
    if datetime.date.fromisoformat(user['license_valid_until']) < datetime.date.today():
        conn.close()
        return jsonify({'ok': False, 'error': 'Platnost licence vypršela.'}), 403
    conn.execute('UPDATE users SET last_login=? WHERE id=?',
                 (datetime.datetime.now().isoformat(timespec='seconds'), user['id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'user': {
        'id': user['id'], 'username': user['username'],
        'full_name': user['full_name'], 'company': user['company'],
        'role': user['role'], 'license_type': user['license_type'],
        'license_valid_until': user['license_valid_until'],
    }})

# ─── SSE ─────────────────────────────────────────────────────────────────────

@app.route('/api/usb-events')
def usb_events():
    def generate():
        # Aktuálně připojená
        for uid, info in list(connected_devices.items()):
            yield f"data: {json.dumps({'event': 'connected', 'udid': uid, 'info': info})}\n\n"
        # Stream
        while True:
            try:
                evt = usb_event_queue.get(timeout=25)
                yield f"data: {json.dumps(evt)}\n\n"
            except queue.Empty:
                yield 'data: {"event":"ping"}\n\n'
    return app.response_class(generate(), mimetype='text/event-stream',
                               headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/devices')
def api_devices():
    devs = []
    for uid, info in connected_devices.items():
        d = dict(info) if isinstance(info, dict) else {'info': info}
        d.setdefault('udid', uid)   # zaruci, ze UDID je videt (napr. pro full-scan/differential)
        devs.append(d)
    return jsonify({'ok': True, 'count': len(devs), 'devices': devs})

@app.route('/api/device-vals/<udid>')
def api_device_vals(udid):
    """Vypíše hodnoty ze všech domén zařízení pro debugging."""
    async def _fetch():
        from pymobiledevice3.lockdown import create_using_usbmux
        import inspect

        sig = inspect.signature(create_using_usbmux)
        params = list(sig.parameters.keys())
        if inspect.iscoroutinefunction(create_using_usbmux):
            ld = await create_using_usbmux(serial=udid) if 'serial' in params else await create_using_usbmux(udid)
        else:
            ld = create_using_usbmux(serial=udid) if 'serial' in params else create_using_usbmux(udid)

        result = {}

        # Dotaz na specifické domény kde jsou kapacita a baterie
        domains = [
            None,  # výchozí doména
            'com.apple.disk_usage',
            'com.apple.disk_usage.factory',
            'com.apple.mobile.battery',
            'com.apple.mobile.iTunes',
            'com.apple.mobile.iTunes.store',
            'com.apple.mobile.data_sync',
            'com.apple.xcode.developerdomain',
            'com.apple.mobile.internal',
            'com.apple.fmip',
            'com.apple.mobile.dolgen',
            'com.apple.mobile.software_behavior',
            'com.apple.mobile.chaperone',
        ]

        for domain in domains:
            try:
                if domain:
                    vals = ld.get_value(domain=domain)
                else:
                    vals = ld.all_values
                if asyncio.iscoroutine(vals):
                    vals = await vals
                if isinstance(vals, dict):
                    key = domain or 'default'
                    result[key] = {k: str(v) for k, v in vals.items()}
            except Exception as e:
                result[domain or 'default'] = {'error': str(e)}

        # IOKit ioregentry AppleSmartBattery – syrovy vypis (Serial, Manufacturer,
        # DeviceName atd. – hledame cokoli, co by naznacovalo puvod/originalitu baterie)
        try:
            import inspect as _insp
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            diag = DiagnosticsService(ld)
            if _insp.iscoroutinefunction(DiagnosticsService):
                diag = await DiagnosticsService(ld)
            for entry_name in ('AppleSmartBattery', 'AppleARMPMUCharger', 'AppleSMC'):
                try:
                    ioreg = diag.ioregentry(entry_name)
                    if asyncio.iscoroutine(ioreg):
                        ioreg = await ioreg
                    if isinstance(ioreg, dict):
                        result['ioreg_' + entry_name] = {k: str(v) for k, v in ioreg.items()}
                except Exception as e2:
                    result['ioreg_' + entry_name + '_error'] = str(e2)
        except Exception as e:
            result['ioreg_error'] = str(e)

        return result

    loop = asyncio.new_event_loop()
    try:
        with _usbmux_lock:
            result = loop.run_until_complete(_fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        loop.close()


@app.route('/api/debug')
def api_debug():
    info = {'connected': list(connected_devices.keys())}
    try:
        import pymobiledevice3
        info['version'] = getattr(pymobiledevice3, '__version__', '?')
        from pymobiledevice3.lockdown import create_using_usbmux
        import inspect
        info['create_using_usbmux_async'] = inspect.iscoroutinefunction(create_using_usbmux)
        info['create_using_usbmux_params'] = list(inspect.signature(create_using_usbmux).parameters.keys())
        from pymobiledevice3.usbmux import select_devices_by_connection_type
        info['select_devices_async'] = inspect.iscoroutinefunction(select_devices_by_connection_type)
    except Exception as e:
        info['error'] = str(e)
    return jsonify(info)

# ─── USERS API ───────────────────────────────────────────────────────────────

def require_admin(req):
    u = req.headers.get('X-Username', '')
    p = req.headers.get('X-Password', '')
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username=? AND password_hash=? AND role='admin' AND active=1",
        (u, hash_pw(p))
    ).fetchone()
    conn.close()
    return user

@app.route('/api/users', methods=['GET'])
def api_users_list():
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    conn = get_db()
    rows = conn.execute('SELECT id,username,full_name,email,company,role,license_type,license_valid_until,active,created_at,last_login,notes FROM users ORDER BY id').fetchall()
    conn.close()
    return jsonify({'ok': True, 'users': [dict(r) for r in rows]})

@app.route('/api/users', methods=['POST'])
def api_users_create():
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    data = request.get_json()
    for f in ['username', 'password', 'full_name', 'email']:
        if not data.get(f):
            return jsonify({'ok': False, 'error': f'Pole {f} je povinné.'}), 400
    now = datetime.datetime.now().isoformat(timespec='seconds')
    conn = get_db()
    try:
        conn.execute('INSERT INTO users (username,password_hash,full_name,email,company,role,license_type,license_valid_until,active,created_at,notes) VALUES (?,?,?,?,?,?,?,?,1,?,?)',
                     (data['username'], hash_pw(data['password']), data['full_name'], data['email'],
                      data.get('company', ''), data.get('role', 'technician'), data.get('license_type', 'pro'),
                      data.get('license_valid_until', '2026-12-31'), now, data.get('notes', '')))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Uživatelské jméno již existuje.'}), 409
    uid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'id': uid}), 201

@app.route('/api/users/<int:uid>', methods=['PUT'])
def api_users_update(uid):
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    data = request.get_json()
    conn = get_db()
    fields, values = [], []
    for f in ['full_name', 'email', 'company', 'role', 'license_type', 'license_valid_until', 'notes']:
        if f in data:
            fields.append(f'{f}=?')
            values.append(data[f])
    if 'active' in data:
        fields.append('active=?')
        values.append(int(data['active']))
    if data.get('password'):
        fields.append('password_hash=?')
        values.append(hash_pw(data['password']))
    if fields:
        values.append(uid)
        conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
def api_users_delete(uid):
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id=?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ─── RESET PASSWORD API ─────────────────────────────────────────────────────

@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data        = request.get_json() or {}
    license_key = (data.get('license_key') or '').strip().upper()
    username    = (data.get('username') or '').strip()
    new_password= (data.get('new_password') or '')

    if not license_key or not username or not new_password:
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400
    if not license_key.startswith('ISUP-'):
        return jsonify({'ok': False, 'error': 'Invalid licence key format'}), 400
    if len(new_password) < 4:
        return jsonify({'ok': False, 'error': 'Password too short'}), 400

    # Ověř klíč na Railway API
    import urllib.request, json as json_lib, socket
    try:
        hwid     = get_hwid()
        hostname = socket.gethostname()
        payload  = json_lib.dumps({
            'license_key': license_key,
            'hwid':        hwid,
            'hostname':    hostname,
        }).encode()
        req = urllib.request.Request(
            f"{LICENSE_API}/api/validate",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json_lib.loads(resp.read())
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Cannot reach licence server: {e}'}), 503

    if not result.get('ok'):
        return jsonify({'ok': False, 'error': 'Invalid or expired licence key'}), 403

    # Zkontroluj jestli uživatel existuje
    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'ok': False, 'error': f'Username "{username}" not found on this device'}), 404

    # Resetuj heslo
    conn.execute(
        'UPDATE users SET password_hash=?, last_login=? WHERE username=?',
        (hash_pw(new_password), datetime.datetime.now().isoformat(timespec='seconds'), username)
    )
    conn.commit()
    conn.close()
    print(f"  ✓ Heslo resetováno pro: {username}")
    return jsonify({'ok': True, 'username': username})


# ─── ACTIVATION API ──────────────────────────────────────────────────────────

@app.route('/api/licence-status', methods=['GET'])
def api_licence_status():
    """Vrátí jestli je licence už aktivována na tomto PC."""
    key = load_license_key()
    return jsonify({
        'activated': key is not None,
        'key_preview': key[:12] + '...' if key else None
    })


@app.route('/api/activate', methods=['POST'])
def api_activate():
    data        = request.get_json() or {}
    license_key = (data.get('license_key') or '').strip().upper()
    username    = (data.get('username') or '').strip()
    password    = (data.get('password') or '')

    if not license_key or not username or not password:
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400
    if not license_key.startswith('ISUP-'):
        return jsonify({'ok': False, 'error': 'Invalid licence key format'}), 400

    # Overeni klice na Railway API
    import urllib.request, json as json_lib, socket
    try:
        hwid     = get_hwid()
        hostname = socket.gethostname()
        payload  = json_lib.dumps({
            'license_key': license_key,
            'hwid':        hwid,
            'hostname':    hostname,
        }).encode()
        req = urllib.request.Request(
            f"{LICENSE_API}/api/validate",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json_lib.loads(resp.read())
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Cannot reach licence server: {e}'}), 503

    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Invalid licence key')}), 403

    # Uloz licencni klic
    try:
        with open(LICENSE_FILE, 'w') as lf:
            lf.write(license_key)
        if result.get('token'):
            save_token_cache(result['token'])
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Cannot save licence: {e}'}), 500

    # Vytvor nebo aktualizuj lokalniho uzivatele
    conn = get_db()
    try:
        existing = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        now = datetime.datetime.now().isoformat(timespec='seconds')
        plan = result.get('plan', 'pro')
        valid_until = result.get('valid_until', '2099-12-31')
        if existing:
            conn.execute(
                'UPDATE users SET password_hash=?, license_type=?, license_valid_until=?, last_login=? WHERE username=?',
                (hash_pw(password), plan, valid_until, now, username)
            )
        else:
            conn.execute(
                'INSERT INTO users (username, password_hash, full_name, email, company, role, license_type, license_valid_until, active, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)',
                (username, hash_pw(password), username, result.get('email',''), result.get('company',''), 'technician', plan, valid_until, now)
            )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'ok': False, 'error': f'DB error: {e}'}), 500
    conn.close()

    return jsonify({'ok': True, 'username': username, 'plan': plan, 'valid_until': valid_until, 'company': result.get('company','')})

# ─── SCANS ───────────────────────────────────────────────────────────────────

@app.route('/api/scans', methods=['POST'])
def api_save_scan():
    data = request.get_json()
    conn = get_db()
    conn.execute('''INSERT INTO scan_results
        (imei,serial,model,storage,color,ios_version,battery_pct,grade,result,technician,tests_json,scanned_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (data.get('imei'), data.get('serial'), data.get('model'), data.get('storage'),
         data.get('color'), data.get('ios'), data.get('battery'), data.get('grade'),
         data.get('result'), data.get('technician'),
         json.dumps(data.get('tests', {})),
         datetime.datetime.now().isoformat(timespec='seconds')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/scans', methods=['GET'])
def api_get_scans():
    conn = get_db()
    rows = conn.execute('SELECT * FROM scan_results ORDER BY scanned_at DESC LIMIT 500').fetchall()
    conn.close()
    return jsonify({'ok': True, 'scans': [dict(r) for r in rows]})


# ─── E-SHOP INTEGRACE – napojení scanu na libovolný e-shop přes API ─────
# Konfigurace se ukládá do lokální DB (tabulka settings), nastavuje se v admin UI.
def _ensure_settings():
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    _ensure_settings()
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    _ensure_settings()
    conn = get_db()
    conn.execute('INSERT INTO settings (key,value) VALUES (?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    conn.commit()
    conn.close()

def eshop_target():
    if get_setting('eshop_enabled', '0') != '1':
        return None, None
    url = (get_setting('eshop_url') or '').strip()
    key = (get_setting('eshop_key') or '').strip()
    return (url or None), (key or None)

def _post_to_eshop(url, key, body, timeout=8):
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json', 'x-scan-key': key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode('utf-8')

@app.route('/api/eshop-config', methods=['GET'])
def api_eshop_config_get():
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    return jsonify({'ok': True, 'url': get_setting('eshop_url', ''),
                    'key': get_setting('eshop_key', ''),
                    'enabled': get_setting('eshop_enabled', '0') == '1'})

@app.route('/api/eshop-config', methods=['POST'])
def api_eshop_config_set():
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    data = request.get_json() or {}
    set_setting('eshop_url', (data.get('url') or '').strip())
    set_setting('eshop_key', (data.get('key') or '').strip())
    set_setting('eshop_enabled', '1' if data.get('enabled') else '0')
    return jsonify({'ok': True})

@app.route('/api/eshop-test', methods=['POST'])
def api_eshop_test():
    if not require_admin(request):
        return jsonify({'ok': False, 'error': 'Přístup odepřen.'}), 403
    url = (get_setting('eshop_url') or '').strip()
    key = (get_setting('eshop_key') or '').strip()
    if not url or not key:
        return jsonify({'ok': False, 'error': 'Vyplňte URL i klíč.'}), 400
    try:
        code, body = _post_to_eshop(url, key, {'test': True, 'source': 'isupply-scan',
            'model': 'iPhone TEST', 'capacity': '128 GB', 'color': 'Test', 'condition': 'A'})
        return jsonify({'ok': True, 'status': code, 'response': body[:300]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502

@app.route('/api/eshop-push', methods=['POST'])
def api_eshop_push():
    url, key = eshop_target()
    if not url or not key:
        return jsonify({'ok': False, 'skipped': True}), 200
    d = request.get_json() or {}
    body = {
        'source':    'isupply-scan',
        'imei':      d.get('imei'),       # jen pro deduplikaci na straně e-shopu
        'serial':    d.get('serial'),     # jen pro deduplikaci
        'model':     d.get('model'),
        'capacity':  d.get('storage'),
        'color':     d.get('color'),
        'condition': d.get('condition'),  # stav zadaný technikem: A/B/C/zánovní/nový
    }
    try:
        code, resp = _post_to_eshop(url, key, body)
        try: parsed = json.loads(resp)
        except Exception: parsed = {'raw': resp[:300]}
        return jsonify({'ok': True, 'status': code, 'eshop': parsed})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 502


# ─── SOFTWARE TOOL – iOS RESTORE (IPSW) ─────────────────────────────────────
#
# Backend pro záložku "Software Tool" v iphone-diagnostic.html.
# Používá idevicerestore (libimobiledevice) ke stažení a nahrání IPSW.
# idevicerestore.exe musí být v tools/idevicerestore.exe vedle .exe/server.py
# (Windows binárku lze získat přes MSYS2 balíček mingw-w64-x86_64-idevicerestore,
# oficiální postup je popsán v README projektu libimobiledevice/idevicerestore).

import urllib.request
import re as _re_restore

IPSW_CACHE_DIR      = os.path.join(BASE_DIR, 'ipsw_cache')
IDEVICERESTORE_BIN  = os.path.join(BASE_DIR, 'tools', 'idevicerestore.exe')

restore_log_queue = queue.Queue()
restore_state = {'running': False, 'process': None}

# Known idevicerestore / iTunes restore error codes -> pravděpodobná příčina.
# Toto je heuristika podobná té, kterou používá 3uTools (nejde o jistotu,
# jde o nejpravděpodobnější vysvětlení na základě známých vzorců chyb
# rozšířených v repair komunitě – GSM fórum, iFixit, Apple support vlákna).
RESTORE_ERROR_DIAGNOSIS = {
    '9':    'USB komunikace selhala během restore. Obvykle vadný/nekvalitní USB kabel, USB port na PC, nebo poškozený lightning konektor telefonu.',
    '14':   'Chyba ověření firmware komponenty – většinou zastaralá verze idevicerestore, ne hardware.',
    '20':   'Chyba zápisu na NAND (flash paměť). Pokud se opakuje i po výměně kabelu/portu, jde pravděpodobně o vadný NAND čip nebo základní desku.',
    '21':   'Baseband/modem selhal při aktualizaci – podezření na poškozený baseband čip nebo neautorizovaný zásah na desce.',
    '23':   'Chyba komunikace během DFU – zkuste jiný kabel a USB port přímo na základní desce PC (ne hub).',
    '26':   'Chyba čtení konfigurace zařízení – může indikovat poškozenou NAND nebo logic board.',
    '28':   'Chyba power management IC / baterie – restore selhal na kroku napájení. Zkontrolujte baterii a nabíjecí obvod.',
    '34':   'Chyba zápisu na disk počítače – nedostatek místa nebo poškozený IPSW soubor.',
    '35':   'Chyba přehrání firmware image – zkuste IPSW stáhnout znovu (poškozený soubor).',
    '36':   'Chyba zápisu do flash paměti. Pokud přetrvává, pravděpodobně vadná NAND / logic board.',
    '37':   'Chyba ověření kernel image – poškozený IPSW nebo problém s pamětí zařízení.',
    '40':   'Chyba hardwarového testu při restore – často NAND nebo základní deska.',
    '56':   'Hardwarová chyba hlášená přímo zařízením (Apple hardware error) – typicky logic board.',
    '1000': 'Nedostatek místa na disku počítače pro IPSW/dočasné soubory.',
    '1013': 'Baseband update selhal – možný vadný baseband čip.',
    '1015': 'Zařízení odmítlo downgrade (SHSH okno pro tuto verzi je zavřené) – nejde o hardware.',
    '1600': 'Zařízení nekomunikuje v DFU módu – zkuste znovu uvést do DFU, jiný kabel/port.',
    '1601': 'Chyba komunikace v recovery módu – kabel, port nebo USB rozbočovač.',
    '1602': 'Timeout při čekání na odezvu zařízení – kabel, port nebo nabíjecí konektor.',
    '1603': 'Chyba přenosu dat v recovery módu.',
    '1604': 'Zařízení se odpojilo během restore – vadný kabel/port, nebo se telefon sám vypnul (baterie/power IC).',
    '1611': 'Baseband personalizace selhala.',
    '2001': 'Zařízení nerozpoznáno v DFU – kabel, port, nebo poškozený lightning konektor.',
    '2002': 'Chyba enumerace USB zařízení – zkuste jiný port/kabel.',
    '2005': 'Zařízení se nepodařilo najít po restartu do DFU – nabíjecí konektor nebo power button flex.',
    '3004': 'Baseband update selhal opakovaně – podezření na vadný baseband čip.',
    '3194': 'Apple servery už tuto verzi nepodepisují (signing window closed) – nejde o hardware.',
    '4005': 'Zařízení se odpojilo během restore. Pokud se opakuje s různými kabely/porty/PC, jde pravděpodobně o vadnou nabíjecí civku/konektor nebo základní desku.',
    '4013': 'Timeout během restore – klasická "hardwarová" chyba. Zkuste jiný kabel, jiný USB port (ideálně přímo na desce PC) a jiný počítač. Pokud chyba přetrvává napříč všemi kombinacemi, pravděpodobně jde o vadnou základní desku nebo NAND.',
    '4014': 'Podobné jako 4013 – přerušení komunikace při restore. Kabel/port/hub, při opakovaném výskytu logic board.',
    '4020': 'Baseband selhal při restore – možný vadný baseband/modem čip.',
}


def _diagnose_restore_failure(log_text, returncode):
    """Projde log z idevicerestore a zkusí najít známý error kód -> vrátí
    lidsky čitelnou diagnózu podobnou té, co ukazuje 3uTools."""
    codes_found = _re_restore.findall(r'(?:error|Error)\s*[:#]?\s*(-?\d{1,5})', log_text)
    codes_found += _re_restore.findall(r'\((-?\d{1,5})\)', log_text)

    for code in codes_found:
        clean = code.lstrip('-')
        if clean in RESTORE_ERROR_DIAGNOSIS:
            return {'code': code, 'diagnosis': RESTORE_ERROR_DIAGNOSIS[clean]}

    low = log_text.lower()
    if 'disconnect' in low or 'no device' in low:
        return {'code': None, 'diagnosis': 'Zařízení se odpojilo během procesu. Zkontrolujte kabel, USB port a nabíjecí konektor telefonu.'}
    if 'timeout' in low:
        return {'code': None, 'diagnosis': 'Vypršel časový limit komunikace. Obvykle kabel/port, ve vzácných případech logic board.'}
    if returncode not in (0, None):
        return {'code': str(returncode), 'diagnosis': 'Restore selhal s neznámou chybou. Zkuste jiný kabel/port a zopakujte, aby šlo odlišit hardwarovou příčinu od dočasného výpadku.'}
    return None


def _download_ipsw(url, dest_path, progress_cb):
    """Stahuje IPSW se sledováním postupu (5–40 %)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'iSupply-Scan/1.0'})
    with urllib.request.urlopen(req) as resp:
        total = int(resp.getheader('Content-Length') or 0)
        downloaded = 0
        chunk_size = 1024 * 256
        with open(dest_path, 'wb') as f:
            while True:
                buf = resp.read(chunk_size)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if total:
                    pct = 5 + int((downloaded / total) * 35)
                    progress_cb(min(pct, 40))


def _run_restore_job(udid, ipsw_url, version, buildid):
    restore_state['running'] = True
    full_log = []

    def emit(text, type_='info', progress=None):
        payload = {'log': text, 'type': type_}
        if progress is not None:
            payload['progress'] = progress
        full_log.append(text)
        restore_log_queue.put(payload)

    try:
        os.makedirs(IPSW_CACHE_DIR, exist_ok=True)
        ipsw_filename = f"{buildid or version or 'firmware'}.ipsw"
        ipsw_path = os.path.join(IPSW_CACHE_DIR, ipsw_filename)

        if not os.path.exists(ipsw_path):
            emit(f'Stahuji IPSW (iOS {version})...', 'info', 5)
            _download_ipsw(ipsw_url, ipsw_path, lambda p: emit(f'Stahování: {p}%', 'info', p))
            emit('IPSW úspěšně stažen.', 'ok', 40)
        else:
            emit('IPSW nalezen v cache, přeskakuji stahování.', 'info', 40)

        if not os.path.exists(IDEVICERESTORE_BIN):
            emit(f'idevicerestore.exe nenalezen v: {IDEVICERESTORE_BIN}', 'err')
            restore_log_queue.put({
                'done': True,
                'error': 'idevicerestore.exe chybí – doplňte nástroj do složky tools/ vedle aplikace.'
            })
            return

        emit(f'Spouštím restore pro zařízení {udid} (všechna data budou smazána)...', 'info', 42)

        cmd = [IDEVICERESTORE_BIN, '-u', udid, '-y', '-e', ipsw_path]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True
        )
        restore_state['process'] = proc

        progress = 45
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            low = line.lower()
            if 'extract' in low:
                progress = max(progress, 48)
            elif 'personaliz' in low or 'tss' in low:
                progress = max(progress, 55)
            elif 'dfu' in low or 'recovery' in low:
                progress = max(progress, 62)
            elif 'sending' in low or 'uploading' in low:
                progress = max(progress, 70)
            elif 'verify' in low or 'flash' in low:
                progress = max(progress, 82)
            elif 'reboot' in low or 'restart' in low:
                progress = max(progress, 95)

            is_err = 'error' in low or 'fail' in low
            emit(line, 'err' if is_err else 'info', progress)

        proc.wait()
        returncode = proc.returncode

        if returncode == 0:
            emit('Restore dokončen úspěšně.', 'ok', 100)
            restore_log_queue.put({'done': True, 'progress': 100})
        else:
            full_text = '\n'.join(full_log)
            diag = _diagnose_restore_failure(full_text, returncode)
            if diag:
                emit(f"Pravděpodobná příčina: {diag['diagnosis']}", 'err')
            restore_log_queue.put({
                'done': True,
                'error': f'Restore selhal (kód {returncode}).',
                'diagnosis': diag['diagnosis'] if diag else None,
            })

    except Exception as e:
        emit(f'Výjimka: {e}', 'err')
        restore_log_queue.put({'done': True, 'error': str(e)})
    finally:
        restore_state['running'] = False
        restore_state['process'] = None


@app.route('/api/restore', methods=['POST'])
def api_restore():
    data     = request.get_json() or {}
    udid     = (data.get('udid') or '').strip()
    ipsw_url = data.get('ipsw_url')
    version  = data.get('version')
    buildid  = data.get('buildid')

    if not udid or not ipsw_url:
        return jsonify({'ok': False, 'error': 'Chybí udid nebo ipsw_url'}), 400

    if restore_state['running']:
        return jsonify({'ok': False, 'error': 'Jiný restore už běží.'}), 409

    while not restore_log_queue.empty():
        try:
            restore_log_queue.get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(target=_run_restore_job, args=(udid, ipsw_url, version, buildid), daemon=True)
    t.start()

    return jsonify({'ok': True})


@app.route('/api/restore-logs')
def api_restore_logs():
    def generate():
        while True:
            try:
                evt = restore_log_queue.get(timeout=25)
                yield f"data: {json.dumps(evt)}\n\n"
                if evt.get('done'):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return app.response_class(generate(), mimetype='text/event-stream',
                               headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ─── START ───────────────────────────────────────────────────────────────────



# ─── V34 SPU HID ALS LAST-VALUE PROBE ───────────────────────────────────────
# READ-ONLY. V33/full-plane dumps exposed the real topology:
#   als  -> AppleSPUHIDInterface
#   prox -> AppleSPUHIDInterface -> AppleSphinxProxHIDEventDriver
# V34 targets those exact nodes/classes and scans every returned property/blob
# for the known 3uTools ALS identifier and plausible 6-byte serial candidates.

_V34_DEFAULT_ALS_REF = "0311133F6B07"
_V34_TARGETS = (
    "als", "prox",
    "AppleSPUHIDInterface",
    "AppleSphinxProxHIDEventDriver",
    "AppleSPUHIDDriver", "AppleSPU", "AppleSPUHIDDevice",
)

def _v34_safe(value, max_binary=262144):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "_type": "bytes",
            "length": len(raw),
            "hex": raw[:max_binary].hex().upper(),
            "ascii": raw[:max_binary].decode("ascii", errors="replace").replace("\x00", "\\0"),
            "truncated": len(raw) > max_binary,
        }
    if isinstance(value, dict):
        return {str(k): _v34_safe(v, max_binary) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v34_safe(v, max_binary) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)

def _v34_leaf_raw(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), "bytes"
    if isinstance(value, str):
        return value.encode("utf-8", errors="ignore"), "string"
    if isinstance(value, int) and value >= 0:
        n = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(n, "little"), "integer_le"
    return None, None

def _v34_walk(obj, path="$", depth=0):
    if depth > 120:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            p = f"{path}.{key}"
            yield p, str(key), value
            if isinstance(value, (dict, list, tuple)):
                yield from _v34_walk(value, p, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            p = f"{path}[{i}]"
            yield p, str(i), value
            if isinstance(value, (dict, list, tuple)):
                yield from _v34_walk(value, p, depth + 1)

def _v34_patterns(ref):
    compact = re.sub(r"[^A-Fa-f0-9]", "", str(ref or ""))
    raw6 = bytes.fromhex(compact) if len(compact) % 2 == 0 else b""
    pats = []
    def add(name, raw):
        if raw and raw not in [x[1] for x in pats]:
            pats.append((name, raw))
    add("ascii", str(ref).encode("ascii", errors="ignore"))
    add("raw6", raw6)
    add("raw6_reversed", raw6[::-1])
    if len(raw6) == 6:
        add("word16_le_swap", b"".join(raw6[i:i+2][::-1] for i in range(0, 6, 2)))
        add("word16_order_reversed", b"".join(
            [raw6[4:6], raw6[2:4], raw6[0:2]]
        ))
    add("utf16le", str(ref).encode("utf-16-le"))
    add("utf16be", str(ref).encode("utf-16-be"))
    return pats

async def _v34_spu_hid_als_collect(udid, als_ref=None):
    import inspect, datetime as _dt, plistlib, hashlib
    ld, diag = await _open_diag(udid)
    ref = (als_ref or _V34_DEFAULT_ALS_REF).strip()
    patterns = _v34_patterns(ref)

    calls, errors, sources = [], {}, []

    async def capture(label, fn):
        try:
            obj = fn()
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({
                "source": label, "ok": True,
                "result_type": type(obj).__name__,
                "truthy": bool(obj),
            })
            sources.append((label, obj))
            return obj
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})
            return None

    # Exact topology targets discovered from V33 dumps.
    for target in _V34_TARGETS:
        await capture(
            f"ioregistry:name:{target}",
            lambda target=target: diag.ioregistry(name=target)
        )

    # Cross-check full planes, but V34 will specifically rank SPU/HID/ALS paths.
    for plane in ("IOService", "IODeviceTree", "IOPower"):
        await capture(
            f"ioregistry:plane:{plane}",
            lambda plane=plane: diag.ioregistry(plane=plane)
        )

    # Raw IORegistry requests with exact node names. Some relay builds expose
    # more properties through raw request dictionaries than the helper method.
    for target in _V34_TARGETS:
        for key_name in ("EntryName", "Name", "CurrentEntry", "RegistryEntryName"):
            payload = {"Request": "IORegistry", key_name: target}
            await capture(
                f"diagnostics:raw:IORegistry:{key_name}:{target}",
                lambda payload=payload: diag._send_recv(payload)
            )

    hits, interesting, six_byte_candidates = [], [], []

    for source, obj in sources:
        for path, key, value in _v34_walk(obj):
            raw, value_type = _v34_leaf_raw(value)
            if raw is None:
                continue

            exact_here = []
            for transform, needle in patterns:
                start = 0
                while needle:
                    off = raw.find(needle, start)
                    if off < 0:
                        break
                    exact_here.append({
                        "transform": transform,
                        "offset": off,
                        "needle_hex": needle.hex().upper(),
                    })
                    start = off + 1
            if exact_here:
                hits.append({
                    "source": source, "path": path, "key": key,
                    "value_type": value_type, "raw_length": len(raw),
                    "raw_hex": raw[:65536].hex().upper(),
                    "exact_hits": exact_here,
                })

            norm = re.sub(r"[^a-z0-9]", "", f"{source} {path} {key}".lower())
            toks = [t for t in (
                "als", "ambient", "light", "spu", "hid", "sphinx",
                "prox", "sensor", "serial", "module", "report",
                "descriptor", "element", "calib", "factory", "vendor"
            ) if t in norm]
            if toks:
                interesting.append({
                    "source": source, "path": path, "key": key,
                    "matched_tokens": toks, "value_type": value_type,
                    "raw_length": len(raw),
                    "raw_hex": raw[:32768].hex().upper(),
                    "ascii": raw[:32768].decode("ascii", errors="replace").replace("\x00", "\\0"),
                })

                # ALS value shown by 3uTools is exactly six bytes rendered as hex.
                # Collect every 6-byte leaf and every 6-byte window from short,
                # ALS/SPU/HID-related binary properties for offline ranking.
                if len(raw) == 6:
                    six_byte_candidates.append({
                        "source": source, "path": path, "key": key,
                        "mode": "exact_leaf_6b", "offset": 0,
                        "hex": raw.hex().upper(),
                    })
                elif value_type == "bytes" and 7 <= len(raw) <= 512:
                    for off in range(0, len(raw) - 5):
                        chunk = raw[off:off+6]
                        six_byte_candidates.append({
                            "source": source, "path": path, "key": key,
                            "mode": "window_6b", "offset": off,
                            "hex": chunk.hex().upper(),
                        })

    # De-duplicate.
    def dedup(items, fields):
        seen, out = set(), []
        for item in items:
            ident = tuple(str(item.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident)
                out.append(item)
        return out

    hits = dedup(hits, ("source", "path", "key", "value_type"))
    interesting = dedup(interesting, ("source", "path", "key", "value_type"))
    six_byte_candidates = dedup(
        six_byte_candidates, ("source", "path", "key", "mode", "offset", "hex")
    )

    # Prefer exact 6-byte leaves and ALS-specific paths.
    def cand_score(row):
        hay = re.sub(r"[^a-z0-9]", "", (
            row["source"] + " " + row["path"] + " " + row["key"]
        ).lower())
        score = 100 if row["mode"] == "exact_leaf_6b" else 0
        for token, pts in (
            ("als", 80), ("ambient", 60), ("light", 40),
            ("spu", 30), ("hid", 25), ("serial", 25),
            ("module", 15), ("sensor", 15), ("report", 10),
        ):
            if token in hay:
                score += pts
        return score

    for row in six_byte_candidates:
        row["score"] = cand_score(row)
    six_byte_candidates.sort(key=lambda x: (-x["score"], x["source"], x["path"], x["offset"]))
    interesting.sort(key=lambda x: (-len(x["matched_tokens"]), x["source"], x["path"]))

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v34", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    source_files = []
    for idx, (label, obj) in enumerate(sources, 1):
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:150]
        base = f"{idx:03d}_{safe_label}"
        safe_obj = _v34_safe(obj)
        jpath = os.path.join(capture_dir, base + ".json")
        with open(jpath, "w", encoding="utf-8") as fh:
            json.dump(safe_obj, fh, ensure_ascii=False, indent=2)
        source_files.append({"source": label, "file": os.path.basename(jpath)})

        try:
            bpath = os.path.join(capture_dir, base + ".plist_binary.bin")
            with open(bpath, "wb") as fh:
                fh.write(plistlib.dumps(obj, fmt=plistlib.FMT_BINARY))
            source_files.append({"source": label, "file": os.path.basename(bpath)})
        except Exception:
            pass

    result = {
        "ok": True,
        "probe": "spu-hid-als-last-value-v34",
        "read_only": True,
        "udid": udid,
        "als_reference": ref,
        "topology_hypothesis": {
            "als": "als -> AppleSPUHIDInterface",
            "prox": "prox -> AppleSPUHIDInterface -> AppleSphinxProxHIDEventDriver",
        },
        "exact_hits": hits,
        "six_byte_candidates": six_byte_candidates[:5000],
        "interesting_properties": interesting[:2000],
        "calls": calls,
        "errors": errors,
        "capture_dir": capture_dir,
        "captured_sources": source_files,
        "summary": {
            "exact_hits": len(hits),
            "six_byte_candidates": len(six_byte_candidates),
            "interesting_properties": len(interesting),
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
        },
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    return result

@app.route('/api/v34-spu-hid-als-probe/<udid>', methods=['GET'])
def api_v34_spu_hid_als_probe(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V34_DEFAULT_ALS_REF).strip()
        result = _run_async_isolated(
            _v34_spu_hid_als_collect(udid, als_ref=als_ref),
            timeout=900
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "spu-hid-als-last-value-v34",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200





# ─── V35 ACTIVE HID REPORT PROBE ─────────────────────────────────────────────
# READ-ONLY discovery probe.
# Cíl: zjistit, zda diagnostics relay umí vrátit živý HID input/feature/vendor
# report pro SPU/ALS/prox větev. Probe neposílá SetReport ani žádný payload,
# který by zapisoval do HID zařízení.
_V35_HID_NAMES = (
    "AppleSPUHIDDevice",
    "AppleSPUHIDDriver",
    "AppleSPUHIDInterface",
    "AppleSphinxProxHIDEventDriver",
    "AppleProxDriver",
    "als",
    "prox",
)

_V35_EXPECTED_ALS = "0311133F6B07"

def _v35_safe(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        return {
            "__type__": "bytes",
            "length": len(b),
            "hex": b.hex().upper(),
            "ascii": b.decode("ascii", errors="replace").replace("\x00", "\\0"),
        }
    if isinstance(value, dict):
        return {str(k): _v35_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_v35_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)

def _v35_walk(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _v35_walk(child, child_path)
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            child_path = f"{path}[{idx}]"
            yield child_path, str(idx), child
            yield from _v35_walk(child, child_path)

def _v35_forms(value):
    out = []
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="ignore")
        out.append(("string", value, raw))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        out.append(("bytes", raw.hex().upper(), raw))
    elif isinstance(value, int) and value >= 0:
        n = max(1, (value.bit_length() + 7) // 8)
        for endian in ("little", "big"):
            try:
                raw = value.to_bytes(n, endian)
                out.append((f"integer_{endian}", str(value), raw))
            except Exception:
                pass
    return out

def _v35_analyse(label, obj):
    expected = bytes.fromhex(_V35_EXPECTED_ALS)
    expected_forms = {
        "raw6": expected,
        "raw6_reversed": expected[::-1],
        "ascii": _V35_EXPECTED_ALS.encode("ascii"),
        "utf16le": _V35_EXPECTED_ALS.encode("utf-16le"),
        "utf16be": _V35_EXPECTED_ALS.encode("utf-16be"),
    }
    hits, report_candidates = [], []
    tokens = ("report", "hid", "input", "feature", "vendor", "spu",
              "prox", "als", "ambient", "sensor", "event", "value", "data")
    for path, key, value in _v35_walk(obj):
        hay = (path + "." + key).lower()
        for form, rendered, raw in _v35_forms(value):
            for transform, needle in expected_forms.items():
                pos = raw.find(needle)
                if pos >= 0:
                    hits.append({
                        "source": label, "path": path, "key": key,
                        "value_form": form, "expected_transform": transform,
                        "offset": pos, "raw_hex": raw.hex().upper(),
                    })
            if any(t in hay for t in tokens):
                report_candidates.append({
                    "source": label, "path": path, "key": key,
                    "value_form": form, "value": rendered[:4000],
                    "raw_hex": raw.hex().upper()[:8000],
                    "raw_length": len(raw),
                })
    return hits, report_candidates

async def _v35_hid_report_probe_collect(udid):
    import inspect
    import datetime as _dt
    import json as _json

    ld, diag = await _open_diag(udid)
    calls, responses, exact_hits, candidates = [], {}, [], []

    async def read(label, fn):
        try:
            obj = fn()
            if inspect.isawaitable(obj):
                obj = await obj
            calls.append({
                "source": label, "ok": True,
                "truthy": bool(obj), "result_type": type(obj).__name__,
            })
            responses[label] = _v35_safe(obj)
            if obj:
                h, c = _v35_analyse(label, obj)
                exact_hits.extend(h)
                candidates.extend(c)
            return obj
        except Exception as exc:
            calls.append({
                "source": label, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None

    # Baseline: normální IORegistry API.
    for name in _V35_HID_NAMES:
        await read(
            f"ioregistry:name:{name}",
            lambda name=name: diag.ioregistry(name=name)
        )

    # Raw diagnostics IORegistry variants. V34 potvrdila, že tyto cesty existují.
    for name in _V35_HID_NAMES:
        for selector in ("EntryName", "Name", "CurrentEntry", "RegistryEntryName"):
            payload = {"Request": "IORegistry", selector: name}
            await read(
                f"diagnostics:raw:IORegistry:{selector}:{name}",
                lambda payload=payload: diag._send_recv(payload)
            )

    # Aktivní READ probe. Záměrně pouze názvy operací typu Get/Read/Copy.
    # ŽÁDNÝ SetReport / WriteReport / OutputReport.
    read_requests = (
        "HIDReport",
        "HIDInputReport",
        "HIDFeatureReport",
        "IOHIDReport",
        "IOHIDInputReport",
        "IOHIDFeatureReport",
        "GetHIDReport",
        "GetHIDInputReport",
        "GetHIDFeatureReport",
        "ReadHIDReport",
        "ReadHIDInputReport",
        "ReadHIDFeatureReport",
        "CopyHIDReport",
        "HIDEvent",
        "HIDEvents",
        "IOHIDEvent",
        "IOHIDEvents",
    )

    # Report 0 = AppleSPUHIDDevice input report podle descriptoru.
    # 0x5A = známý ChildVendorMessage prox report.
    report_ids = (0x00, 0x5A)

    for request_name in read_requests:
        # Nejdřív čistý request: některé relay implementace ignorují selektory.
        payload = {"Request": request_name}
        await read(
            f"diagnostics:active:{request_name}",
            lambda payload=payload: diag._send_recv(payload)
        )

        for name in _V35_HID_NAMES:
            for report_id in report_ids:
                # Varianty názvů parametrů jsou discovery-only. Všechny jsou read-only.
                variants = (
                    {"Request": request_name, "EntryName": name, "ReportID": report_id},
                    {"Request": request_name, "Name": name, "ReportID": report_id},
                    {"Request": request_name, "RegistryEntryName": name, "ReportID": report_id},
                    {"Request": request_name, "Device": name, "ReportID": report_id},
                    {"Request": request_name, "Service": name, "ReportID": report_id},
                    {"Request": request_name, "EntryName": name, "ReportId": report_id},
                )
                for idx, payload in enumerate(variants):
                    await read(
                        f"diagnostics:active:{request_name}:{name}:rid{report_id:02X}:v{idx}",
                        lambda payload=payload: diag._send_recv(payload)
                    )

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    # Dedup candidates/hits.
    def dedup(rows, fields):
        seen, out = set(), []
        for row in rows:
            ident = tuple(str(row.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident)
                out.append(row)
        return out

    exact_hits = dedup(
        exact_hits,
        ("source", "path", "expected_transform", "offset", "raw_hex")
    )
    candidates = dedup(
        candidates,
        ("source", "path", "key", "value_form", "raw_hex")
    )

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v35", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    with open(os.path.join(capture_dir, "responses.json"), "w", encoding="utf-8") as fh:
        _json.dump(responses, fh, ensure_ascii=False, indent=2)

    result = {
        "ok": True,
        "probe": "active-hid-report-probe-v35",
        "read_only": True,
        "udid": udid,
        "goal": "Read live HID/input/feature/vendor report data from SPU ALS/prox path",
        "expected_als": _V35_EXPECTED_ALS,
        "target_report_ids": ["0x00", "0x5A"],
        "write_requests_sent": 0,
        "exact_hits": exact_hits,
        "report_candidates": candidates[:5000],
        "calls": calls,
        "summary": {
            "calls_total": len(calls),
            "calls_ok": sum(1 for x in calls if x.get("ok")),
            "calls_truthy": sum(1 for x in calls if x.get("truthy")),
            "exact_hits": len(exact_hits),
            "report_candidates": len(candidates),
        },
        "capture_dir": capture_dir,
        "files": {"responses": "responses.json"},
        "conclusion": (
            "If an active diagnostics HID request returns live report bytes, inspect "
            "exact_hits and report_candidates. If all active request names are rejected "
            "or return only IORegistry metadata, diagnostics relay does not expose "
            "IOHIDDeviceGetReport directly and the next step is reproducing the exact "
            "private request observed from 3uTools traffic/runtime instrumentation."
        ),
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        _json.dump(result, fh, ensure_ascii=False, indent=2)

    return result

@app.route('/api/v35-hid-report-probe/<udid>', methods=['GET'])
def api_v35_hid_report_probe(udid):
    try:
        result = _run_async_isolated(
            _v35_hid_report_probe_collect(udid),
            timeout=900
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "active-hid-report-probe-v35",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200





# ─── V36 ALS SPU/AOP CONSUMER + PROTOCOL MAP ────────────────────────────────
# READ-ONLY. Cíl: přestat hádat obecné HID requesty a zmapovat přesnou větev
# spu_als / ALS / AppleSPU / AOP v IORegistry + IOReport metadatech.
_V36_ALS_DEFAULT_REF = "0311133F6B07"
_V36_NAMES = (
    "als", "spu_als", "ambient-light-sensor", "AppleSPUHIDInterface",
    "AppleSPUHIDDevice", "AppleSPUHIDDriver", "AppleSPU",
    "AppleAOP", "AppleAOPAudio", "AppleHIDALSService",
    "AppleALSDriver", "AppleAmbientLightSensor", "prox",
)
_V36_TOKENS = (
    "spu_als", "als", "ambient", "light", "sensor", "spu", "aop",
    "hid", "report", "channel", "legend", "consumer", "provider",
    "client", "userclient", "service", "driver", "calib", "serial",
    "module", "property", "message", "endpoint", "mailbox",
)

def _v36_safe(obj, max_bytes=1048576):
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raw = bytes(obj)
        return {"__type__": "bytes", "length": len(raw),
                "hex": raw[:max_bytes].hex().upper(),
                "truncated": len(raw) > max_bytes}
    if isinstance(obj, dict):
        return {str(k): _v36_safe(v, max_bytes) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_v36_safe(v, max_bytes) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return repr(obj)

def _v36_walk(obj, path="$", ancestors=()):
    if isinstance(obj, dict):
        local_name = str(obj.get("name") or obj.get("IORegistryEntryName") or "")
        anc = ancestors + ((local_name,) if local_name else ())
        for k, v in obj.items():
            p = f"{path}.{k}"
            yield p, str(k), v, anc
            if isinstance(v, (dict, list, tuple)):
                yield from _v36_walk(v, p, anc)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield p, str(i), v, ancestors
            if isinstance(v, (dict, list, tuple)):
                yield from _v36_walk(v, p, ancestors)

def _v36_raw_forms(value):
    out = []
    if isinstance(value, (bytes, bytearray, memoryview)):
        out.append(("bytes", bytes(value)))
    elif isinstance(value, str):
        out.append(("utf8", value.encode("utf-8", errors="ignore")))
        compact = re.sub(r"[^0-9A-Fa-f]", "", value)
        if compact and len(compact) % 2 == 0:
            try: out.append(("hex_string", bytes.fromhex(compact)))
            except Exception: pass
    elif isinstance(value, int) and value >= 0:
        n = max(1, (value.bit_length() + 7) // 8)
        out.append(("int_le", value.to_bytes(n, "little")))
        out.append(("int_be", value.to_bytes(n, "big")))
    return out

def _v36_ref_patterns(ref):
    compact = re.sub(r"[^0-9A-Fa-f]", "", str(ref or ""))
    raw = bytes.fromhex(compact) if compact and len(compact) % 2 == 0 else b""
    pats = []
    if raw:
        pats += [("raw", raw), ("reverse", raw[::-1])]
        if len(raw) == 6:
            pats += [
                ("pair_swap", b"".join(raw[i:i+2][::-1] for i in range(0, 6, 2))),
                ("pair_order_reverse", raw[4:6] + raw[2:4] + raw[0:2]),
            ]
    s = str(ref or "")
    pats += [("ascii", s.encode("ascii", errors="ignore")),
             ("utf16le", s.encode("utf-16le")),
             ("utf16be", s.encode("utf-16be"))]
    return [(n, p) for n, p in pats if p]

async def _v36_als_spu_aop_collect(udid, als_ref=None):
    import inspect, datetime as _dt, json as _json, plistlib

    ref = (als_ref or _V36_ALS_DEFAULT_REF).strip()
    patterns = _v36_ref_patterns(ref)
    ld, diag = await _open_diag(udid)

    calls, sources, errors = [], [], {}

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            calls.append({"source": label, "ok": True,
                          "truthy": bool(value), "result_type": type(value).__name__})
            if value:
                sources.append((label, value))
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            calls.append({"source": label, "ok": False, "error": err})
            errors[label] = err
            return None

    # 1) Přesné uzly potvrzené V34/V35 + AOP/SPU kandidáti.
    for name in _V36_NAMES:
        await capture(f"ioregistry:name:{name}",
                      lambda name=name: diag.ioregistry(name=name))

    # 2) Celé registry planes kvůli rodičům/dětem/consumerům.
    for plane in ("IOService", "IODeviceTree", "IOPower"):
        await capture(f"ioregistry:plane:{plane}",
                      lambda plane=plane: diag.ioregistry(plane=plane))

    # 3) Raw IORegistry pouze s přesnými názvy; žádné Get/Set HID hádání.
    for name in _V36_NAMES:
        for selector in ("EntryName", "Name", "CurrentEntry", "RegistryEntryName"):
            payload = {"Request": "IORegistry", selector: name}
            await capture(
                f"raw:IORegistry:{selector}:{name}",
                lambda payload=payload: diag._send_recv(payload)
            )

    # 4) Diagnostics/IOReport discovery: pouze čtecí požadavky bez konfigurace
    # kanálů a bez zápisu do zařízení.
    for request_name in ("IOReport", "IOReportLegend", "IOReportChannels",
                         "IOReportCopyChannels", "IOReportSample",
                         "Diagnostics", "All"):
        for channel in ("spu_als", "als", "SPU", "AOP"):
            payload = {"Request": request_name, "Channel": channel}
            await capture(
                f"raw:{request_name}:channel:{channel}",
                lambda payload=payload: diag._send_recv(payload)
            )

    exact_hits, token_hits, six_byte_leaves, topology = [], [], [], []

    for source, obj in sources:
        for path, key, value, ancestors in _v36_walk(obj):
            context = " ".join((source, path, key, *ancestors)).lower()
            matched = [t for t in _V36_TOKENS if t in context]

            for form, raw in _v36_raw_forms(value):
                for transform, needle in patterns:
                    start = 0
                    while needle:
                        off = raw.find(needle, start)
                        if off < 0: break
                        exact_hits.append({
                            "source": source, "path": path, "key": key,
                            "form": form, "transform": transform,
                            "offset": off, "raw_hex": raw.hex().upper(),
                            "ancestors": list(ancestors),
                        })
                        start = off + 1

                if len(raw) == 6 and matched:
                    six_byte_leaves.append({
                        "source": source, "path": path, "key": key,
                        "form": form, "hex": raw.hex().upper(),
                        "matched_tokens": matched,
                        "ancestors": list(ancestors),
                    })

                if matched:
                    token_hits.append({
                        "source": source, "path": path, "key": key,
                        "form": form, "raw_length": len(raw),
                        "raw_hex": raw[:4096].hex().upper(),
                        "printable": raw[:4096].decode("ascii", errors="replace").replace("\x00", "\\0"),
                        "matched_tokens": matched,
                        "ancestors": list(ancestors),
                    })

            # Zachyť celé dictionary uzly, jejichž identita/path míří na ALS/SPU/AOP.
            if isinstance(value, dict) and any(t in context for t in ("spu_als", "als", "ambient", "applespu", "aop")):
                topology.append({
                    "source": source, "path": path, "key": key,
                    "ancestors": list(ancestors),
                    "dict_keys": [str(x) for x in value.keys()][:300],
                    "name": _v36_safe(value.get("name")),
                    "className": _v36_safe(value.get("className")),
                    "inheritance": _v36_safe(value.get("inheritance")),
                    "regEntry": _v36_safe(value.get("regEntry")),
                })

    def dedup(rows, fields):
        seen, out = set(), []
        for row in rows:
            ident = tuple(str(row.get(f)) for f in fields)
            if ident not in seen:
                seen.add(ident); out.append(row)
        return out

    exact_hits = dedup(exact_hits, ("source", "path", "transform", "offset", "raw_hex"))
    token_hits = dedup(token_hits, ("source", "path", "key", "form", "raw_hex"))
    six_byte_leaves = dedup(six_byte_leaves, ("source", "path", "key", "form", "hex"))
    topology = dedup(topology, ("source", "path", "key", "regEntry"))

    def score(row):
        s = " ".join((row.get("source",""), row.get("path",""), row.get("key",""))).lower()
        pts = 0
        for token, weight in (("spu_als",200), ("ambient",100), ("als",80),
                              ("serial",70), ("calib",45), ("aop",40),
                              ("spu",35), ("message",30), ("endpoint",25),
                              ("report",15), ("hid",10)):
            if token in s: pts += weight
        return pts

    for row in token_hits: row["score"] = score(row)
    for row in six_byte_leaves: row["score"] = score(row)
    token_hits.sort(key=lambda x: (-x["score"], x["source"], x["path"]))
    six_byte_leaves.sort(key=lambda x: (-x["score"], x["source"], x["path"]))

    try:
        cr = diag.close()
        if inspect.isawaitable(cr):
            await cr
    except Exception:
        pass

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v36", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    source_manifest = []
    for idx, (label, obj) in enumerate(sources, 1):
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:150]
        fn = f"{idx:03d}_{safe_label}.json"
        with open(os.path.join(capture_dir, fn), "w", encoding="utf-8") as fh:
            _json.dump(_v36_safe(obj), fh, ensure_ascii=False, indent=2)
        source_manifest.append({"source": label, "file": fn})

    result = {
        "ok": True,
        "probe": "als-spu-aop-consumer-protocol-map-v36",
        "read_only": True,
        "udid": udid,
        "als_reference": ref,
        "exact_hits": exact_hits,
        "six_byte_leaves": six_byte_leaves[:5000],
        "topology": topology[:3000],
        "ranked_als_spu_aop_hits": token_hits[:10000],
        "calls": calls,
        "errors": errors,
        "summary": {
            "calls_total": len(calls),
            "calls_ok": sum(1 for c in calls if c.get("ok")),
            "calls_truthy": sum(1 for c in calls if c.get("truthy")),
            "sources_captured": len(sources),
            "exact_hits": len(exact_hits),
            "six_byte_leaves": len(six_byte_leaves),
            "topology_nodes": len(topology),
            "ranked_hits": len(token_hits),
        },
        "capture_dir": capture_dir,
        "captured_sources": source_manifest,
        "next_decision": (
            "If exact_hits > 0, promote the exact source/path/key into the production ALS reader. "
            "If exact_hits == 0, inspect topology and highest-scored spu_als/AOP message/endpoint hits; "
            "this probe intentionally does not invent private HID commands or write to the device."
        ),
    }

    safe_result = _v36_safe(result)
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        _json.dump(safe_result, fh, ensure_ascii=False, indent=2)
    return safe_result

@app.route('/api/v36-als-spu-aop-map/<udid>', methods=['GET'])
def api_v36_als_spu_aop_map(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V36_ALS_DEFAULT_REF).strip()
        result = _run_async_isolated(
            _v36_als_spu_aop_collect(udid, als_ref=als_ref), timeout=900
        )
        return jsonify(_v36_safe(result)), 200
    except Exception as exc:
        return jsonify({
            "ok": False, "probe": "als-spu-aop-consumer-protocol-map-v36",
            "udid": udid, "error": f"{type(exc).__name__}: {exc}"
        }), 200



# ─── V37 FINAL ALS EVENT-CORRELATED LAST-VALUE PROBE ─────────────────────────
# READ-ONLY. Vzorkuje potvrzenou AOP/SPU/ALS větev během řízených světelných
# fází a hledá leaf/blob, který se mění ve stejné chvíli jako "ALS reports".
# Nic nezapisuje do telefonu a neposílá HID SetReport.
_V37_TARGETS = (
    "als", "AppleSPU", "AppleSPUHIDDevice", "AppleSPUHIDDriver",
    "AppleSPUVD6287", "AppleHIDALSService", "AppleALSDriver",
    "AppleAmbientLightSensor", "AppleAOP",
)
_V37_PHASES = (
    ("BASELINE", 8.0, "Nechte senzor v běžném světle."),
    ("COVER", 10.0, "Zakryjte horní senzor / Dynamic Island."),
    ("UNCOVER", 10.0, "Odkryjte senzor."),
    ("LIGHT", 10.0, "Posviťte přímo na horní senzor."),
    ("FINAL", 8.0, "Vraťte telefon do běžného světla."),
)

def _v37_leaf_map(obj):
    out = {}
    for path, key, value, ancestors in _v36_walk(obj):
        if isinstance(value, (dict, list, tuple)):
            continue
        forms = _v36_raw_forms(value)
        if not forms:
            continue
        # prefer bytes/utf8; u intů zachovej LE i BE explicitně
        for form, raw in forms:
            out[f"{path}|{form}"] = {
                "path": path, "key": key, "form": form,
                "raw": raw, "hex": raw.hex().upper(),
                "ancestors": list(ancestors),
            }
    return out

def _v37_counter_candidates(obj):
    rows = []
    for path, key, value, ancestors in _v36_walk(obj):
        ctx = " ".join((path, key, *ancestors)).lower()
        if any(x in ctx for x in ("als reports", "ready reports", "message reports",
                                  "spi reports", "responses", "total packets",
                                  "ioreportchannels", "als: received")):
            for form, raw in _v36_raw_forms(value):
                rows.append({
                    "path": path, "key": key, "form": form,
                    "hex": raw.hex().upper(), "length": len(raw),
                    "ancestors": list(ancestors),
                })
    return rows

async def _v37_als_final_collect(udid, als_ref=None, interval=0.20):
    import inspect, datetime as _dt, json as _json, time as _time, statistics

    ref = (als_ref or _V36_ALS_DEFAULT_REF).strip()
    patterns = _v36_ref_patterns(ref)
    interval = min(2.0, max(0.08, float(interval)))

    ld, diag = await _open_diag(udid)
    calls, errors, samples = [], {}, []
    started = _time.monotonic()

    async def read_one(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            calls.append({"source": label, "ok": True, "truthy": bool(value)})
            return value
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            calls.append({"source": label, "ok": False, "error": err})
            errors[label] = err
            return None

    async def snapshot(phase, phase_index, instruction):
        captured = {}
        # Nejdůležitější: přesný ALS node + konkrétní SPU/AOP sousedé.
        for name in _V37_TARGETS:
            obj = await read_one(
                f"{phase}:ioregistry:name:{name}",
                lambda name=name: diag.ioregistry(name=name)
            )
            if obj:
                captured[f"name:{name}"] = obj

        # IOService obsahuje potvrzenou cestu:
        # RTBuddy(AOP) -> AOPEndpoint12 -> AppleSPU -> als -> ... -> AppleSPUVD6287
        ios = await read_one(
            f"{phase}:ioregistry:plane:IOService",
            lambda: diag.ioregistry(plane="IOService")
        )
        if ios:
            captured["plane:IOService"] = ios

        # IOReport metadata/counter větev; pokud Diagnostics Relay vrátí snapshot,
        # uložíme ho. UnknownRequest je jen evidován, probe pokračuje.
        for req in ("IOReportChannels", "IOReportLegend"):
            obj = await read_one(
                f"{phase}:raw:{req}:AOP",
                lambda req=req: diag._send_recv({"Request": req, "Channel": "AOP"})
            )
            if obj:
                captured[f"raw:{req}:AOP"] = obj

        merged_leaves = {}
        counters = []
        for source, obj in captured.items():
            for ident, leaf in _v37_leaf_map(obj).items():
                merged_leaves[f"{source}|{ident}"] = {"source": source, **leaf}
            for row in _v37_counter_candidates(obj):
                counters.append({"source": source, **row})

        samples.append({
            "index": len(samples),
            "t": round(_time.monotonic() - started, 6),
            "phase": phase,
            "phase_index": phase_index,
            "instruction": instruction,
            "leaves": merged_leaves,
            "counters": counters,
        })

    try:
        for phase_index, (phase, duration, instruction) in enumerate(_V37_PHASES):
            phase_start = _time.monotonic()
            while _time.monotonic() - phase_start < duration:
                await snapshot(phase, phase_index, instruction)
                elapsed = _time.monotonic() - phase_start
                await asyncio.sleep(max(0.0, interval - (elapsed % interval)))
    finally:
        try:
            cr = diag.close()
            if inspect.isawaitable(cr):
                await cr
        except Exception:
            pass

    # Timeline per stable leaf identity.
    timelines = {}
    for smp in samples:
        for ident, leaf in smp["leaves"].items():
            timelines.setdefault(ident, []).append({
                "i": smp["index"], "t": smp["t"], "phase": smp["phase"],
                "hex": leaf["hex"], "source": leaf["source"],
                "path": leaf["path"], "key": leaf["key"],
                "form": leaf["form"], "raw": leaf["raw"],
                "ancestors": leaf["ancestors"],
            })

    candidates, exact_hits = [], []
    for ident, seq in timelines.items():
        if len(seq) < 2:
            continue
        unique = []
        transitions = 0
        last = None
        phase_values = {}
        for x in seq:
            hx = x["hex"]
            if hx != last:
                transitions += 1
                last = hx
            if hx not in unique:
                unique.append(hx)
            phase_values.setdefault(x["phase"], set()).add(hx)

            raw = x["raw"]
            for transform, needle in patterns:
                off = raw.find(needle)
                if off >= 0:
                    exact_hits.append({
                        "identity": ident, "source": x["source"], "path": x["path"],
                        "key": x["key"], "form": x["form"], "phase": x["phase"],
                        "transform": transform, "offset": off, "raw_hex": hx,
                    })

        if len(unique) <= 1:
            continue

        ctx = " ".join((seq[0]["source"], seq[0]["path"], seq[0]["key"],
                        *seq[0]["ancestors"])).lower()
        score = 0
        for token, weight in (
            ("applespuvd6287", 300), ("als", 220), ("ambient", 180),
            ("aopendpoint12", 150), ("spu", 100), ("report", 80),
            ("state", 60), ("data", 50), ("serial", 40), ("calib", 35),
        ):
            if token in ctx:
                score += weight
        # Změna napříč fázemi je mnohem cennější než šum uvnitř jedné fáze.
        phase_signatures = {
            p: tuple(sorted(vals)) for p, vals in phase_values.items()
        }
        distinct_phase_sets = len(set(phase_signatures.values()))
        score += min(250, distinct_phase_sets * 50)
        score += min(100, transitions)

        raw_lengths = sorted({len(x["raw"]) for x in seq})
        six_byte = 6 in raw_lengths
        if six_byte:
            score += 180

        candidates.append({
            "score": score,
            "identity": ident,
            "source": seq[0]["source"],
            "path": seq[0]["path"],
            "key": seq[0]["key"],
            "form": seq[0]["form"],
            "ancestors": seq[0]["ancestors"],
            "sample_count": len(seq),
            "transitions": transitions,
            "unique_value_count": len(unique),
            "raw_lengths": raw_lengths,
            "six_byte": six_byte,
            "phase_values": {p: sorted(vals)[:100] for p, vals in phase_values.items()},
            "value_sequence": [
                {"i": x["i"], "t": x["t"], "phase": x["phase"], "hex": x["hex"]}
                for x in seq
            ][:1000],
        })

    candidates.sort(key=lambda x: (-x["score"], -int(x["six_byte"]),
                                   -x["transitions"], x["identity"]))
    exact_hits.sort(key=lambda x: (x["source"], x["path"], x["phase"]))

    # Surové samples bez duplicitních raw bytes; JSON-safe.
    serializable_samples = []
    for smp in samples:
        serializable_samples.append({
            "index": smp["index"], "t": smp["t"], "phase": smp["phase"],
            "phase_index": smp["phase_index"], "instruction": smp["instruction"],
            "leaf_count": len(smp["leaves"]),
            "counters": smp["counters"],
        })

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v37", f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir, exist_ok=True)

    result = {
        "ok": True,
        "probe": "als-event-correlated-last-value-v37-final",
        "read_only": True,
        "udid": udid,
        "als_reference": ref,
        "interval_requested": interval,
        "phases": [
            {"name": p, "duration_seconds": d, "instruction": ins}
            for p, d, ins in _V37_PHASES
        ],
        "exact_hits": exact_hits,
        "top_candidates": candidates[:500],
        "six_byte_changing_candidates": [x for x in candidates if x["six_byte"]][:500],
        "samples": serializable_samples,
        "calls": calls,
        "errors": errors,
        "summary": {
            "samples": len(samples),
            "calls_total": len(calls),
            "calls_ok": sum(1 for x in calls if x.get("ok")),
            "calls_truthy": sum(1 for x in calls if x.get("truthy")),
            "changing_leaves": len(candidates),
            "changing_six_byte_leaves": sum(1 for x in candidates if x["six_byte"]),
            "exact_hits": len(exact_hits),
            "duration_seconds": round(_time.monotonic() - started, 3),
        },
        "capture_dir": capture_dir,
        "files": {
            "manifest": "manifest.json",
            "candidates": "candidates.json",
            "exact_hits": "exact_hits.json",
            "samples": "samples.json",
        },
        "decision": (
            "FIRST inspect exact_hits. If empty, inspect six_byte_changing_candidates, "
            "then top_candidates. The strongest ALS last-value candidate should change "
            "between COVER/UNCOVER/LIGHT and live under the confirmed AOP->AppleSPU->als "
            "or AppleSPUVD6287 path. Do not promote a value that only changes as generic "
            "registry noise."
        ),
    }

    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        _json.dump(_v36_safe(result), fh, ensure_ascii=False, indent=2)
    with open(os.path.join(capture_dir, "candidates.json"), "w", encoding="utf-8") as fh:
        _json.dump(_v36_safe(candidates), fh, ensure_ascii=False, indent=2)
    with open(os.path.join(capture_dir, "exact_hits.json"), "w", encoding="utf-8") as fh:
        _json.dump(_v36_safe(exact_hits), fh, ensure_ascii=False, indent=2)
    with open(os.path.join(capture_dir, "samples.json"), "w", encoding="utf-8") as fh:
        _json.dump(_v36_safe(serializable_samples), fh, ensure_ascii=False, indent=2)

    return _v36_safe(result)

@app.route('/api/v37-als-final-probe/<udid>', methods=['GET'])
def api_v37_als_final_probe(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V36_ALS_DEFAULT_REF).strip()
        interval = request.args.get("interval", "0.20")
        result = _run_async_isolated(
            _v37_als_final_collect(udid, als_ref=als_ref, interval=float(interval)),
            timeout=900
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "als-event-correlated-last-value-v37-final",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V39 ALS HID TOPOLOGY / REPORT MAP ──────────────────────────────────────
_V39_ALS_REF = "0311133F6B07"
_V39_NAMES = (
    "als", "AppleSPUHIDDevice", "IOHIDInterface", "AppleSPUVD6287",
    "AppleSPUHIDDriver", "AppleSPUHIDDriverUserClient",
    "IOHIDEventServiceUserClient", "AppleSPU", "AOPEndpoint12",
)
_V39_PATH_TOKENS = ("aop", "aopendpoint12", "spu", "als", "hid", "vd6287",
                    "report", "descriptor", "provider", "consumer", "userclient")

def _v39_jsonable(v):
    if isinstance(v, bytes):
        return {"__type__": "bytes", "hex": v.hex().upper(), "length": len(v)}
    if isinstance(v, bytearray):
        b = bytes(v); return {"__type__": "bytearray", "hex": b.hex().upper(), "length": len(b)}
    if isinstance(v, dict):
        return {str(k): _v39_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_v39_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return {"__type__": type(v).__name__, "repr": repr(v)}

def _v39_walk(v, path="$", ancestors=None):
    ancestors = list(ancestors or [])
    if isinstance(v, dict):
        node_name = v.get("name") or v.get("className")
        anc = ancestors + ([str(node_name)] if node_name else [])
        yield path, v, anc
        for k, x in v.items():
            yield from _v39_walk(x, f"{path}.{k}", anc)
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            yield from _v39_walk(x, f"{path}[{i}]", ancestors)

def _v39_value(v):
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v); return {"type": type(v).__name__, "hex": b.hex().upper(), "length": len(b)}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return {"type": type(v).__name__, "value": v}
    return {"type": type(v).__name__, "repr": repr(v)[:2000]}

def _v39_extract(source, root):
    nodes, props, descriptors, report_ids = [], [], [], set()
    for path, node, ancestors in _v39_walk(root):
        hay = (" ".join(ancestors) + " " + path).lower()
        if not any(t in hay for t in _V39_PATH_TOKENS):
            continue
        name = str(node.get("name") or node.get("className") or "")
        nodes.append({"source": source, "path": path, "node_name": name or None,
                      "ancestors": ancestors[-18:], "keys": sorted(map(str, node.keys()))[:400]})
        for k, v in node.items():
            kl = str(k).lower()
            if not any(t in kl for t in ("report", "descriptor", "serial", "provider",
                                         "consumer", "client", "usage", "registry",
                                         "entry", "manufacturer", "product", "vendor",
                                         "transport", "location", "state", "inheritance")):
                continue
            row = {"source": source, "path": f"{path}.{k}", "key": str(k),
                   "ancestors": ancestors[-18:], "value": _v39_value(v)}
            props.append(row)
            if kl == "reportid" and isinstance(v, int):
                report_ids.add(v)
            if "descriptor" in kl:
                descriptors.append(row)
            if kl in ("inputreportelements", "outputreportelements", "featurereportelements"):
                seq = v if isinstance(v, (list, tuple)) else [v]
                for el in seq:
                    if isinstance(el, dict) and isinstance(el.get("ReportID"), int):
                        report_ids.add(el["ReportID"])
    return nodes, props, descriptors, report_ids

async def _v39_als_hid_topology_collect(udid, als_ref=_V39_ALS_REF):
    import inspect, datetime as _dt, json as _json, os, re
    ld, diag = await _open_diag(udid)
    calls, errors, captured = [], {}, []

    async def capture(label, fn):
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            captured.append((label, value))
            calls.append({"source": label, "ok": True, "truthy": bool(value),
                          "result_type": type(value).__name__})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            errors[label] = err
            calls.append({"source": label, "ok": False, "error": err})

    await capture("ioregistry:plane:IOService", lambda: diag.ioregistry(plane="IOService"))
    for name in _V39_NAMES:
        await capture(f"ioregistry:name:{name}", lambda name=name: diag.ioregistry(name=name))
        for selector in ("EntryName", "Name", "CurrentEntry", "RegistryEntryName"):
            payload = {"Request": "IORegistry", selector: name}
            await capture(f"diagnostics:raw:IORegistry:{selector}:{name}",
                          lambda payload=payload: diag._send_recv(payload))

    nodes, props, descriptors, report_ids = [], [], [], set()
    source_summaries = []
    for label, value in captured:
        n, p, d, r = _v39_extract(label, value)
        nodes += n; props += p; descriptors += d; report_ids.update(r)
        source_summaries.append({"source": label, "nodes": len(n), "properties": len(p),
                                 "descriptors": len(d), "report_ids": sorted(r)})

    def dedup(rows):
        seen, out = set(), []
        for row in rows:
            ident = repr(row)
            if ident not in seen:
                seen.add(ident); out.append(row)
        return out
    nodes, props, descriptors = dedup(nodes), dedup(props), dedup(descriptors)

    wanted = ("aopendpoint12", "applespu", "als", "applespuhiddevice",
              "iohidinterface", "applespuvd6287", "userclient")
    def rank(row):
        hay = (" ".join(row.get("ancestors", [])) + " " + row.get("path", "") +
               " " + str(row.get("node_name", ""))).lower()
        return (-sum(x in hay for x in wanted), row.get("source", ""), row.get("path", ""))
    nodes.sort(key=rank); props.sort(key=rank)

    exact_chain = []
    for row in nodes:
        hay = (" ".join(row.get("ancestors", [])) + " " + str(row.get("node_name", ""))).lower()
        if "als" in hay and "applespuhiddevice" in hay and "applespuvd6287" in hay:
            exact_chain.append(row)

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid = re.sub(r"[^A-Za-z0-9._-]", "_", udid)
    capture_dir = os.path.join(BASE_DIR, "discovery_v39", f"{safe_udid}_{stamp}")
    raw_dir = os.path.join(capture_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_files = []
    for i, (label, value) in enumerate(captured, 1):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:140]
        fn = f"{i:03d}_{safe}.json"
        with open(os.path.join(raw_dir, fn), "w", encoding="utf-8") as fh:
            _json.dump(_v39_jsonable(value), fh, ensure_ascii=False, indent=2)
        raw_files.append({"source": label, "file": os.path.join("raw", fn)})

    try:
        cr = diag.close()
        if inspect.isawaitable(cr): await cr
    except Exception:
        pass

    result = {
        "ok": True, "probe": "als-hid-topology-report-map-v39", "read_only": True,
        "udid": udid, "als_reference": als_ref,
        "confirmed_target_chain": ["RTBuddy(AOP)", "AOPEndpoint12", "AppleSPU", "als",
                                   "AppleSPUHIDDevice", "IOHIDInterface", "AppleSPUVD6287"],
        "report_ids": sorted(report_ids),
        "descriptor_candidates": descriptors[:500],
        "exact_chain_nodes": exact_chain[:500],
        "topology_nodes": nodes[:3000],
        "hid_properties": props[:10000],
        "source_summaries": source_summaries,
        "calls": calls, "errors": errors, "capture_dir": capture_dir,
        "files": {"manifest": "manifest.json", "topology": "topology.json",
                  "hid_properties": "hid_properties.json", "descriptors": "descriptors.json",
                  "raw_dir": "raw"},
        "summary": {"calls_total": len(calls),
                    "calls_ok": sum(1 for x in calls if x.get("ok")),
                    "calls_truthy": sum(1 for x in calls if x.get("truthy")),
                    "captured_sources": len(captured), "topology_nodes": len(nodes),
                    "hid_properties": len(props), "report_ids": sorted(report_ids),
                    "descriptor_candidates": len(descriptors),
                    "exact_chain_nodes": len(exact_chain)},
        "decision": ("Inspect exact_chain_nodes and hid_properties for the confirmed ALS chain. "
                     "Then inspect ReportID/ReportSize/ReportCount, InputReportElements and "
                     "descriptor_candidates. V39 performs no HID writes. If the component ID "
                     "is absent, correlate this exact report map with the private 3uTools request sequence.")
    }
    for fn, obj in (("manifest.json", result), ("topology.json", nodes),
                    ("hid_properties.json", props), ("descriptors.json", descriptors)):
        with open(os.path.join(capture_dir, fn), "w", encoding="utf-8") as fh:
            _json.dump(_v39_jsonable(obj), fh, ensure_ascii=False, indent=2)
    return _v39_jsonable(result)

@app.route('/api/v39-als-hid-topology/<udid>', methods=['GET'])
def api_v39_als_hid_topology(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V39_ALS_REF).strip()
        result = _run_async_isolated(
            _v39_als_hid_topology_collect(udid, als_ref=als_ref), timeout=900)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "probe": "als-hid-topology-report-map-v39",
                        "udid": udid, "error": f"{type(exc).__name__}: {exc}"}), 200



# ─── V40 3UTOOLS REQUEST-SEQUENCE CORRELATOR ────────────────────────────────
# PASSIVE / READ-ONLY. V39 confirmed the ALS chain but Diagnostics Relay does
# not expose its HID descriptor/report payload. V40 therefore correlates the
# single 3uTools "iDevice Details" action with passive Windows USB capture.
# It never sends a HID report or writes to the iPhone.
#
# Best backend: USBPcapCMD.exe (installed with USBPcap/Wireshark). The resulting
# pcap is scanned as raw bytes for the known ALS component ID and transformed
# variants. If tshark is available, V40 also exports per-packet usb.capdata /
# usb.data fragments and ranks packets around the ACTION phase.

_V40_DEFAULT_ALS_REF = "0311133F6B07"
_V40_PHASES = (
    ("BASELINE", 8.0, "Nechte 3uTools otevrene, ale NEOTVIREJTE iDevice Details."),
    ("ACTION", 18.0, "TED otevřete v 3uTools iDevice Details a nechte detail nacist."),
    ("AFTER", 8.0, "Na nic neklikejte; detail nechte otevreny."),
)

def _v40_which(names):
    import shutil as _shutil
    for name in names:
        p = _shutil.which(name)
        if p:
            return p
    extra = [
        r"C:\\Program Files\\USBPcap\\USBPcapCMD.exe",
        r"C:\\Program Files\\Wireshark\\tshark.exe",
        r"C:\\Program Files\\Wireshark\\USBPcapCMD.exe",
        r"C:\\Program Files (x86)\\USBPcap\\USBPcapCMD.exe",
    ]
    wanted = {str(n).lower() for n in names}
    for p in extra:
        if os.path.isfile(p) and os.path.basename(p).lower() in wanted:
            return p
    return None

def _v40_patterns(ref):
    return _v33_patterns(ref)

def _v40_scan_blob(raw, patterns, label, phase_bounds=None):
    hits = []
    for transform, needle in patterns:
        start = 0
        while needle:
            off = raw.find(needle, start)
            if off < 0:
                break
            hits.append({
                "source": label, "transform": transform, "offset": off,
                "needle_hex": needle.hex().upper(),
                "context_hex": raw[max(0, off-64):off+len(needle)+64].hex().upper(),
            })
            start = off + 1
    return hits

def _v40_usbpcap_roots(usbpcap):
    try:
        cp = subprocess.run([usbpcap, "-d"], capture_output=True, text=True,
                            errors="replace", timeout=15)
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    roots = []
    for line in text.splitlines():
        m = re.search(r"(\\\\\.\\USBPcap\d+)", line, re.I)
        if m and m.group(1) not in roots:
            roots.append(m.group(1))
    return roots, text

def _v40_process_snapshot():
    if os.name != "nt":
        return []
    ps = r'''$ErrorActionPreference='SilentlyContinue'; Get-CimInstance Win32_Process | Where-Object { $_.Name -match '3u|i4|Apple|MobileDevice|usbmux' -or $_.ExecutablePath -match '3uTools|i4Tools' } | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress'''
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, errors="replace", timeout=20)
        obj = json.loads(cp.stdout) if cp.stdout.strip() else []
        return obj if isinstance(obj, list) else [obj]
    except Exception:
        return []

def _v40_net_snapshot():
    if os.name != "nt":
        return []
    ps = r'''$ErrorActionPreference='SilentlyContinue'; Get-NetTCPConnection | Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess | ConvertTo-Json -Compress'''
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, errors="replace", timeout=20)
        obj = json.loads(cp.stdout) if cp.stdout.strip() else []
        return obj if isinstance(obj, list) else [obj]
    except Exception:
        return []

def _v40_tshark_packets(tshark, pcap_path, patterns):
    if not tshark or not os.path.isfile(pcap_path):
        return [], []
    fields = ["frame.number", "frame.time_epoch", "usb.src", "usb.dst",
              "usb.transfer_type", "usb.endpoint_address", "usb.capdata", "usb.data"]
    cmd = [tshark, "-r", pcap_path, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    cmd += ["-E", "separator=|", "-E", "occurrence=a"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180)
    except Exception:
        return [], []
    packets, exact = [], []
    for line in cp.stdout.splitlines():
        parts = line.split("|")
        parts += [""] * (len(fields)-len(parts))
        row = dict(zip(fields, parts[:len(fields)]))
        blobs = []
        for key in ("usb.capdata", "usb.data"):
            hx = re.sub(r"[^0-9A-Fa-f]", "", row.get(key, ""))
            if hx and len(hx) % 2 == 0:
                try: blobs.append((key, bytes.fromhex(hx)))
                except Exception: pass
        if not blobs:
            continue
        score = 0
        token = " ".join(str(row.get(k, "")) for k in row).lower()
        if row.get("usb.endpoint_address"): score += 10
        if row.get("usb.transfer_type"): score += 5
        row_hits = []
        for key, raw in blobs:
            hs = _v40_scan_blob(raw, patterns, f"packet:{row.get('frame.number')}:{key}")
            if hs:
                score += 10000
                row_hits.extend(hs)
            if 6 <= len(raw) <= 512: score += 25
        row["payloads"] = [{"field": k, "length": len(b), "hex": b[:4096].hex().upper()} for k,b in blobs]
        row["score"] = score
        if row_hits:
            exact.extend(row_hits)
        packets.append(row)
    packets.sort(key=lambda x: (-int(x.get("score", 0)), int(x.get("frame.number") or 0)))
    return packets[:5000], exact

async def _v40_3utools_sequence_collect(udid, als_ref=_V40_DEFAULT_ALS_REF,
                                        root=None, baseline=8.0, action=18.0, after=8.0):
    ref = re.sub(r"[^0-9A-Fa-f]", "", str(als_ref or "")).upper()
    patterns = _v40_patterns(ref)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    capture_dir = os.path.abspath(os.path.join("discovery_v40", f"{udid}_{stamp}"))
    os.makedirs(capture_dir, exist_ok=True)

    usbpcap = _v40_which(("USBPcapCMD.exe", "USBPcapCMD"))
    tshark = _v40_which(("tshark.exe", "tshark"))
    roots, root_diag = _v40_usbpcap_roots(usbpcap) if usbpcap else ([], "USBPcapCMD not found")
    selected_root = root or (roots[0] if len(roots) == 1 else None)

    pre = {"processes": _v40_process_snapshot(), "tcp": _v40_net_snapshot()}
    phase_log = []
    pcap_path = os.path.join(capture_dir, "3utools_usb.pcap")
    proc = None
    capture_error = None

    if usbpcap and selected_root:
        cmd = [usbpcap, "-d", selected_root, "-o", pcap_path, "-A"]
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=flags)
            await asyncio.sleep(1.0)
            if proc.poll() is not None:
                so, se = proc.communicate(timeout=5)
                capture_error = "USBPcap exited early: " + (se or so or b"").decode("utf-8", "replace")
                proc = None
        except Exception as exc:
            capture_error = f"{type(exc).__name__}: {exc}"
            proc = None

    durations = (("BASELINE", float(baseline), _V40_PHASES[0][2]),
                 ("ACTION", float(action), _V40_PHASES[1][2]),
                 ("AFTER", float(after), _V40_PHASES[2][2]))
    t0 = time.monotonic()
    for name, duration, instruction in durations:
        begin = time.monotonic() - t0
        print(f"[V40] {name}: {instruction} ({duration:.1f}s)", flush=True)
        await asyncio.sleep(max(0.1, duration))
        end = time.monotonic() - t0
        phase_log.append({"name": name, "start_s": round(begin, 6), "end_s": round(end, 6),
                          "duration_s": duration, "instruction": instruction})

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try: proc.kill()
            except Exception: pass

    post = {"processes": _v40_process_snapshot(), "tcp": _v40_net_snapshot()}
    raw_hits = []
    pcap_size = 0
    if os.path.isfile(pcap_path):
        pcap_size = os.path.getsize(pcap_path)
        try:
            with open(pcap_path, "rb") as fh:
                raw_hits = _v40_scan_blob(fh.read(), patterns, "raw_pcap")
        except Exception as exc:
            capture_error = capture_error or f"pcap scan: {type(exc).__name__}: {exc}"

    packets, packet_hits = _v40_tshark_packets(tshark, pcap_path, patterns)
    exact_hits = raw_hits + packet_hits

    result = {
        "ok": bool(os.path.isfile(pcap_path) and pcap_size > 0),
        "probe": "3utools-passive-usb-request-sequence-correlator-v40",
        "read_only": True,
        "udid": udid,
        "als_reference": ref,
        "confirmed_target_chain": ["RTBuddy(AOP)", "AOPEndpoint12", "AppleSPU", "als",
                                   "AppleSPUHIDDevice", "IOHIDInterface", "AppleSPUVD6287"],
        "backend": {"usbpcap": usbpcap, "tshark": tshark, "roots": roots,
                    "selected_root": selected_root, "root_diagnostics": root_diag,
                    "capture_error": capture_error},
        "phases": phase_log,
        "exact_hits": exact_hits,
        "ranked_usb_packets": packets,
        "snapshots": {"before": pre, "after": post},
        "capture_dir": capture_dir,
        "files": {"pcap": os.path.basename(pcap_path) if os.path.isfile(pcap_path) else None,
                  "manifest": "manifest.json"},
        "summary": {"pcap_bytes": pcap_size, "exact_hits": len(exact_hits),
                    "ranked_usb_packets": len(packets)},
        "decision": (
            "If exact_hits > 0, inspect the matching frame and its immediately preceding OUT/control "
            "transfer: that request/response pair is the prime 3uTools ALS read candidate. If exact_hits == 0 "
            "but capture succeeded, compare ACTION packets against BASELINE/AFTER using frame.time_epoch and "
            "rank short vendor/control payloads. If multiple USBPcap roots were found and selected_root is null, "
            "rerun with ?root=\\\\.\\USBPcapN for the root carrying the iPhone."
        ),
    }
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(_v36_safe(result), fh, ensure_ascii=False, indent=2)
    return _v36_safe(result)

@app.route('/api/v40-3utools-sequence/<udid>', methods=['GET'])
def api_v40_3utools_sequence(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V40_DEFAULT_ALS_REF).strip()
        root = (request.args.get("root") or "").strip() or None
        baseline = float(request.args.get("baseline", "8"))
        action = float(request.args.get("action", "18"))
        after = float(request.args.get("after", "8"))
        result = _run_async_isolated(
            _v40_3utools_sequence_collect(udid, als_ref, root, baseline, action, after),
            timeout=max(180, int(baseline + action + after + 240))
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False,
                        "probe": "3utools-passive-usb-request-sequence-correlator-v40",
                        "udid": udid, "error": f"{type(exc).__name__}: {exc}"}), 200



# ─── V41 MULTI-ROOT USBPCAP 3UTOOLS ALS CORRELATOR ──────────────────────────
# PASSIVE / READ-ONLY.
# V40 failed before capture because USBPcapCMD -d requires a device argument.
# V41 does not use "-d" as a listing command. It probes/captures candidate
# \\.\USBPcap1..16 directly, keeps every root that stays alive, and scans every
# resulting PCAP for the known ALS identifier and transformed variants.
_V41_DEFAULT_ALS_REF = "0311133F6B07"
_V41_PHASES = (
    ("BASELINE", 10.0, "3uTools nechte otevrene MIMO iDevice Details."),
    ("ACTION", 25.0, "TED otevřete iDevice Details a pockejte, az je videt Ambient Light / ALS hodnota."),
    ("AFTER", 10.0, "Detail s ALS hodnotou nechte otevreny a na nic neklikejte."),
)

def _v41_start_root(usbpcap, root, pcap_path):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    variants = (
        [usbpcap, "-d", root, "-o", pcap_path, "-A"],
        [usbpcap, "-d", root, "-o", pcap_path],
    )
    errors = []
    for cmd in variants:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=flags
            )
            time.sleep(0.8)
            if proc.poll() is None:
                return proc, cmd, None
            so, se = proc.communicate(timeout=3)
            errors.append((se or so or b"").decode("utf-8", "replace"))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return None, None, " | ".join(x.strip() for x in errors if x.strip())

def _v41_stop_capture(proc):
    if proc is None:
        return
    # USBPcapCMD normally stops cleanly on ENTER when stdin is available.
    try:
        if proc.stdin:
            proc.stdin.write(b"\n")
            proc.stdin.flush()
        proc.wait(timeout=8)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def _v41_phase_for_epoch(epoch, capture_epoch, phases):
    rel = float(epoch) - float(capture_epoch)
    for p in phases:
        if p["start_s"] <= rel <= p["end_s"]:
            return p["name"], rel
    return "OUTSIDE", rel

def _v41_tshark_packets(tshark, pcap_path, patterns, capture_epoch, phases, root):
    if not tshark or not os.path.isfile(pcap_path):
        return [], []
    fields = [
        "frame.number", "frame.time_epoch", "usb.src", "usb.dst",
        "usb.transfer_type", "usb.endpoint_address", "usb.device_address",
        "usb.bus_id", "usb.capdata", "usb.data"
    ]
    cmd = [tshark, "-r", pcap_path, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    cmd += ["-E", "separator=|", "-E", "occurrence=a"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            errors="replace", timeout=240)
    except Exception:
        return [], []

    packets, exact = [], []
    for line in cp.stdout.splitlines():
        parts = line.split("|")
        parts += [""] * (len(fields) - len(parts))
        row = dict(zip(fields, parts[:len(fields)]))
        try:
            phase, rel = _v41_phase_for_epoch(
                float(row.get("frame.time_epoch") or 0), capture_epoch, phases
            )
        except Exception:
            phase, rel = "UNKNOWN", None

        blobs = []
        for key in ("usb.capdata", "usb.data"):
            hx = re.sub(r"[^0-9A-Fa-f]", "", row.get(key, ""))
            if hx and len(hx) % 2 == 0:
                try:
                    blobs.append((key, bytes.fromhex(hx)))
                except Exception:
                    pass
        if not blobs:
            continue

        row_hits = []
        score = 0
        if phase == "ACTION":
            score += 500
        elif phase == "AFTER":
            score += 80
        if row.get("usb.endpoint_address"):
            score += 10
        if row.get("usb.transfer_type"):
            score += 5

        payloads = []
        for key, raw in blobs:
            hs = _v40_scan_blob(
                raw, patterns,
                f"{root}:frame:{row.get('frame.number')}:{key}"
            )
            if hs:
                score += 100000
                for h in hs:
                    h.update({
                        "root": root,
                        "pcap": os.path.basename(pcap_path),
                        "frame_number": row.get("frame.number"),
                        "frame_time_epoch": row.get("frame.time_epoch"),
                        "phase": phase,
                        "relative_s": rel,
                        "usb_src": row.get("usb.src"),
                        "usb_dst": row.get("usb.dst"),
                        "usb_transfer_type": row.get("usb.transfer_type"),
                        "usb_endpoint_address": row.get("usb.endpoint_address"),
                        "usb_device_address": row.get("usb.device_address"),
                        "payload_field": key,
                    })
                row_hits.extend(hs)
            if 6 <= len(raw) <= 512:
                score += 25
            payloads.append({
                "field": key, "length": len(raw),
                "hex": raw[:8192].hex().upper()
            })

        row["root"] = root
        row["pcap"] = os.path.basename(pcap_path)
        row["phase"] = phase
        row["relative_s"] = rel
        row["payloads"] = payloads
        row["score"] = score
        packets.append(row)
        exact.extend(row_hits)

    packets.sort(key=lambda x: (
        -int(x.get("score", 0)),
        int(x.get("frame.number") or 0)
    ))
    return packets, exact

async def _v41_3utools_multiroot_collect(
    udid, als_ref=_V41_DEFAULT_ALS_REF,
    baseline=10.0, action=25.0, after=10.0, max_roots=16
):
    ref = re.sub(r"[^0-9A-Fa-f]", "", str(als_ref or "")).upper()
    patterns = _v40_patterns(ref)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    capture_dir = os.path.abspath(
        os.path.join("discovery_v41", f"{udid}_{stamp}")
    )
    os.makedirs(capture_dir, exist_ok=True)

    usbpcap = _v40_which(("USBPcapCMD.exe", "USBPcapCMD"))
    tshark = _v40_which(("tshark.exe", "tshark"))
    pre = {
        "processes": _v40_process_snapshot(),
        "tcp": _v40_net_snapshot()
    }

    candidates = [rf"\\.\USBPcap{i}" for i in range(1, int(max_roots) + 1)]
    captures = []
    probe_errors = {}
    capture_epoch = time.time()

    if usbpcap:
        for i, root in enumerate(candidates, 1):
            pcap_path = os.path.join(capture_dir, f"USBPcap{i}.pcap")
            proc, cmd, err = _v41_start_root(usbpcap, root, pcap_path)
            if proc is not None:
                captures.append({
                    "root": root, "pcap_path": pcap_path,
                    "proc": proc, "cmd": cmd
                })
                print(f"[V41] CAPTURE ACTIVE: {root}", flush=True)
            else:
                probe_errors[root] = err
    else:
        probe_errors["backend"] = "USBPcapCMD not found"

    phase_log = []
    durations = (
        ("BASELINE", float(baseline), _V41_PHASES[0][2]),
        ("ACTION", float(action), _V41_PHASES[1][2]),
        ("AFTER", float(after), _V41_PHASES[2][2]),
    )
    t0 = time.monotonic()
    # capture_epoch must correspond to the phase timer origin, not process probe.
    phase_epoch = time.time()
    for name, duration, instruction in durations:
        begin = time.monotonic() - t0
        print(f"[V41] {name}: {instruction} ({duration:.1f}s)", flush=True)
        await asyncio.sleep(max(0.1, duration))
        end = time.monotonic() - t0
        phase_log.append({
            "name": name, "start_s": round(begin, 6),
            "end_s": round(end, 6), "duration_s": duration,
            "instruction": instruction
        })

    for cap in captures:
        _v41_stop_capture(cap["proc"])

    post = {
        "processes": _v40_process_snapshot(),
        "tcp": _v40_net_snapshot()
    }

    all_packets, exact_hits, raw_hits = [], [], []
    pcap_files = []
    for cap in captures:
        path = cap["pcap_path"]
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        pcap_files.append({
            "root": cap["root"],
            "file": os.path.basename(path) if os.path.isfile(path) else None,
            "bytes": size,
            "command": cap["cmd"],
        })
        if size:
            try:
                with open(path, "rb") as fh:
                    rh = _v40_scan_blob(
                        fh.read(), patterns, f"raw_pcap:{cap['root']}"
                    )
                    for h in rh:
                        h["root"] = cap["root"]
                        h["pcap"] = os.path.basename(path)
                    raw_hits.extend(rh)
            except Exception:
                pass
            packets, hits = _v41_tshark_packets(
                tshark, path, patterns, phase_epoch, phase_log, cap["root"]
            )
            all_packets.extend(packets)
            exact_hits.extend(hits)

    exact_hits = raw_hits + exact_hits
    all_packets.sort(key=lambda x: (
        -int(x.get("score", 0)),
        0 if x.get("phase") == "ACTION" else 1,
        str(x.get("root", "")),
        int(x.get("frame.number") or 0)
    ))

    action_packets = [x for x in all_packets if x.get("phase") == "ACTION"]
    short_action = [
        x for x in action_packets
        if any(1 <= int(p.get("length", 0)) <= 512 for p in x.get("payloads", []))
    ]

    result = {
        "ok": any(x["bytes"] > 0 for x in pcap_files),
        "probe": "3utools-multiroot-usb-als-correlator-v41",
        "read_only": True,
        "udid": udid,
        "als_reference": ref,
        "backend": {
            "usbpcap": usbpcap,
            "tshark": tshark,
            "candidate_roots": candidates,
            "active_roots": [x["root"] for x in captures],
            "probe_errors": probe_errors,
        },
        "test_instruction": (
            "BASELINE: 3uTools mimo iDevice Details. ACTION: otevrit presne "
            "iDevice Details a pockat, az je na obrazovce Ambient Light "
            f"{ref}. AFTER: detail nechat otevreny."
        ),
        "phases": phase_log,
        "pcap_files": pcap_files,
        "exact_hits": exact_hits,
        "ranked_usb_packets": all_packets[:10000],
        "top_action_packets": short_action[:5000],
        "snapshots": {"before": pre, "after": post},
        "capture_dir": capture_dir,
        "files": {
            "manifest": "manifest.json",
            "ranked_usb_packets": "ranked_usb_packets.json",
            "top_action_packets": "top_action_packets.json",
            "exact_hits": "exact_hits.json",
        },
        "summary": {
            "active_roots": len(captures),
            "pcaps_with_data": sum(1 for x in pcap_files if x["bytes"] > 0),
            "pcap_bytes_total": sum(x["bytes"] for x in pcap_files),
            "packets_with_payload": len(all_packets),
            "action_packets_with_payload": len(action_packets),
            "short_action_packets": len(short_action),
            "exact_hits": len(exact_hits),
        },
        "decision": (
            "If exact_hits > 0, use the matching frame and preceding OUT/control "
            "frames on the same root/device/endpoint as the prime ALS read sequence. "
            "If exact_hits == 0 but PCAP data exists, inspect top_action_packets and "
            "compare ACTION-only short payloads; the response may be encoded, framed, "
            "encrypted, or the ALS value may be derived after transport."
        ),
    }

    for fn, obj in (
        ("manifest.json", result),
        ("ranked_usb_packets.json", all_packets[:10000]),
        ("top_action_packets.json", short_action[:5000]),
        ("exact_hits.json", exact_hits),
    ):
        with open(os.path.join(capture_dir, fn), "w", encoding="utf-8") as fh:
            json.dump(_v36_safe(obj), fh, ensure_ascii=False, indent=2)

    return _v36_safe(result)

@app.route('/api/v41-3utools-als-capture/<udid>', methods=['GET'])
def api_v41_3utools_als_capture(udid):
    try:
        als_ref = (request.args.get("als_ref") or _V41_DEFAULT_ALS_REF).strip()
        baseline = float(request.args.get("baseline", "10"))
        action = float(request.args.get("action", "25"))
        after = float(request.args.get("after", "10"))
        max_roots = int(request.args.get("max_roots", "16"))
        result = _run_async_isolated(
            _v41_3utools_multiroot_collect(
                udid, als_ref, baseline, action, after, max_roots
            ),
            timeout=max(240, int(baseline + action + after + 300))
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "probe": "3utools-multiroot-usb-als-correlator-v41",
            "udid": udid,
            "error": f"{type(exc).__name__}: {exc}",
        }), 200



# ─── V42 USBPCAP ALL-DEVICES 3UTOOLS ALS CORRELATOR ─────────────────────────
# PASSIVE / READ-ONLY. V41 failed because USBPcap explicitly required -A.
_V42_DEFAULT_ALS_REF = "0311133F6B07"

def _v42_start_all_devices(usbpcap, pcap_path):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    variants = ([usbpcap, "-A", "-o", pcap_path],
                [usbpcap, "-o", pcap_path, "-A"])
    errors = []
    for cmd in variants:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags)
            time.sleep(1.2)
            if proc.poll() is None:
                return proc, cmd, None
            so, se = proc.communicate(timeout=3)
            errors.append((se or so or b"").decode("utf-8", errors="replace"))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return None, None, "\n---\n".join(x for x in errors if x)

def _v42_stop_capture(proc):
    if proc is None: return
    try:
        if proc.poll() is None and proc.stdin:
            proc.stdin.write(b"\n"); proc.stdin.flush(); time.sleep(1)
    except Exception: pass
    try:
        if proc.poll() is None:
            proc.terminate(); proc.wait(timeout=5)
    except Exception:
        try: proc.kill()
        except Exception: pass

def _v42_hex_bytes(value):
    text = str(value or "").strip().replace(":", "").replace(" ", "")
    try: return bytes.fromhex(text) if text else b""
    except Exception: return b""

def _v42_tshark_rows(tshark, pcap_path, patterns, capture_epoch, phase_log):
    if not tshark or not os.path.isfile(pcap_path):
        return [], [], None
    fields = ["frame.number","frame.time_epoch","frame.len","usb.src","usb.dst",
        "usb.device_address","usb.endpoint_address","usb.transfer_type","usb.data_len",
        "usb.capdata","usb.control.Response","usb.setup.bmRequestType",
        "usb.setup.bRequest","usb.setup.wValue","usb.setup.wIndex",
        "usb.setup.wLength","usbpcap.data"]
    cmd = [tshark,"-r",pcap_path,"-T","fields","-E","separator=\t",
           "-E","quote=n","-E","occurrence=f"]
    for field in fields: cmd += ["-e", field]
    try: cp = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception as exc: return [], [], f"{type(exc).__name__}: {exc}"
    if cp.returncode != 0:
        return [], [], (cp.stderr or cp.stdout or b"").decode("utf-8",errors="replace")[:20000]

    def phase_for(epoch):
        rel = epoch - capture_epoch
        for p in phase_log:
            if p["start_s"] <= rel <= p["end_s"]: return p["name"], rel
        return "OUTSIDE", rel

    packets, hits = [], []
    for line in cp.stdout.decode("utf-8",errors="replace").splitlines():
        cols = line.split("\t") + [""] * len(fields)
        row = dict(zip(fields, cols[:len(fields)]))
        try: epoch = float(row.get("frame.time_epoch") or 0)
        except Exception: epoch = 0.0
        phase, rel = phase_for(epoch)
        blobs = []
        for key in ("usb.capdata","usb.control.Response","usbpcap.data"):
            raw = _v42_hex_bytes(row.get(key))
            if raw: blobs.append((key, raw))
        if not blobs: continue
        score = 1000 if phase == "ACTION" else 0
        if "control" in str(row.get("usb.transfer_type") or "").lower(): score += 300
        if row.get("usb.setup.bmRequestType"): score += 150
        payloads = []
        for key, raw in blobs:
            if 1 <= len(raw) <= 512: score += 50
            if len(raw) == 6: score += 500
            for transform, needle in patterns:
                if not needle: continue
                start = 0
                while True:
                    off = raw.find(needle, start)
                    if off < 0: break
                    score += 100000
                    hits.append({"transform":transform,"needle_hex":needle.hex().upper(),
                        "offset":off,"payload_field":key,
                        "frame_number":row.get("frame.number"),
                        "frame_time_epoch":row.get("frame.time_epoch"),
                        "phase":phase,"relative_s":rel,
                        "usb_src":row.get("usb.src"),"usb_dst":row.get("usb.dst"),
                        "usb_device_address":row.get("usb.device_address"),
                        "usb_endpoint_address":row.get("usb.endpoint_address"),
                        "usb_transfer_type":row.get("usb.transfer_type")})
                    start = off + 1
            payloads.append({"field":key,"length":len(raw),
                             "hex":raw[:8192].hex().upper()})
        row.update({"phase":phase,"relative_s":rel,"payloads":payloads,"score":score})
        packets.append(row)
    packets.sort(key=lambda x:(-int(x.get("score",0)),
        0 if x.get("phase")=="ACTION" else 1,int(x.get("frame.number") or 0)))
    return packets, hits, None

def _v42_action_novelty(packets):
    def sigs(phase):
        out=set()
        for row in packets:
            if row.get("phase") != phase: continue
            for p in row.get("payloads",[]):
                if p.get("hex"):
                    out.add((row.get("usb_device_address"),row.get("usb_endpoint_address"),
                        row.get("usb_transfer_type"),p.get("field"),p.get("hex")))
        return out
    noise = sigs("BASELINE") | sigs("AFTER")
    novel=[]
    for row in packets:
        if row.get("phase") != "ACTION": continue
        is_novel=False
        for p in row.get("payloads",[]):
            sig=(row.get("usb_device_address"),row.get("usb_endpoint_address"),
                 row.get("usb_transfer_type"),p.get("field"),p.get("hex"))
            if p.get("hex") and sig not in noise: is_novel=True; break
        if is_novel:
            item=dict(row); item["novel_action_only"]=True
            item["score"]=int(item.get("score",0))+5000; novel.append(item)
    novel.sort(key=lambda x:(-int(x.get("score",0)),int(x.get("frame.number") or 0)))
    return novel

async def _v42_3utools_all_devices_collect(udid, als_ref=_V42_DEFAULT_ALS_REF,
                                           baseline=10.0, action=25.0, after=10.0):
    ref = re.sub(r"[^0-9A-Fa-f]","",str(als_ref or "")).upper()
    patterns = _v40_patterns(ref)
    try:
        raw_ref=bytes.fromhex(ref)
        extra=[("v42_raw_exact",raw_ref),("v42_raw_reverse",raw_ref[::-1]),
               ("v42_ascii_hex",ref.encode("ascii")),
               ("v42_ascii_hex_lower",ref.lower().encode("ascii"))]
        existing={(n,b) for n,b in patterns}
        patterns += [(n,b) for n,b in extra if (n,b) not in existing]
    except Exception: pass

    stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_udid=re.sub(r"[^A-Za-z0-9._-]","_",udid)
    capture_dir=os.path.join(BASE_DIR,"discovery_v42",f"{safe_udid}_{stamp}")
    os.makedirs(capture_dir,exist_ok=True)
    pcap_path=os.path.join(capture_dir,"USBPcap_ALL_DEVICES.pcap")
    usbpcap=_v40_which(("USBPcapCMD.exe","USBPcapCMD"))
    tshark=_v40_which(("tshark.exe","tshark"))
    pre={"processes":_v40_process_snapshot(),"tcp":_v40_net_snapshot()}
    proc=cmd=None; capture_error=None; capture_epoch=time.time()
    if usbpcap: proc,cmd,capture_error=_v42_start_all_devices(usbpcap,pcap_path)
    else: capture_error="USBPcapCMD not found"

    phase_log=[]; started=time.time()
    phases=(("BASELINE",float(baseline),"3uTools nechte otevrene MIMO iDevice Details."),
            ("ACTION",float(action),"TED otevřete iDevice Details a pockejte, az je videt Ambient Light / ALS hodnota."),
            ("AFTER",float(after),"Detail s ALS hodnotou nechte otevreny a na nic neklikejte."))
    try:
        for name,duration,instruction in phases:
            start_s=time.time()-started
            print(f"[V42] {name}: {instruction}",flush=True)
            time.sleep(max(0.0,duration))
            phase_log.append({"name":name,"duration_s":duration,"start_s":start_s,
                              "end_s":time.time()-started,"instruction":instruction})
    finally: _v42_stop_capture(proc)

    post={"processes":_v40_process_snapshot(),"tcp":_v40_net_snapshot()}
    pcap_size=os.path.getsize(pcap_path) if os.path.isfile(pcap_path) else 0
    raw_hits=[]
    if pcap_size:
        try:
            with open(pcap_path,"rb") as fh:
                raw_hits=_v40_scan_blob(fh.read(),patterns,"raw_pcap:ALL_DEVICES")
        except Exception: pass
    packets,packet_hits,tshark_error=_v42_tshark_rows(
        tshark,pcap_path,patterns,capture_epoch,phase_log)
    exact_hits=raw_hits+packet_hits
    novel_action=_v42_action_novelty(packets)
    action_packets=[x for x in packets if x.get("phase")=="ACTION"]
    short_action=[x for x in action_packets if any(
        1 <= int(p.get("length",0)) <= 512 for p in x.get("payloads",[]))]

    result={"ok":bool(pcap_size>0),
      "probe":"3utools-all-devices-usb-als-correlator-v42","read_only":True,
      "udid":udid,"als_reference":ref,
      "backend":{"usbpcap":usbpcap,"tshark":tshark,"capture_mode":"ALL_DEVICES_-A",
                 "capture_command":cmd,"capture_error":capture_error,"tshark_error":tshark_error},
      "test_instruction":f"BASELINE mimo detail. ACTION otevrit iDevice Details a cekat na ALS {ref}. AFTER detail nechat otevreny.",
      "phases":phase_log,
      "pcap_file":os.path.basename(pcap_path) if os.path.isfile(pcap_path) else None,
      "exact_hits":exact_hits,"novel_action_packets":novel_action[:10000],
      "top_action_packets":short_action[:10000],"ranked_usb_packets":packets[:20000],
      "snapshots":{"before":pre,"after":post},"capture_dir":capture_dir,
      "files":{"manifest":"manifest.json","pcap":os.path.basename(pcap_path) if os.path.isfile(pcap_path) else None,
               "exact_hits":"exact_hits.json","novel_action_packets":"novel_action_packets.json",
               "top_action_packets":"top_action_packets.json","ranked_usb_packets":"ranked_usb_packets.json"},
      "summary":{"pcap_bytes":pcap_size,"packets_with_payload":len(packets),
                 "action_packets_with_payload":len(action_packets),
                 "short_action_packets":len(short_action),
                 "novel_action_packets":len(novel_action),"exact_hits":len(exact_hits)},
      "decision":"FIRST exact_hits. If empty inspect novel_action_packets; then reconstruct the preceding OUT/control frame on the same device and endpoint."}
    for fn,obj in (("manifest.json",result),("exact_hits.json",exact_hits),
                   ("novel_action_packets.json",novel_action[:10000]),
                   ("top_action_packets.json",short_action[:10000]),
                   ("ranked_usb_packets.json",packets[:20000])):
        with open(os.path.join(capture_dir,fn),"w",encoding="utf-8") as fh:
            json.dump(_v36_safe(obj),fh,ensure_ascii=False,indent=2)
    return _v36_safe(result)

@app.route('/api/v42-3utools-als-capture/<udid>', methods=['GET'])
def api_v42_3utools_als_capture(udid):
    try:
        als_ref=(request.args.get("als_ref") or _V42_DEFAULT_ALS_REF).strip()
        baseline=float(request.args.get("baseline","10"))
        action=float(request.args.get("action","25"))
        after=float(request.args.get("after","10"))
        result=_run_async_isolated(_v42_3utools_all_devices_collect(
            udid,als_ref,baseline,action,after),
            timeout=max(240,int(baseline+action+after+300)))
        return jsonify(result),200
    except Exception as exc:
        return jsonify({"ok":False,"probe":"3utools-all-devices-usb-als-correlator-v42",
                        "udid":udid,"error":f"{type(exc).__name__}: {exc}"}),200

if __name__ == '__main__':
    print("─" * 52)
    print("  iSupply Scan Server")
    print("─" * 52)

    # ── KONTROLA LICENCE ──
    lic_ok, lic_msg = check_license()
    print(f"  Licence: {lic_msg}")
    if not lic_ok:
        print("  ⚠ Licence nenalezena – server poběží v aktivačním módu")
        print("  ⚠ Otevři prohlížeč a zadej licenční klíč")

    _check_apple_driver()
    init_db()
    t = threading.Thread(target=usb_monitor_thread, daemon=True)
    t.start()

    print("  Diagnostika:  http://localhost:5000")
    print("  Admin panel:  http://localhost:5000/admin")
    print("─" * 52)

    # Automaticky otevri prohlizec po startu
    import threading, webbrowser
    def _open():
        import time; time.sleep(2.5)
        webbrowser.open('http://localhost:5000')
    threading.Thread(target=_open, daemon=True).start()

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

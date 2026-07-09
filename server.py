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

# ─── MODEL DATABÁZE ─────────────────────────────────────────────────────────
# Překlad Apple ProductType → čitelný název + číslo modelu A-Series

APPLE_MODELS = {
    # iPhone 16 Series
    'iPhone17,1': ('iPhone 16 Pro', 'A3293'),
    'iPhone17,2': ('iPhone 16 Pro Max', 'A3292'),
    'iPhone17,3': ('iPhone 16 Plus', 'A3291'),
    'iPhone17,4': ('iPhone 16', 'A3290'),
    # iPhone 15 Series
    'iPhone16,1': ('iPhone 15', 'A3090'),
    'iPhone16,2': ('iPhone 15 Plus', 'A3093'),
    'iPhone16,3': ('iPhone 15 Pro', 'A3101'),
    'iPhone16,4': ('iPhone 15 Pro Max', 'A3105'),
    # iPhone 14 Series
    'iPhone15,2': ('iPhone 14 Pro', 'A2890'),
    'iPhone15,3': ('iPhone 14 Pro Max', 'A2893'),
    'iPhone15,4': ('iPhone 14', 'A2882'),
    'iPhone15,5': ('iPhone 14 Plus', 'A2886'),
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
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_fetch())
        print(f"  ✓ {result['name']} | IMEI: {result['imei']} | iOS: {result['ios']} | Baterie: {result['battery']}%")
        return result
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
        usbmux.list_devices()
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
    return jsonify({'ok': True, 'build': 'activation-diag-v1',
                    'endpoints': ['activation-diag']})

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
    return jsonify({'ok': True, 'devices': list(connected_devices.values())})

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

        return result

    loop = asyncio.new_event_loop()
    try:
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

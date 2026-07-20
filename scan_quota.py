"""
iSupply Scan — odecitani skenu z kvoty (klientska strana).

Modul se stara o komunikaci s licencnim serverem:
  · PRED skenem  -> authorize_scan()  overi, jestli licence ma volny sken
  · PO skenu     -> complete_scan()   ulozi baseline komponent na server

Dulezite: rozhoduje SERVER, ne tahle aplikace. Kdyz server rekne ne,
sken se nespusti. Tenhle soubor jen preposila odpoved.

Pouziti v server.py:
    import scan_quota
    scan_quota.configure(api_base=LICENSE_API, licence_key=LICENSE_KEY)
"""

import threading
import time

import requests

_API_BASE = "https://isupply-scan.cz"
_LICENCE_KEY = None
_TIMEOUT = 15

# Kdyz server neni dostupny, dovolime skenovat dal? False = tvrdy stop.
ALLOW_OFFLINE = False

# Kratka pamet, aby se pri opakovanem dotazu behem par vterin
# nevolal server znovu (frontend obcas vola dvakrat po sobe).
_cache = {}
_cache_lock = threading.Lock()
_CACHE_SEC = 20


def configure(api_base=None, licence_key=None, allow_offline=None):
    global _API_BASE, _LICENCE_KEY, ALLOW_OFFLINE
    if api_base:
        _API_BASE = api_base.rstrip("/")
    if licence_key:
        _LICENCE_KEY = licence_key
    if allow_offline is not None:
        ALLOW_OFFLINE = bool(allow_offline)


def _valid_imei(imei):
    s = str(imei or "").strip()
    return s.isdigit() and 14 <= len(s) <= 17


# ─────────────────────────────────────────────────────────────────────
# 1) Autorizace PRED skenem
# ─────────────────────────────────────────────────────────────────────
def authorize_scan(imei, model=None, ios_version=None):
    """
    Vraci dict:
      {'allowed': True,  'billed': True/False, 'scan_event_id': 123, ...}
      {'allowed': False, 'error': 'quota_exceeded', 'message': '...', ...}

    Nikdy nevyhazuje vyjimku — volajici jen kouka na ['allowed'].
    """
    if not _LICENCE_KEY:
        return {"allowed": False, "error": "no_licence",
                "message": "Chybi licencni klic (soubor licence.key)."}

    if not _valid_imei(imei):
        # Zarizeni jeste nedohlasilo IMEI. Neuctujeme, ale ani neblokujeme —
        # frontend si data dotahne a zavola znovu.
        return {"allowed": True, "billed": False, "reason": "imei_unknown"}

    key = str(imei)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SEC:
            return hit[1]

    try:
        r = requests.post(
            f"{_API_BASE}/api/scan/authorize",
            json={"licence_key": _LICENCE_KEY, "imei": key,
                  "model": model, "ios_version": ios_version},
            timeout=_TIMEOUT,
        )
        data = r.json() if r.content else {}

        if r.status_code == 402:
            data.setdefault("allowed", False)
            data.setdefault("message", "Vycerpana mesicni kvota skenu.")
        elif r.status_code == 403:
            data.setdefault("allowed", False)
            data.setdefault("message", "Licence neni aktivni.")
        elif r.status_code == 404:
            data.setdefault("allowed", False)
            data.setdefault("message", "Licencni klic nebyl rozpoznan.")
        elif r.status_code >= 400:
            data.setdefault("allowed", False)
            data.setdefault("message", f"Server vratil chybu {r.status_code}.")

        with _cache_lock:
            _cache[key] = (now, data)
        return data

    except Exception as exc:
        print(f"  [kvota] server nedostupny: {exc}")
        if ALLOW_OFFLINE:
            return {"allowed": True, "billed": False, "reason": "offline_grace"}
        return {"allowed": False, "error": "server_unreachable",
                "message": "Licencni server je nedostupny. "
                           "Zkontroluj pripojeni k internetu."}


# ─────────────────────────────────────────────────────────────────────
# 2) Ulozeni vysledku PO skenu
# ─────────────────────────────────────────────────────────────────────
def complete_scan(imei, device=None, components=None,
                  scan_event_id=None, grade=None):
    """
    Posle na server serialy komponent pro trvalou baseline.
    Bezi na pozadi — vysledek skenu na tom nezavisi.
    """
    if not _LICENCE_KEY or not _valid_imei(imei):
        return

    payload = {
        "licence_key": _LICENCE_KEY,
        "imei": str(imei),
        "device": device or {},
        "components": components or [],
        "scan_event_id": scan_event_id,
        "grade": grade,
    }

    def _send():
        try:
            requests.post(f"{_API_BASE}/api/scan/complete",
                          json=payload, timeout=_TIMEOUT)
        except Exception as exc:
            print(f"  [kvota] ulozeni baseline selhalo: {exc}")

    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────
# 3) Zustatek pro UI
# ─────────────────────────────────────────────────────────────────────
def licence_status():
    if not _LICENCE_KEY:
        return {"error": "no_licence"}
    try:
        r = requests.get(f"{_API_BASE}/api/licence/status",
                         params={"licence_key": _LICENCE_KEY}, timeout=_TIMEOUT)
        return r.json()
    except Exception as exc:
        return {"error": "server_unreachable", "detail": str(exc)}


_features_cache = {"t": 0.0, "list": None}
_FEATURES_CACHE_SEC = 300


def features(force=False):
    """Seznam funkci povolenych tarifem. Drzi se 5 minut v pameti,
    aby se licencni server nevolal pri kazdem dotazu."""
    now = time.time()
    if not force and _features_cache["list"] is not None \
            and now - _features_cache["t"] < _FEATURES_CACHE_SEC:
        return _features_cache["list"]
    st = licence_status()
    if st.get("error"):
        # Server nedostupny: drzime posledni znamy stav, jinak nic nepovolime.
        return _features_cache["list"] or []
    lst = list(st.get("features") or [])
    _features_cache.update(t=now, list=lst)
    return lst


def has_feature(feature, force=False):
    """Rychla kontrola, jestli tarif ma danou funkci (napr. 'excel_io')."""
    return feature in features(force=force)


# ─────────────────────────────────────────────────────────────────────
# 4) Prevod vysledku skenu na format pro /api/scan/complete
# ─────────────────────────────────────────────────────────────────────
def components_from_result(result):
    """
    Z vysledku _component_serials_collect() udela seznam pro server.
    Ocekava strukturu {'components': {'battery': {'serial': ..,
    'source_path': .., 'source_key': ..}, ...}} — pokud se lisi,
    uprav mapovani zde na jednom miste.
    """
    out = []
    comps = (result or {}).get("components") or {}
    if isinstance(comps, dict):
        for name, val in comps.items():
            if isinstance(val, dict):
                out.append({
                    "component": name,
                    "serial": val.get("serial") or val.get("value"),
                    "source_path": val.get("source_path") or val.get("path"),
                    "source_key": val.get("source_key") or val.get("key"),
                    "is_factory": bool(val.get("is_factory")),
                })
            else:
                out.append({"component": name, "serial": val})
    elif isinstance(comps, list):
        for val in comps:
            if isinstance(val, dict) and val.get("component"):
                out.append(val)
    return out


# ─────────────────────────────────────────────────────────────────────
# 5) Heartbeat — hlasi serveru, ze aplikace bezi
# ─────────────────────────────────────────────────────────────────────
_HEARTBEAT_SEC = 60   # kratsi interval = rychlejsi prechod na offline
_heartbeat_started = False
_HWID = None


def start_heartbeat(hwid, hostname=None, version=None):
    """Spusti vlakno, ktere pravidelne hlasi serveru, ze aplikace bezi."""
    global _heartbeat_started, _HWID
    _HWID = hwid
    if _heartbeat_started or not _LICENCE_KEY:
        return
    _heartbeat_started = True

    def _loop():
        while True:
            try:
                requests.post(
                    f"{_API_BASE}/api/licence/heartbeat",
                    json={"licence_key": _LICENCE_KEY, "hwid": hwid,
                          "hostname": hostname, "version": version},
                    timeout=_TIMEOUT,
                )
            except Exception:
                pass  # vypadek site neni duvod cokoli hlasit
            time.sleep(_HEARTBEAT_SEC)

    threading.Thread(target=_loop, daemon=True).start()


def send_offline():
    """
    Nahlasi serveru, ze aplikace koncí. Bez toho by v admin panelu
    zustala zelena tecka az do vyprseni okna neaktivity.
    """
    if not _LICENCE_KEY:
        return
    try:
        import socket
        requests.post(
            f"{_API_BASE}/api/licence/heartbeat",
            json={"licence_key": _LICENCE_KEY, "hwid": _HWID,
                  "hostname": socket.gethostname(), "offline": True},
            timeout=5,
        )
        print("  [kvota] odhlaseno")
    except Exception as exc:
        print(f"  [kvota] odhlaseni selhalo: {exc}")

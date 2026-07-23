"""
iSupply Scan — zakaznicke API v1 + verejne overeni skenu.

DVE ODLISNE VRSTVY, nepletli si je:

  /api/v1/...      NEVEREJNE. Vyzaduje API klic (hlavicka Authorization).
                   Vraci vsechno vcetne serialu komponent a IMEI - to jsou
                   data obchodnika.

  /overit/<kod>    VEREJNE. Odkaz, ktery obchodnik da ke svoji nabidce.
                   Vraci JEN to, co ma videt koncovy kupujici: model, grade,
                   kondici baterie, datum testu a stav komponent jako OK /
                   zavada. ZADNE serialy, ZADNE IMEI - to jsou obchodni data
                   a na verejny web nepatri.

Autorizace: API klic je zamerne jiny nez license_key. Licencni klic aktivuje
aplikaci u techniku; kdyby koloval v integracich a unikl, musel by se menit
vsem. API klic jde otocit bez dopadu na provoz.
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, Response

bp = Blueprint("api_v1", __name__)

# Sdilime pripojeni a pomocne funkce s modulem kvot, at je jedna pravda.
from quota import db, IMEI_RE  # noqa: E402

PUBLIC_BASE = os.environ.get("ISUPPLY_PUBLIC_BASE", "https://isupply-scan.cz")

# Kod do verejneho odkazu. Bez znaku, ktere jdou zamenit pri prepisu
# z papiru (0/O, 1/I/l), aby ho slo nadiktovat po telefonu.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_verify_code(length: int = 8) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


# ─────────────────────────────────────────────────────────────────────
#  Autorizace API klicem
# ─────────────────────────────────────────────────────────────────────
def _api_key_from_request():
    """Klic z hlavicky. Podporujeme 'Bearer <klic>' i holy klic,
    protoze ruzne e-shopove platformy posilaji ruzne."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth
    return (request.headers.get("X-API-Key") or "").strip()


def _licence_by_api_key(cur, key):
    if not key or len(key) < 20:
        return None
    cur.execute(
        """SELECT id, license_key, plan, active, unlimited, scan_limit,
                  period_start, period_end, company
             FROM licenses WHERE api_key = %s""", (key,))
    return cur.fetchone()


def require_api(fn):
    """Overi API klic a preda licenci jako prvni argument."""
    def wrapper(*args, **kwargs):
        key = _api_key_from_request()
        if not key:
            return jsonify(error="unauthorized",
                           message="Chybi API klic. Posli ho v hlavicce "
                                   "Authorization: Bearer <klic>."), 401
        with db() as cur:
            lic = _licence_by_api_key(cur, key)
            if not lic:
                return jsonify(error="unauthorized",
                               message="Neplatny API klic."), 401
            if not lic["active"]:
                return jsonify(error="licence_inactive",
                               message="Licence je neaktivni."), 403
            from quota import has_feature
            if not has_feature(lic["plan"], "api"):
                return jsonify(error="feature_unavailable",
                               message="API neni soucasti tarifu "
                                       f"{lic['plan']}."), 403
            return fn(lic, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def _page_args():
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except ValueError:
        limit = 50
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        offset = 0
    return limit, offset


def _iso(dt):
    return dt.isoformat() if dt else None


# ─────────────────────────────────────────────────────────────────────
#  Sprava API klice  (autorizace licencnim klicem, ne API klicem)
# ─────────────────────────────────────────────────────────────────────
@bp.route("/api/v1/key", methods=["POST"])
def issue_api_key():
    """Vyda novy API klic. Volani znovu klic OTOCI - stary hned prestane
    platit, takze se tim resi i unik."""
    data = request.get_json(silent=True) or {}
    licence_key = (data.get("licence_key") or "").strip()
    if not licence_key:
        return jsonify(error="bad_request", message="Chybi licence_key."), 400

    with db() as cur:
        cur.execute("""SELECT id, plan, active FROM licenses
                        WHERE license_key = %s FOR UPDATE""", (licence_key,))
        lic = cur.fetchone()
        if not lic:
            return jsonify(error="licence_invalid"), 404
        if not lic["active"]:
            return jsonify(error="licence_inactive"), 403
        from quota import has_feature
        if not has_feature(lic["plan"], "api"):
            return jsonify(error="feature_unavailable",
                           message=f"API neni soucasti tarifu {lic['plan']}."), 403

        new_key = "isk_" + secrets.token_urlsafe(32)
        cur.execute("""UPDATE licenses
                          SET api_key = %s, api_key_created = now()
                        WHERE id = %s""", (new_key, lic["id"]))
    return jsonify(ok=True, api_key=new_key,
                   note="Ulozte si ho, znovu se nezobrazi. "
                        "Dalsi volani tohohle endpointu klic otoci.")


# ─────────────────────────────────────────────────────────────────────
#  Ctení: zarizeni
# ─────────────────────────────────────────────────────────────────────
@bp.route("/api/v1/devices", methods=["GET"])
@require_api
def list_devices(lic):
    """Zarizeni, ktera tahle licence testovala.

    Filtry: ?state=tested|listed|sold  ?model=iPhone%2015  ?since=2026-07-01
    Strankovani: ?limit=50&offset=0
    """
    limit, offset = _page_args()
    where = ["se.license_id = %s"]
    params = [lic["id"]]

    if request.args.get("state"):
        where.append("d.lifecycle_state = %s")
        params.append(request.args["state"])
    if request.args.get("model"):
        where.append("d.model ILIKE %s")
        params.append(f"%{request.args['model']}%")
    if request.args.get("since"):
        where.append("se.created_at >= %s")
        params.append(request.args["since"])

    sql = f"""
        SELECT DISTINCT ON (d.imei)
               d.imei, d.serial_number, d.model, d.model_identifier,
               d.capacity_gb, d.color, d.lifecycle_state, d.last_grade,
               d.external_ref, d.scan_count, d.first_seen_at, d.last_seen_at,
               se.verify_code, se.battery_health, se.battery_cycles,
               se.components_ok, se.components_total, se.created_at AS last_scan_at
          FROM devices d
          JOIN scan_events se ON se.imei = d.imei
         WHERE {' AND '.join(where)}
         ORDER BY d.imei, se.created_at DESC
         LIMIT %s OFFSET %s"""
    params += [limit, offset]

    with db() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return jsonify(
        ok=True, count=len(rows), limit=limit, offset=offset,
        devices=[{
            "device_id": r["imei"],
            "imei": r["imei"] if IMEI_RE.match(r["imei"] or "") else None,
            "serial_number": r["serial_number"],
            "model": r["model"],
            "model_identifier": r["model_identifier"],
            "capacity_gb": r["capacity_gb"],
            "color": r["color"],
            "grade": r["last_grade"],
            "state": r["lifecycle_state"],
            "external_ref": r["external_ref"],
            "battery": {"health_pct": r["battery_health"],
                        "cycles": r["battery_cycles"]},
            "components": {"ok": r["components_ok"],
                           "total": r["components_total"]},
            "scan_count": r["scan_count"],
            "first_seen_at": _iso(r["first_seen_at"]),
            "last_scan_at": _iso(r["last_scan_at"]),
            "verify_url": (f"{PUBLIC_BASE}/overit/{r['verify_code']}"
                           if r["verify_code"] else None),
        } for r in rows])


@bp.route("/api/v1/devices/<device_id>", methods=["GET"])
@require_api
def device_detail(lic, device_id):
    """Kompletni karta kusu vcetne serialu komponent a historie vymen.
    device_id = IMEI u telefonu, seriove cislo u iPadu."""
    device_id = (device_id or "").strip()
    with db() as cur:
        # Overit, ze kus patri TEHLE licenci - jinak by slo cist cizi data.
        cur.execute("""SELECT 1 FROM scan_events
                        WHERE license_id = %s AND imei = %s LIMIT 1""",
                    (lic["id"], device_id))
        if not cur.fetchone():
            return jsonify(error="not_found",
                           message="Zarizeni nenalezeno pod touhle licenci."), 404

        cur.execute("""SELECT * FROM devices WHERE imei = %s""", (device_id,))
        dev = cur.fetchone()

        cur.execute("""SELECT component, is_factory, serial, source_path,
                              source_key, ios_version, first_seen_at, last_seen_at
                         FROM device_components WHERE imei = %s
                        ORDER BY component, is_factory DESC""", (device_id,))
        comps = cur.fetchall()

        cur.execute("""SELECT component, old_serial, new_serial, verdict,
                              source_key, created_at
                         FROM device_component_history
                        WHERE imei = %s ORDER BY created_at DESC LIMIT 100""",
                    (device_id,))
        hist = cur.fetchall()

        cur.execute("""SELECT id, created_at, billed, grade, battery_health,
                              battery_cycles, components_ok, components_total,
                              ios_version, technician, verify_code
                         FROM scan_events
                        WHERE license_id = %s AND imei = %s
                        ORDER BY created_at DESC LIMIT 50""",
                    (lic["id"], device_id))
        scans = cur.fetchall()

    # Komponenty slouceny do jedne polozky: puvodni (baseline) + aktualni.
    merged = {}
    for c in comps:
        item = merged.setdefault(c["component"], {
            "component": c["component"], "original": None, "current": None,
            "source_key": c["source_key"], "source_path": c["source_path"]})
        item["original" if c["is_factory"] else "current"] = c["serial"]
    for item in merged.values():
        o, cur_v = item["original"], item["current"]
        item["verdict"] = ("MATCH" if o and cur_v and o == cur_v
                           else "MISMATCH" if o and cur_v
                           else "FIRST_SEEN" if not o
                           else "MISSING")

    return jsonify(ok=True, device={
        "device_id": dev["imei"],
        "imei": dev["imei"] if IMEI_RE.match(dev["imei"] or "") else None,
        "serial_number": dev["serial_number"],
        "model": dev["model"],
        "model_identifier": dev["model_identifier"],
        "capacity_gb": dev["capacity_gb"],
        "color": dev["color"],
        "grade": dev["last_grade"],
        "state": dev["lifecycle_state"],
        "external_ref": dev["external_ref"],
        "scan_count": dev["scan_count"],
        "first_seen_at": _iso(dev["first_seen_at"]),
        "last_seen_at": _iso(dev["last_seen_at"]),
        "components": list(merged.values()),
        "component_history": [{
            "component": h["component"], "from": h["old_serial"],
            "to": h["new_serial"], "verdict": h["verdict"],
            "source_key": h["source_key"], "at": _iso(h["created_at"]),
        } for h in hist],
        "scans": [{
            "scan_id": s["id"], "at": _iso(s["created_at"]),
            "billed": s["billed"], "grade": s["grade"],
            "battery": {"health_pct": s["battery_health"],
                        "cycles": s["battery_cycles"]},
            "components": {"ok": s["components_ok"],
                           "total": s["components_total"]},
            "ios_version": s["ios_version"], "technician": s["technician"],
            "verify_url": (f"{PUBLIC_BASE}/overit/{s['verify_code']}"
                           if s["verify_code"] else None),
        } for s in scans],
    })


@bp.route("/api/v1/devices/<device_id>/state", methods=["PATCH", "POST"])
@require_api
def set_state(lic, device_id):
    """Zmena stavu kusu v zivotnim cyklu + volitelne reference do e-shopu.

    Body: {"state": "listed", "external_ref": "eshop-produkt-4712"}

    Zamerne to NENI sklad: mnozstvi ani rezervace tu nejsou. Refurb kus je
    unikat, takze mnozstvi je vzdy 1 a stav zasob si eshop resi sam.
    """
    data = request.get_json(silent=True) or {}
    state = (data.get("state") or "").strip()
    allowed = ("tested", "listed", "sold", "returned", "scrapped")
    if state not in allowed:
        return jsonify(error="bad_request",
                       message=f"state musi byt jeden z: {', '.join(allowed)}"), 400

    with db() as cur:
        cur.execute("""SELECT 1 FROM scan_events
                        WHERE license_id = %s AND imei = %s LIMIT 1""",
                    (lic["id"], device_id))
        if not cur.fetchone():
            return jsonify(error="not_found"), 404
        cur.execute("""UPDATE devices
                          SET lifecycle_state = %s,
                              external_ref = coalesce(%s, external_ref)
                        WHERE imei = %s""",
                    (state, data.get("external_ref"), device_id))
    return jsonify(ok=True, device_id=device_id, state=state)


@bp.route("/api/v1/scans", methods=["GET"])
@require_api
def list_scans(lic):
    """Skeny za obdobi. Hodi se na fakturaci a na prehled vytizeni."""
    limit, offset = _page_args()
    where = ["se.license_id = %s"]
    params = [lic["id"]]
    if request.args.get("since"):
        where.append("se.created_at >= %s")
        params.append(request.args["since"])
    if request.args.get("until"):
        where.append("se.created_at < %s")
        params.append(request.args["until"])
    if request.args.get("billed") in ("true", "false"):
        where.append("se.billed = %s")
        params.append(request.args["billed"] == "true")

    with db() as cur:
        cur.execute(f"""
            SELECT se.id, se.imei, se.created_at, se.billed, se.grade,
                   se.battery_health, se.battery_cycles, se.components_ok,
                   se.components_total, se.model, se.ios_version,
                   se.technician, se.verify_code
              FROM scan_events se
             WHERE {' AND '.join(where)}
             ORDER BY se.created_at DESC
             LIMIT %s OFFSET %s""", params + [limit, offset])
        rows = cur.fetchall()

    return jsonify(ok=True, count=len(rows), limit=limit, offset=offset,
                   scans=[{
                       "scan_id": r["id"], "device_id": r["imei"],
                       "at": _iso(r["created_at"]), "billed": r["billed"],
                       "model": r["model"], "grade": r["grade"],
                       "battery": {"health_pct": r["battery_health"],
                                   "cycles": r["battery_cycles"]},
                       "components": {"ok": r["components_ok"],
                                      "total": r["components_total"]},
                       "ios_version": r["ios_version"],
                       "technician": r["technician"],
                       "verify_url": (f"{PUBLIC_BASE}/overit/{r['verify_code']}"
                                      if r["verify_code"] else None),
                   } for r in rows])


@bp.route("/api/v1/usage", methods=["GET"])
@require_api
def usage(lic):
    """Kolik skenu z tarifu je vycerpano. Same cislo, jake vidi aplikace."""
    with db() as cur:
        cur.execute("""SELECT count(*) AS used FROM scan_events
                        WHERE license_id = %s AND billed
                          AND created_at >= %s""",
                    (lic["id"], lic["period_start"]))
        used = cur.fetchone()["used"]
        cur.execute("""SELECT coalesce(sum(remaining),0) AS c FROM credit_packs
                        WHERE license_id = %s AND remaining > 0
                          AND (expires_at IS NULL OR expires_at > now())""",
                    (lic["id"],))
        credits = cur.fetchone()["c"]
    return jsonify(ok=True, plan=lic["plan"], unlimited=lic["unlimited"],
                   scan_limit=lic["scan_limit"], used=used,
                   remaining=(None if lic["unlimited"]
                              else max(lic["scan_limit"] - used, 0)),
                   credits=credits,
                   period_start=_iso(lic["period_start"]),
                   period_end=_iso(lic["period_end"]))


# ─────────────────────────────────────────────────────────────────────
#  Webhooky
# ─────────────────────────────────────────────────────────────────────
@bp.route("/api/v1/webhooks", methods=["GET", "POST"])
@require_api
def webhooks(lic):
    if request.method == "GET":
        with db() as cur:
            cur.execute("""SELECT id, url, event, active, last_status,
                                  last_error, last_sent_at, created_at
                             FROM webhooks WHERE license_id = %s
                            ORDER BY id""", (lic["id"],))
            rows = cur.fetchall()
        return jsonify(ok=True, webhooks=[dict(r) for r in rows])

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("https://"):
        return jsonify(error="bad_request",
                       message="url musi zacinat https://"), 400
    secret = "whsec_" + secrets.token_urlsafe(24)
    with db() as cur:
        cur.execute("""INSERT INTO webhooks (license_id, url, secret, event)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (lic["id"], url, secret, data.get("event") or "scan.completed"))
        wid = cur.fetchone()["id"]
    return jsonify(ok=True, id=wid, secret=secret,
                   note="Kazdy pozadavek podepisujeme hlavickou "
                        "X-iSupply-Signature (HMAC-SHA256 tela timhle tajemstvim). "
                        "Overte ji, jinak vam muze poslat data kdokoli.")


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def fire_webhooks(cur, license_id, event, payload):
    """Odesle webhook. Chyba NESMI shodit sken - proto jen zaznamenavame."""
    import urllib.request
    cur.execute("""SELECT id, url, secret FROM webhooks
                    WHERE license_id = %s AND active AND event = %s""",
                (license_id, event))
    for hook in cur.fetchall():
        body = json.dumps({"event": event, "data": payload},
                          ensure_ascii=False).encode()
        req = urllib.request.Request(
            hook["url"], data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-iSupply-Signature": sign_payload(hook["secret"], body),
                     "User-Agent": "iSupply-Scan-Webhook/1"})
        status, err = None, None
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        cur.execute("""UPDATE webhooks SET last_status = %s, last_error = %s,
                                           last_sent_at = now()
                        WHERE id = %s""", (status, err, hook["id"]))


# ─────────────────────────────────────────────────────────────────────
#  VEREJNE overeni
# ─────────────────────────────────────────────────────────────────────
def _public_payload(cur, code):
    """Data pro verejnou stranku. VEDOME vynechava serialy a IMEI -
    to jsou obchodni data prodejce, ne informace pro kupujiciho."""
    cur.execute("""
        SELECT se.created_at, se.grade, se.battery_health, se.battery_cycles,
               se.components_ok, se.components_total, se.ios_version,
               d.model, d.capacity_gb, d.color, l.company
          FROM scan_events se
          JOIN devices d ON d.imei = se.imei
          JOIN licenses l ON l.id = se.license_id
         WHERE se.verify_code = %s""", (code,))
    return cur.fetchone()


@bp.route("/api/v1/verify/<code>", methods=["GET"])
def verify_json(code):
    with db() as cur:
        row = _public_payload(cur, (code or "").strip().upper())
    if not row:
        return jsonify(ok=False, error="not_found"), 404
    return jsonify(ok=True, verified=True, report={
        "model": row["model"],
        "capacity_gb": row["capacity_gb"],
        "color": row["color"],
        "grade": row["grade"],
        "battery_health_pct": row["battery_health"],
        "battery_cycles": row["battery_cycles"],
        "components_ok": row["components_ok"],
        "components_total": row["components_total"],
        "ios_version": row["ios_version"],
        "tested_at": _iso(row["created_at"]),
        "tested_by": row["company"],
    })


@bp.route("/overit/<code>", methods=["GET"])
def verify_page(code):
    with db() as cur:
        row = _public_payload(cur, (code or "").strip().upper())

    if not row:
        html = _page_shell(
            "Kód nenalezen",
            "<p class='lead'>Tenhle ověřovací kód neznáme. Zkontroluj, jestli "
            "sedí s tím na štítku nebo v inzerátu.</p>")
        return Response(html, status=404, mimetype="text/html")

    ok = row["components_ok"] or 0
    total = row["components_total"] or 0
    vse_ok = total > 0 and ok == total
    bh = row["battery_health"]
    datum = row["created_at"].strftime("%-d. %-m. %Y") if row["created_at"] else "—"

    html = _page_shell(f"{row['model'] or 'Zařízení'} — ověřený test", f"""
      <div class="grade-row">
        <div class="box">
          <span class="lbl">Grade</span>
          <strong class="big">{row['grade'] or '—'}</strong>
        </div>
        <div class="box">
          <span class="lbl">Baterie</span>
          <strong class="big">{f'{bh} %' if bh else '—'}</strong>
          <span class="sub">{f"{row['battery_cycles']} cyklů" if row['battery_cycles'] else ''}</span>
        </div>
        <div class="box">
          <span class="lbl">Komponenty</span>
          <strong class="big">{ok}/{total}</strong>
          <span class="sub">{'vše v pořádku' if vse_ok else 'zjištěn nález'}</span>
        </div>
      </div>
      <p class="lead">
        {row['model'] or 'Zařízení'}{f" · {row['capacity_gb']} GB" if row['capacity_gb'] else ''}{f" · {row['color']}" if row['color'] else ''}
        {f" · iOS {row['ios_version']}" if row['ios_version'] else ''}
      </p>
      <p class="meta">Testováno {datum}{f" · {row['tested_by']}" if row.get('tested_by') else ''}</p>
      <p class="note">
        Test proběhl nástrojem iSupply Scan přímo na zařízení. Kontroluje se
        stav komponent proti záznamu z prvního testu, kondice baterie a
        záznamy o pádech systému. Výsledek nelze zpětně upravit.
      </p>""")
    return Response(html, mimetype="text/html")


def _page_shell(title, body):
    """Verejna stranka. Zamerne strizlivá a bez zavislosti - nacte se
    i na mobilu s pomalym pripojenim, coz je typicky pripad, kdy si
    kupujici kod overuje primo u prodejce."""
    return f"""<!doctype html>
<html lang="cs"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} · iSupply Scan</title>
<style>
  :root {{ --bg:#0A0A0C; --fg:#fff; --dim:#8b8f98; --pink:#F05CB6; --line:#22242b; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; background:var(--bg); color:var(--fg); min-height:100vh;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; padding:24px }}
  .card {{ width:100%; max-width:560px }}
  .brand {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
            letter-spacing:.22em; color:var(--dim); margin-bottom:22px }}
  .brand b {{ color:var(--pink); font-weight:600 }}
  h1 {{ font-size:26px; line-height:1.2; margin:0 0 20px; font-weight:650 }}
  .grade-row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:22px }}
  .box {{ border:1px solid var(--line); border-radius:12px; padding:14px 12px }}
  .lbl {{ display:block; font-family:ui-monospace,monospace; font-size:10px;
          letter-spacing:.12em; color:var(--dim); text-transform:uppercase }}
  .big {{ display:block; font-size:26px; line-height:1.15; margin-top:6px }}
  .sub {{ display:block; font-size:11px; color:var(--dim); margin-top:3px }}
  .lead {{ font-size:15px; color:#d6d8dd; margin:0 0 6px }}
  .meta {{ font-family:ui-monospace,monospace; font-size:12px; color:var(--dim); margin:0 0 22px }}
  .note {{ font-size:13px; line-height:1.65; color:var(--dim);
           border-top:1px solid var(--line); padding-top:16px; margin:0 }}
  a {{ color:var(--pink) }}
  @media (max-width:420px) {{ .grade-row {{ grid-template-columns:1fr; }} }}
</style></head>
<body><div class="card">
  <div class="brand">iSupply <b>SCAN</b> · OVĚŘENÍ TESTU</div>
  <h1>{title}</h1>
  {body}
</div></body></html>"""

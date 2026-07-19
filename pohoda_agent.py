"""
POHODA agent — běží na Windows PC vedle POHODY.

Každou minutu se zeptá serveru, jestli je něco k vystavení, přeloží to na
POHODA XML, pošle na mServer (localhost) a přidělené číslo dokladu i VS
vrátí zpátky na server.

Ven se neotevírá žádný port. Veškerá komunikace je odchozí.

Spuštění:
    python pohoda_agent.py                 # normální běh
    python pohoda_agent.py --dry-run       # jen vypíše XML, nic neodešle
    python pohoda_agent.py --once          # jeden průchod a konec

Konfigurace: soubor agent.ini vedle skriptu, nebo proměnné prostředí.

⚠️ XML schéma se mezi verzemi POHODY liší. Před ostrým během pusť
   --dry-run, vzniklé XML zkus ručně naimportovat a ověř, že sedí
   sazby DPH, číselná řada a forma úhrady.
"""

import argparse
import configparser
import datetime as dt
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# ── konfigurace ──────────────────────────────────────────────────────
cfg = configparser.ConfigParser()
cfg.read(HERE / "agent.ini", encoding="utf-8")


def conf(section, key, default=None):
    env = os.environ.get(f"POHODA_{key.upper()}")
    if env:
        return env
    try:
        return cfg.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


API_BASE    = conf("server", "api_base", "https://isupply-scan.cz")
AGENT_TOKEN = conf("server", "token", "")
POLL_SEC    = int(conf("server", "poll_seconds", "60"))

MSERVER_URL = conf("pohoda", "mserver_url", "http://127.0.0.1:444/xml")
MSERVER_USER = conf("pohoda", "user", "@")
MSERVER_PASS = conf("pohoda", "password", "")
ICO          = conf("pohoda", "ico", "23199351")

# číselné řady a účty — musí odpovídat nastavení v POHODĚ
SERIES_CARD     = conf("pohoda", "series_card", "")       # řada pro kartové platby
SERIES_TRANSFER = conf("pohoda", "series_transfer", "")    # řada pro faktury na převod
ACCOUNT_STRIPE  = conf("pohoda", "account_stripe", "STRIPE")
ACCOUNT_BANK    = conf("pohoda", "account_bank", "")
DUE_DAYS        = int(conf("pohoda", "due_days", "14"))

NS = {
    "dat": "http://www.stormware.cz/schema/version_2/data.xsd",
    "inv": "http://www.stormware.cz/schema/version_2/invoice.xsd",
    "typ": "http://www.stormware.cz/schema/version_2/type.xsd",
    "lst": "http://www.stormware.cz/schema/version_2/list.xsd",
    "rsp": "http://www.stormware.cz/schema/version_2/response.xsd",
    "ftr": "http://www.stormware.cz/schema/version_2/filter.xsd",
}
for p, u in NS.items():
    ET.register_namespace(p, u)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler(HERE / "agent.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pohoda-agent")


def q(prefix, tag):
    return f"{{{NS[prefix]}}}{tag}"


# ── sestavení XML ────────────────────────────────────────────────────
def build_invoice_xml(inv):
    """Jeden dataPack s jednou vydanou fakturou."""
    today = dt.date.today()
    is_card = inv.get("payment_method", "card") == "card"

    # reverse charge: firma v EU mimo ČR s platným DIČ
    country = (inv.get("country") or "").upper()
    reverse_charge = bool(inv.get("vat_id")) and country and country != "CZ"

    pack = ET.Element(q("dat", "dataPack"), {
        "version": "2.0", "id": inv["ref"], "ico": ICO,
        "application": "iSupply Scan", "note": "automaticky z iSupply Scan",
    })
    item = ET.SubElement(pack, q("dat", "dataPackItem"),
                         {"version": "2.0", "id": inv["ref"]})
    invoice = ET.SubElement(item, q("inv", "invoice"), {"version": "2.0"})

    # ── hlavička ──
    h = ET.SubElement(invoice, q("inv", "invoiceHeader"))
    ET.SubElement(h, q("inv", "invoiceType")).text = "issuedInvoice"

    series = SERIES_CARD if is_card else SERIES_TRANSFER
    if series:
        num = ET.SubElement(h, q("inv", "number"))
        ET.SubElement(num, q("typ", "numberRequested")).text = ""
        ET.SubElement(num, q("typ", "ids")).text = series

    ET.SubElement(h, q("inv", "date")).text = today.isoformat()
    ET.SubElement(h, q("inv", "dateTax")).text = today.isoformat()
    ET.SubElement(h, q("inv", "dateDue")).text = (
        today if is_card else today + dt.timedelta(days=DUE_DAYS)).isoformat()

    # párovací symbol = naše značka, podle ní si doklad najdeme zpátky
    ET.SubElement(h, q("inv", "symPar")).text = inv["ref"]

    if inv.get("company") or inv.get("email"):
        partner = ET.SubElement(h, q("inv", "partnerIdentity"))
        addr = ET.SubElement(partner, q("typ", "address"))
        ET.SubElement(addr, q("typ", "company")).text = inv.get("company") or inv["email"]
        if inv.get("address"):
            parts = [p.strip() for p in inv["address"].split(",")]
            if parts:
                ET.SubElement(addr, q("typ", "street")).text = parts[0]
            if len(parts) > 1:
                ET.SubElement(addr, q("typ", "city")).text = parts[1]
            if len(parts) > 2:
                ET.SubElement(addr, q("typ", "zip")).text = parts[2]
        if country:
            c = ET.SubElement(addr, q("typ", "country"))
            ET.SubElement(c, q("typ", "ids")).text = country
        if inv.get("vat_id"):
            ET.SubElement(addr, q("typ", "vatNumber")).text = inv["vat_id"]
        ET.SubElement(addr, q("typ", "email")).text = inv["email"]

    pt = ET.SubElement(h, q("inv", "paymentType"))
    ET.SubElement(pt, q("typ", "paymentType")).text = "creditcard" if is_card else "draft"

    acc = ET.SubElement(h, q("inv", "account"))
    ET.SubElement(acc, q("typ", "ids")).text = ACCOUNT_STRIPE if is_card else (ACCOUNT_BANK or "")

    ET.SubElement(h, q("inv", "text")).text = inv["description"]
    if reverse_charge:
        ET.SubElement(h, q("inv", "note")).text = (
            "Daň odvede zákazník / Reverse charge")

    # ── položka ──
    detail = ET.SubElement(invoice, q("inv", "invoiceDetail"))
    line = ET.SubElement(detail, q("inv", "invoiceItem"))
    ET.SubElement(line, q("inv", "text")).text = inv["description"]
    ET.SubElement(line, q("inv", "quantity")).text = "1.0"
    ET.SubElement(line, q("inv", "rateVAT")).text = "none" if reverse_charge else "high"

    price = f"{inv['amount_cents'] / 100:.2f}"
    currency = (inv.get("currency") or "eur").upper()
    if currency == "CZK":
        home = ET.SubElement(line, q("inv", "homeCurrency"))
        ET.SubElement(home, q("typ", "unitPrice")).text = price
    else:
        foreign = ET.SubElement(line, q("inv", "foreignCurrency"))
        cur_el = ET.SubElement(foreign, q("typ", "currency"))
        ET.SubElement(cur_el, q("typ", "ids")).text = currency
        ET.SubElement(foreign, q("typ", "unitPrice")).text = price

    # ── souhrn ──
    summary = ET.SubElement(invoice, q("inv", "invoiceSummary"))
    ET.SubElement(summary, q("inv", "roundingDocument")).text = "none"

    return pack


def xml_bytes(root):
    return b'<?xml version="1.0" encoding="Windows-1250"?>\n' + \
        ET.tostring(root, encoding="windows-1250", xml_declaration=False)


# ── komunikace s mServerem ───────────────────────────────────────────
def send_to_pohoda(root):
    r = requests.post(
        MSERVER_URL,
        data=xml_bytes(root),
        headers={"Content-Type": "application/xml; charset=Windows-1250",
                 "STW-Application": "iSupply Scan"},
        auth=(MSERVER_USER, MSERVER_PASS),
        timeout=60,
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def parse_response(resp):
    """Vytáhne stav a přidělené číslo dokladu z odpovědi mServeru."""
    item = resp.find(".//rsp:responsePackItem", NS)
    if item is None:
        return False, None, "mServer nevrátil responsePackItem"

    state = item.get("state")
    if state != "ok":
        note = item.get("note") or ""
        detail = item.find(".//rsp:itemDetail/rsp:detail/rsp:note", NS)
        if detail is not None and detail.text:
            note = f"{note} {detail.text}".strip()
        return False, None, note or f"stav: {state}"

    number = None
    for path in (".//inv:invoiceHeader/inv:number/typ:numberRequested",
                 ".//inv:invoiceHeader/inv:symVar",
                 ".//rsp:producedDetails/rsp:number"):
        el = item.find(path, NS)
        if el is not None and el.text:
            number = el.text.strip()
            break
    return True, number, None


def fetch_number_by_sympar(ref):
    """Když číslo není v odpovědi, dotáhneme ho exportem podle párovacího symbolu."""
    pack = ET.Element(q("dat", "dataPack"), {
        "version": "2.0", "id": f"q-{ref}", "ico": ICO, "application": "iSupply Scan"})
    item = ET.SubElement(pack, q("dat", "dataPackItem"),
                         {"version": "2.0", "id": f"q-{ref}"})
    req = ET.SubElement(item, q("lst", "listInvoiceRequest"),
                        {"version": "2.0", "invoiceType": "issuedInvoice"})
    ET.SubElement(req, q("lst", "requestInvoice"))
    flt = ET.SubElement(req, q("lst", "filter"))
    ET.SubElement(flt, q("ftr", "symPar")).text = ref

    try:
        resp = send_to_pohoda(pack)
        num = resp.find(".//inv:invoiceHeader/inv:number", NS)
        vs = resp.find(".//inv:invoiceHeader/inv:symVar", NS)
        return (num.text.strip() if num is not None and num.text else None,
                vs.text.strip() if vs is not None and vs.text else None)
    except Exception as exc:
        log.warning("dohledání čísla pro %s selhalo: %s", ref, exc)
        return None, None


# ── server ───────────────────────────────────────────────────────────
def api(method, path, **kw):
    r = requests.request(
        method, f"{API_BASE}{path}",
        headers={"X-Agent-Token": AGENT_TOKEN}, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def process(dry_run=False):
    try:
        queue = api("GET", "/api/pohoda/queue")
    except Exception as exc:
        log.error("nedostupný server: %s", exc)
        return

    invoices = queue.get("invoices", [])
    if not invoices:
        return

    log.info("k vystavení: %d dokladů", len(invoices))
    results = []

    for inv in invoices:
        root = build_invoice_xml(inv)

        if dry_run:
            out = HERE / f"dryrun_{inv['ref']}.xml"
            out.write_bytes(xml_bytes(root))
            log.info("[dry-run] %s → %s", inv["ref"], out.name)
            continue

        try:
            ok, number, err = parse_response(send_to_pohoda(root))
            vs = number
            if ok and not number:
                number, vs = fetch_number_by_sympar(inv["ref"])
            if ok:
                log.info("✔ %s → doklad %s", inv["ref"], number or "(číslo nedohledáno)")
                results.append({"id": inv["id"], "ok": True,
                                "number": number, "vs": vs})
            else:
                log.error("✘ %s → %s", inv["ref"], err)
                results.append({"id": inv["id"], "ok": False, "error": err})
        except Exception as exc:
            log.exception("✘ %s → výjimka", inv["ref"])
            results.append({"id": inv["id"], "ok": False, "error": str(exc)})

    if results:
        try:
            api("POST", "/api/pohoda/result", json={"invoices": results})
        except Exception as exc:
            log.error("výsledky se nepodařilo odeslat: %s", exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="jen vygeneruje XML do souborů, nic neodesílá")
    ap.add_argument("--once", action="store_true", help="jeden průchod a konec")
    args = ap.parse_args()

    if not AGENT_TOKEN:
        log.error("Chybí token. Doplň ho do agent.ini nebo do POHODA_TOKEN.")
        sys.exit(1)

    log.info("agent startuje · server %s · mServer %s", API_BASE, MSERVER_URL)

    while True:
        try:
            process(dry_run=args.dry_run)
            if not args.dry_run:
                try:
                    api("POST", "/api/pohoda/heartbeat")
                except Exception:
                    pass
        except KeyboardInterrupt:
            log.info("konec")
            return
        except Exception:
            log.exception("neočekávaná chyba v hlavní smyčce")

        if args.once:
            return
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Spousteni SQL migraci bez psql.

Pouziti v Railway konzoli:
    python migrate.py           # spusti vsechny migrace v poradi
    python migrate.py --check   # jen vypise stav databaze, nic nemeni
    python migrate.py 001_scan_quota.sql   # jen konkretni soubor

Kazdy .sql soubor ma vlastni BEGIN/COMMIT, takze bud projde cely,
nebo se nezapise nic. Opakovane spusteni nevadi - vsechno je psane
jako IF NOT EXISTS.
"""

import os
import sys
import glob

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("CHYBA: chybi promenna DATABASE_URL")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))

OCEKAVANE_TABULKY = [
    "licenses", "activations", "audit_log",
    "credit_packs", "scan_events",
    "devices", "device_components", "device_component_history",
    "pending_invoices", "stripe_payouts",
]


def stav():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public' ORDER BY table_name""")
    existuji = {r[0] for r in cur.fetchall()}

    print("\n--- TABULKY ---")
    for t in OCEKAVANE_TABULKY:
        print(f"  {'OK    ' if t in existuji else 'CHYBI '} {t}")
    navic = existuji - set(OCEKAVANE_TABULKY)
    if navic:
        print("\n  navic v databazi:", ", ".join(sorted(navic)))

    if "licenses" in existuji:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name = 'licenses'""")
        sloupce = {r[0] for r in cur.fetchall()}
        print("\n--- ROZSIRENI TABULKY licenses ---")
        for c in ["scan_limit", "unlimited", "period_start", "period_end",
                  "vat_id", "stripe_customer_id", "stripe_subscription_id"]:
            print(f"  {'OK    ' if c in sloupce else 'CHYBI '} {c}")

        cur.execute("""SELECT license_key, plan, active,
                              coalesce(scan_limit::text, '-') AS scan_limit
                       FROM licenses ORDER BY created_at DESC LIMIT 10""")
        radky = cur.fetchall()
        if radky:
            print("\n--- LICENCE ---")
            for k, plan, active, limit in radky:
                print(f"  {k:24} {plan:12} {'aktivni' if active else 'NEAKTIVNI':10} limit={limit}")

    cur.close()
    conn.close()


def spust(cesta):
    nazev = os.path.basename(cesta)
    print(f"\n>>> {nazev}")
    with open(cesta, encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True          # soubor si BEGIN/COMMIT resi sam
    cur = conn.cursor()
    try:
        cur.execute(sql)
        print(f"    HOTOVO")
        return True
    except Exception as exc:
        print(f"    CHYBA: {exc}")
        return False
    finally:
        cur.close()
        conn.close()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "--check" in sys.argv:
        stav()
        return

    if args:
        soubory = [os.path.join(HERE, a) for a in args]
    else:
        soubory = sorted(glob.glob(os.path.join(HERE, "0*.sql")))

    if not soubory:
        print("Nenasel jsem zadne .sql soubory.")
        sys.exit(1)

    print("Spoustim migrace:")
    for s in soubory:
        print("  -", os.path.basename(s))

    for s in soubory:
        if not os.path.exists(s):
            print(f"\n>>> {os.path.basename(s)}\n    CHYBI SOUBOR")
            sys.exit(1)
        if not spust(s):
            print("\nMigrace se zastavila. Databaze je v puvodnim stavu "
                  "(soubor se zapisuje cely, nebo vubec).")
            sys.exit(1)

    stav()
    print("\nVsechny migrace probehly.")


if __name__ == "__main__":
    main()

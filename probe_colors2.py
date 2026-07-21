#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_colors2.py — READ-ONLY. Vypise BAREVNE KODY vsech pripojenych iPhonu.

K cemu to je:
  Telefon barvu jako text NEVYDA (overeno forenznim dumpem V63 - nikde v
  lockdownu, MobileGestaltu ani IORegistry neni retezec typu "Pink"/"RED").
  Jedine, co je k dispozici, je CISELNY KOD:

      DeviceEnclosureColor / DeviceHousingColor  = barva TELA
      DeviceColor / DeviceCoverGlassColor        = barva PREDNIHO SKLA

  Vyznam kodu se lisi podle generace, takze tabulku kod -> nazev je nutne
  postavit z fyzicky overenych kusu. Presne tak to dela i 3uTools.

  DULEZITE: tyhle klice NEJSOU v ld.all_values - vyda je jen primy dotaz
  ld.get_value(key=...). Proto je bezny vypis vsech hodnot neukaze.

Pouziti:
  1. Pripoj jeden nebo vic iPhonu (odemcene, potvrzeny Trust).
  2. Zavri iSupply Scan, at nekoliduji USB relace.
  3. python probe_colors2.py
  4. U kazdeho radku doplnis barvu, kterou telefon SKUTECNE ma, a zapises
     ji do model_colors.json -> sekce "enclosure_colors".
"""
import asyncio
import inspect
import json
import os
import sys

COLORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "model_colors.json")

BODY_KEYS = ("DeviceEnclosureColor", "DeviceHousingColor")
FRONT_KEYS = ("DeviceCoverGlassColor", "DeviceColor")
INFO_KEYS = ("ProductType", "ModelNumber", "RegionInfo", "SerialNumber")


async def _aw(x):
    return await x if inspect.isawaitable(x) else x


async def read_one(udid):
    from pymobiledevice3.lockdown import create_using_usbmux

    params = inspect.signature(create_using_usbmux).parameters
    if "serial" in params:
        ld = await _aw(create_using_usbmux(serial=udid))
    elif "udid" in params:
        ld = await _aw(create_using_usbmux(udid=udid))
    else:
        ld = await _aw(create_using_usbmux(udid))

    async def key(k):
        try:
            v = await _aw(ld.get_value(key=k))
            return None if v is None else str(v).strip()
        except Exception:
            return None

    out = {"udid": udid}
    for k in INFO_KEYS:
        out[k] = await key(k)
    out["body_code"] = None
    for k in BODY_KEYS:
        v = await key(k)
        if v not in (None, ""):
            out["body_code"], out["body_key"] = v, k
            break
    out["front_code"] = None
    for k in FRONT_KEYS:
        v = await key(k)
        if v not in (None, ""):
            out["front_code"], out["front_key"] = v, k
            break

    try:
        c = ld.close()
        if inspect.isawaitable(c):
            await c
    except Exception:
        pass
    return out


def known_colors():
    try:
        with open(COLORS_FILE, "r", encoding="utf-8") as fh:
            return (json.load(fh).get("enclosure_colors") or {})
    except Exception:
        return {}


async def main():
    from pymobiledevice3.usbmux import list_devices

    devices = await _aw(list_devices())
    if not devices:
        print("Zadne zarizeni pres USB. Pripoj telefon a potvrd Trust.")
        return

    table = known_colors()
    seen, rows = set(), []

    for dev in devices:
        udid = getattr(dev, "serial", None) or getattr(dev, "udid", None)
        if not udid or udid in seen:
            continue
        seen.add(udid)
        try:
            rows.append(await read_one(udid))
        except Exception as exc:
            print(f"[!] {udid}: {type(exc).__name__}: {exc}")

    print("=" * 78)
    print("  BAREVNE KODY PRIPOJENYCH ZARIZENI")
    print("=" * 78)
    todo = []
    for r in rows:
        pt = r.get("ProductType") or "?"
        code = r.get("body_code")
        name = (table.get(pt) or {}).get(code)
        if isinstance(name, dict):
            name = name.get("color")
        print(f"\n  {pt}  ({r.get('ModelNumber')})   SN {r.get('SerialNumber')}")
        print(f"    telo (kod)   : {code!r}   [{r.get('body_key', '-')}]")
        print(f"    celo (kod)   : {r.get('front_code')!r}   [{r.get('front_key', '-')}]")
        if name:
            print(f"    -> v tabulce : {name}")
        else:
            print("    -> V TABULCE CHYBI, doplnit")
            todo.append((pt, code, r.get("ModelNumber")))

    if todo:
        print("\n" + "=" * 78)
        print("  DOPLN DO model_colors.json -> \"enclosure_colors\":")
        print("=" * 78)
        block = {}
        for pt, code, mn in todo:
            block.setdefault(pt, {})[str(code)] = {
                "color": "SEM_NAPIS_BARVU",
                "overeno": f"fyzicky kus, {mn}",
            }
        print(json.dumps(block, ensure_ascii=False, indent=2))
        print("\n  ('SEM_NAPIS_BARVU' nahrad tim, co telefon skutecne je -")
        print("   anglicky, napr. Pink / Midnight / (PRODUCT)RED / Blue.)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_color.py — READ-ONLY. Zjisti, co telefon realne hlasi o barve.

Projde VSECHNA pripojena zarizeni a u kazdeho vypise klice, ze kterych by
se dala barva urcit. Nic nemeni, nic nezapisuje.

Spusteni:  python probe_color.py

POZOR: pokud bezi iSupply Scan, muze USB relace kolidovat (MuxException 183).
Kdyz to spadne, appku zavri a spust skript znovu.
"""
import asyncio
import inspect
import json
import sys

# Klice, ktere u ruznych generaci nesou barvu nebo konfiguraci kusu.
KEYS = (
    "DeviceColor",
    "DeviceEnclosureColor",
    "DeviceEnclosureRGBColor",
    "DeviceRGBColor",
    "ProductType",
    "ModelNumber",
    "RegionInfo",
    "HardwareModel",
    "SalesModel",
    "ArtworkDeviceSubType",
    "ArtworkTraits",
    "SerialNumber",
)


async def _aw(x):
    return await x if inspect.isawaitable(x) else x


async def read_device(udid):
    from pymobiledevice3.lockdown import create_using_usbmux

    params = inspect.signature(create_using_usbmux).parameters
    if "serial" in params:
        ld = await _aw(create_using_usbmux(serial=udid))
    elif "udid" in params:
        ld = await _aw(create_using_usbmux(udid=udid))
    else:
        ld = await _aw(create_using_usbmux(udid))

    vals = await _aw(ld.all_values)
    if not isinstance(vals, dict):
        vals = {}

    out = {k: vals.get(k) for k in KEYS if k in vals}

    # Jeste cokoli, co ma v nazvu "color" - kdyby se to jmenovalo jinak.
    extra = {k: v for k, v in vals.items()
             if "color" in str(k).lower() and k not in out}
    if extra:
        out["_dalsi_klice_s_color"] = extra

    try:
        c = ld.close()
        if inspect.isawaitable(c):
            await c
    except Exception:
        pass
    return out


async def main():
    from pymobiledevice3.usbmux import list_devices

    devices = await _aw(list_devices())
    if not devices:
        print("Zadne zarizeni pres USB. Pripoj telefon a potvrd Trust.")
        return

    seen = set()
    for dev in devices:
        udid = getattr(dev, "serial", None) or getattr(dev, "udid", None)
        if not udid or udid in seen:
            continue
        seen.add(udid)
        print("=" * 70)
        print(f"UDID: {udid}")
        print("=" * 70)
        try:
            data = await read_device(udid)
        except Exception as exc:
            print(f"  [!] {type(exc).__name__}: {exc}")
            continue

        pt = data.get("ProductType", "?")
        print(f"  ProductType: {pt}")
        for k in KEYS:
            if k in data:
                print(f"  {k:26} = {data[k]!r}")
        if "_dalsi_klice_s_color" in data:
            print("  -- dalsi klice obsahujici 'color' --")
            for k, v in data["_dalsi_klice_s_color"].items():
                print(f"  {k:26} = {v!r}")
        print()

    print("=" * 70)
    print("Posli cely vystup + napis, JAKOU BARVU ma kazdy telefon fyzicky.")
    print("Bez toho se kod na barvu nedá spolehlive namapovat.")
    print("=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)

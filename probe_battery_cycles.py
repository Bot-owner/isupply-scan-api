#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_battery_cycles.py  –  READ-ONLY. Najde, KDE přesně leží počet nabíjecích
cyklů baterie na připojeném iPhonu (hlavně kvůli iOS 26, kde CycleCount není
top-level v AppleSmartBattery, ale nejspíš vnořený jinde).

Spuštění (telefon připojený USB, odemčený, "Trust"):
    python probe_battery_cycles.py
    python probe_battery_cycles.py <UDID>

Pošli mi celý výstup – podle skutečného klíče/cesty pak cykly opravím napevno.
"""
import sys, inspect, json


def _maybe(x):
    if inspect.isawaitable(x):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(x)
    return x


def open_ld(udid):
    from pymobiledevice3.lockdown import create_using_usbmux
    params = inspect.signature(create_using_usbmux).parameters
    if udid:
        if "serial" in params: return _maybe(create_using_usbmux(serial=udid))
        if "udid" in params:   return _maybe(create_using_usbmux(udid=udid))
        return _maybe(create_using_usbmux(udid))
    return _maybe(create_using_usbmux())


def walk_find(obj, needles, path="AppleSmartBattery", depth=0, hits=None):
    """Projde celý dict/list strom a najde všechny klíče, co obsahují 'cycle'
    (nebo cokoli z needles), + vypíše cestu a hodnotu."""
    if hits is None:
        hits = []
    if depth > 8:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if any(n in str(k).lower() for n in needles):
                if not isinstance(v, (dict, list)):
                    hits.append((p, v))
            walk_find(v, needles, p, depth + 1, hits)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            walk_find(v, needles, f"{path}[{i}]", depth + 1, hits)
    return hits


def main():
    udid = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 66)
    print("  BATTERY CYCLE COUNT PROBE  (read-only)")
    print("=" * 66)
    try:
        ld = open_ld(udid)
    except Exception as e:
        print(f"[!] Připojení selhalo: {type(e).__name__}: {e}")
        return

    print(f"\nZařízení: {getattr(ld,'udid',None) or getattr(ld,'identifier','?')}")
    print(f"iOS: {ld.get_value(key='ProductVersion')}  |  Model: {ld.get_value(key='ProductType')}")

    # Získej AppleSmartBattery strom přes DiagnosticsService (stejně jako server.py)
    try:
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        diag = DiagnosticsService(ld)
    except Exception as e:
        print(f"[!] DiagnosticsService nedostupné: {type(e).__name__}: {e}")
        return

    trees = {}
    for entry in ["AppleSmartBattery", "AppleARMPMUCharger"]:
        try:
            fn = getattr(diag, "ioregistry_entry", None)
            node = _maybe(fn(entry)) if fn else None
            if isinstance(node, dict) and node:
                trees[entry] = node
                print(f"\n✓ {entry}: {len(node)} klíčů (top-level)")
        except Exception as e:
            print(f"  {entry}: {type(e).__name__}: {e}")

    if not trees:
        print("\n[!] Žádný battery uzel se nepodařilo přečíst.")
        return

    needles = ["cycle", "count"]
    print("\n" + "-" * 66)
    print("NALEZENÉ klíče obsahující 'cycle' / 'count' (cesta = hodnota):")
    print("-" * 66)
    found_any = False
    for name, tree in trees.items():
        for path, val in walk_find(tree, needles, name):
            print(f"  {path} = {val}")
            found_any = True
    if not found_any:
        print("  (nic – cykly nejspíš přes tenhle uzel iOS 26 nevydá)")

    # Vypiš i top-level klíče AppleSmartBattery, ať vidíme strukturu
    print("\n" + "-" * 66)
    print("TOP-LEVEL klíče AppleSmartBattery (pro přehled struktury):")
    print("-" * 66)
    asb = trees.get("AppleSmartBattery", {})
    for k, v in asb.items():
        t = type(v).__name__
        preview = (str(v)[:60] + "…") if not isinstance(v, (dict, list)) else f"<{t}, {len(v)} pol.>"
        print(f"  {k} ({t}): {preview}")

    print("\n" + "=" * 66)
    print("Pošli mi celý tenhle výstup – podle reálné cesty cykly opravím napevno.")
    print("=" * 66)


if __name__ == "__main__":
    main()

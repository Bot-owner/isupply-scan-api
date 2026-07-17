#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_features.py  –  READ-ONLY. Vytáhne reálné hodnoty pro nové features:
  • Barva (DeviceEnclosureColor / DeviceColor) – ať mapujeme Pink/Black/Starlight správně
  • Sales region (RegionInfo)
  • Wi-Fi SN (sériové číslo Wi-Fi modulu – ne MAC)
  • Activation Lock / iCloud ON/OFF (lokálně přes lockdown)
  • Proximity / front-flex sériák (JasperSNUM apod.)

Spuštění:  python probe_features.py   (iPhone připojený, odemčený, Trust)
Spusť na iPhone 13 mini (Starlight) i iPhone 15 (Pink) a pošli mi celý výstup.
"""
import sys, inspect, asyncio


async def _aw(x):
    return await x if inspect.isawaitable(x) else x


async def open_ld(udid):
    from pymobiledevice3.lockdown import create_using_usbmux
    p = inspect.signature(create_using_usbmux).parameters
    if udid:
        if "serial" in p: return await _aw(create_using_usbmux(serial=udid))
        if "udid" in p:   return await _aw(create_using_usbmux(udid=udid))
        return await _aw(create_using_usbmux(udid))
    return await _aw(create_using_usbmux())


def fmt(v):
    if isinstance(v, (bytes, bytearray)):
        return "<%dB> %s" % (len(v), bytes(v).hex())
    return repr(v)


def walk(obj, needles, path, depth=0, hits=None):
    if hits is None: hits = []
    if depth > 8: return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            pth = f"{path}.{k}"
            if any(n in str(k).lower() for n in needles) and not isinstance(v, (dict, list)):
                hits.append((pth, v))
            walk(v, needles, pth, depth + 1, hits)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            walk(v, needles, f"{path}[{i}]", depth + 1, hits)
    return hits


async def run(udid):
    print("=" * 70)
    print("  FEATURES PROBE  (read-only): barva / region / wifi-sn / iCloud / prox")
    print("=" * 70)
    try:
        ld = await open_ld(udid)
    except Exception as e:
        print(f"[!] Připojení selhalo: {type(e).__name__}: {e}"); return
    try:
        print(f"\niOS: {await _aw(ld.get_value(key='ProductVersion'))} | Model: {await _aw(ld.get_value(key='ProductType'))}")
    except Exception:
        pass

    # 1) Cílené lockdown klíče
    print("\n" + "-" * 70)
    print("1) LOCKDOWN – cílené klíče")
    print("-" * 70)
    keys = [
        # barva
        "DeviceColor", "DeviceEnclosureColor",
        # region
        "RegionInfo", "RegionalInfo", "SalesData", "ModelNumber", "RegionCode",
        # wifi / bluetooth SN
        "WiFiAddress", "WifiVendor", "WiFiSerialNumber", "WifiSerialNumber",
        "BluetoothAddress", "BluetoothSerialNumber",
        # activation / iCloud lock
        "ActivationState", "ActivationStateAcknowledged", "BrickState",
        "FMiPAccountExists", "FMiPStatus", "FindMyEnabled",
    ]
    for k in keys:
        try:
            v = await _aw(ld.get_value(key=k))
        except Exception as e:
            v = f"<chyba {type(e).__name__}>"
        print(f"  {k}: {fmt(v)}" if v not in (None, "", {}, []) else f"  {k}: —")

    # 2) all_values – vyfiltruj barvu/region/wifi/serial/activation/lock/fmip
    print("\n" + "-" * 70)
    print("2) all_values – klíče s color/region/wifi/serial/activation/lock/fmip/sim")
    print("-" * 70)
    try:
        av = await _aw(ld.all_values)
        av = av if isinstance(av, dict) else {}
    except Exception as e:
        av = {}
        print(f"  [!] all_values: {type(e).__name__}: {e}")
    needles = ["color", "region", "wifi", "serial", "activation", "lock", "fmip", "sim", "carrier"]
    hits = [(k, v) for k, v in av.items() if any(n in k.lower() for n in needles)]
    for k, v in hits:
        print(f"  {k}: {fmt(v)}")

    # 3) domény, kde bývá aktivace / FMiP
    print("\n" + "-" * 70)
    print("3) domény (activation / fmip / mobile)")
    print("-" * 70)
    for dom in ("com.apple.fmip", "com.apple.mobile.activation_state",
                "com.apple.mobile.chaperone", "com.apple.mobile.iTunes"):
        try:
            v = await _aw(ld.get_value(domain=dom))
            print(f"  [{dom}] -> {fmt(v) if not isinstance(v, dict) else list(v.keys())}")
        except Exception as e:
            print(f"  [{dom}] chyba: {type(e).__name__}: {e}")

    # 4) IORegistry – WiFi SN + proximity/front-flex (JasperSNUM apod.)
    print("\n" + "-" * 70)
    print("4) IORegistry – WiFi SN + proximity/front-flex")
    print("-" * 70)
    try:
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        diag = DiagnosticsService(ld)

        async def rd(target):
            for kw in ({"name": target}, {"plane": "IOService", "ioclass": target},
                       {"plane": "IOService", "name": target}):
                try:
                    r = await _aw(diag.ioregistry(**kw))
                    if r: return r
                except Exception:
                    pass
            return None

        for node in ("AppleARMWiFi", "AppleBCMWLANCore", "AppleH16CamIn",
                     "AppleH15CamIn", "AppleH14CamIn", "AppleH13CamIn"):
            tree = await rd(node)
            if not tree:
                print(f"  {node}: —"); continue
            found = walk(tree, ["serial", "snum", "jasper", "moduleserial", "flex"], node)
            print(f"  {node}: {'; '.join(f'{p}={fmt(v)}' for p,v in found[:8]) or '(nic zajímavého)'}")
    except Exception as e:
        print(f"  IORegistry chyba: {type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print("Pošli mi celý výstup (z 13 mini i 15) – podle něj napojím barvu, region,")
    print("Wi-Fi SN, iCloud ON/OFF i proximity flex napevno.")
    print("=" * 70)


def main():
    udid = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(udid))


if __name__ == "__main__":
    main()

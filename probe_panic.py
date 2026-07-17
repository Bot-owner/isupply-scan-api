#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_panic.py  –  READ-ONLY. Zjistí, jak z iPhonu vytáhnout crash/panic logy
a jak vypadá panic .ips (kvůli tomu, aby backend endpoint sedl na tvou verzi
pymobiledevice3 na první dobrou).

Spuštění:  python probe_panic.py   (iPhone připojený, odemčený, Trust)
Pošli mi celý výstup – hlavně sekci "PANIC SOUBORY" a ukázku .ips.
"""
import sys, inspect, asyncio


async def _await(x):
    return await x if inspect.isawaitable(x) else x


async def open_ld(udid):
    from pymobiledevice3.lockdown import create_using_usbmux
    params = inspect.signature(create_using_usbmux).parameters
    if udid:
        if "serial" in params: return await _await(create_using_usbmux(serial=udid))
        if "udid" in params:   return await _await(create_using_usbmux(udid=udid))
        return await _await(create_using_usbmux(udid))
    return await _await(create_using_usbmux())


async def run(udid):
    print("=" * 66)
    print("  PANIC / CRASH LOGS PROBE  (read-only)")
    print("=" * 66)
    try:
        ld = await open_ld(udid)
    except Exception as e:
        print(f"[!] Připojení selhalo: {type(e).__name__}: {e}"); return

    try:
        print(f"\niOS: {await _await(ld.get_value(key='ProductVersion'))}  |  Model: {await _await(ld.get_value(key='ProductType'))}")
    except Exception:
        pass

    # 1) Zjisti, co CrashReportsManager na téhle verzi umí
    print("\n" + "-" * 66)
    print("1) CrashReportsManager – dostupné metody")
    print("-" * 66)
    mgr = None
    try:
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        mgr = (await CrashReportsManager(ld)) if inspect.iscoroutinefunction(CrashReportsManager) else CrashReportsManager(ld)
        methods = [m for m in dir(mgr) if not m.startswith("_")]
        print("  metody:", methods)
    except Exception as e:
        print(f"  [!] CrashReportsManager nedostupný: {type(e).__name__}: {e}")

    # 2) Vypiš seznam crash souborů (zkusíme víc metod)
    print("\n" + "-" * 66)
    print("2) SEZNAM crash souborů (root)")
    print("-" * 66)
    listing = None
    if mgr is not None:
        for meth in ("ls", "list", "get_new_sysdiagnose", "iter_files"):
            fn = getattr(mgr, meth, None)
            if not fn:
                continue
            try:
                res = await _await(fn())
                if res:
                    listing = list(res) if not isinstance(res, list) else res
                    print(f"  ✓ přes {meth}(): {len(listing)} položek")
                    break
            except Exception as e:
                print(f"  {meth}(): {type(e).__name__}: {e}")
    # fallback: přímý AFC přes afc_service
    if listing is None and mgr is not None:
        afc = getattr(mgr, "afc", None) or getattr(mgr, "afc_service", None)
        if afc is not None:
            for meth in ("listdir", "ls"):
                fn = getattr(afc, meth, None)
                if fn:
                    try:
                        listing = await _await(fn("/"))
                        print(f"  ✓ přes afc.{meth}('/'): {len(listing)} položek")
                        break
                    except Exception as e:
                        print(f"  afc.{meth}(): {type(e).__name__}: {e}")

    if not listing:
        print("  (nic – crash logy se přes tuhle verzi takhle nedostávají)")
    else:
        for name in listing[:40]:
            print("   ", name)
        if len(listing) > 40:
            print(f"    … a dalších {len(listing)-40}")

    # 3) Najdi panic soubory a vypiš ukázku obsahu
    print("\n" + "-" * 66)
    print("3) PANIC SOUBORY + ukázka .ips")
    print("-" * 66)
    panic_names = [n for n in (listing or []) if "panic" in str(n).lower()]
    if not panic_names:
        # zkus i .ips obecně
        panic_names = [n for n in (listing or []) if str(n).lower().endswith(".ips")][:5]
    if not panic_names:
        print("  Žádný panic/.ips soubor v seznamu (buď je telefon zdravý, nebo jsou jinde).")
    else:
        print(f"  Nalezeno panic/.ips: {panic_names}")
        sample = panic_names[0]
        print(f"\n  --- OBSAH: {sample} (prvních ~2500 znaků) ---")
        content = None
        for meth in ("get_crash", "read", "cat", "pull"):
            fn = getattr(mgr, meth, None)
            if fn:
                try:
                    content = await _await(fn(sample))
                    if content:
                        break
                except Exception as e:
                    print(f"  {meth}('{sample}'): {type(e).__name__}: {e}")
        if content is None and mgr is not None:
            afc = getattr(mgr, "afc", None) or getattr(mgr, "afc_service", None)
            if afc is not None:
                fn = getattr(afc, "get_file_contents", None) or getattr(afc, "cat", None)
                if fn:
                    try:
                        content = await _await(fn(sample))
                    except Exception as e:
                        print(f"  afc get: {type(e).__name__}: {e}")
        if content:
            if isinstance(content, (bytes, bytearray)):
                content = bytes(content).decode("utf-8", errors="ignore")
            print(content[:2500])
        else:
            print("  (obsah se nepodařilo přečíst – pošli mi aspoň názvy souborů výše)")

    print("\n" + "=" * 66)
    print("Pošli mi celý výstup – podle metod + formátu .ips postavím endpoint napevno.")
    print("=" * 66)


def main():
    udid = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(udid))


if __name__ == "__main__":
    main()

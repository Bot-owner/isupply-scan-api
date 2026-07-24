"""
iSupply Scan — spousteni aplikace v jednom okne. Windows i macOS.

  · Flask bezi na pozadi ve vlakne tehoz procesu
  · UI se otevre v nativnim okne pres pywebview
        - Windows: WebView2 (soucast Windows 10/11)
        - macOS:   WKWebView (soucast systemu, nic doinstalovat netreba)
  · zadna konzole, zadny prohlizec, jedno okno

Vystup, ktery drive chodil do konzole, se zapisuje do logu vedle aplikace
(Windows) resp. do ~/Library/Logs/ (macOS).

Build:
  Windows: PyInstaller --noconsole, vstupni bod launcher.py  (BUILD_WINDOWS.bat)
  macOS:   build_macos.sh + NASAZENI_MACOS.md
"""

import os
import sys
import threading
import time

APP_NAME = "iSupply Scan"
# Znacka verze - vypise se do logu i do konzole. Diky ni je hned videt,
# jestli EXE obsahuje aktualni launcher, nebo jestli build vzal stary soubor.
LAUNCHER_VERSION = "2026-07-23 · aktivace-pri-prvnim-spusteni"
PORT = 5000
URL = f"http://127.0.0.1:{PORT}"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

# ctypes.windll existuje jen na Windows - na Macu by import padl.
if IS_WIN:
    import ctypes


def _base_dir():
    """Slozka, odkud aplikace bezi. U .app bundlu je sys.executable uvnitr
    Contents/MacOS/. POZOR: bundle je po notarizaci READ-ONLY - zapisovatelna
    data resi server.py pres slozku v Application Support, ne tady."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    """Cesta k pribalenemu souboru. PyInstaller: _MEIPASS; .app: Resources/."""
    roots = [getattr(sys, "_MEIPASS", None), _base_dir()]
    if IS_MAC and getattr(sys, "frozen", False):
        roots.append(os.path.abspath(os.path.join(_base_dir(), "..", "Resources")))
    for root in roots:
        if not root:
            continue
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    return None


# ─── 1) Log do souboru ───────────────────────────────────────────────
# Bez konzole je sys.stdout None a kazdy print by shodil aplikaci.
# Musi probehnout PRED importem server.py.
def _log_path():
    if IS_MAC:
        logdir = os.path.expanduser("~/Library/Logs/iSupply Scan")
        try:
            os.makedirs(logdir, exist_ok=True)
            return os.path.join(logdir, "isupply-scan.log")
        except Exception:
            pass
    return os.path.join(_base_dir(), "isupply-scan.log")


LOG_PATH = _log_path()


class _Tee:
    def __init__(self, stream, path):
        self.stream = stream
        try:
            self.file = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self.file = None

    def write(self, data):
        for t in (self.stream, self.file):
            try:
                if t:
                    t.write(data)
            except Exception:
                pass

    def flush(self):
        for t in (self.stream, self.file):
            try:
                if t:
                    t.flush()
            except Exception:
                pass


sys.stdout = _Tee(sys.stdout, LOG_PATH)
sys.stderr = _Tee(sys.stderr, LOG_PATH)


# ─── 2) Dialog — nativni na obou systemech ───────────────────────────
def message_box(text, title="iSupply Scan", style=0x40):
    """0x10 chyba, 0x30 varovani, 0x40 info (Windows). macOS -> osascript."""
    if IS_WIN:
        try:
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), style)
            return
        except Exception:
            pass
    elif IS_MAC:
        try:
            import subprocess
            icon = "stop" if style == 0x10 else ("caution" if style == 0x30 else "note")
            safe = str(text).replace('"', '\\"')
            safe_t = str(title).replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                f'display dialog "{safe}" with title "{safe_t}" '
                f'buttons {{"OK"}} default button "OK" with icon {icon}'],
                check=False)
            return
        except Exception:
            pass
    print(f"[dialog] {title}: {text}")


# ─── 3) Server na pozadi ─────────────────────────────────────────────
# Oficialni instalator WebView2 od Microsoftu (trvaly odkaz).
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_version():
    """Verze nainstalovaneho WebView2 Runtime, nebo None.

    Okno aplikace vykresluje WebView2 - tedy zabudovany Microsoft Edge.
    Kdyz na stroji chybi, okno se otevre PRAZDNE (bila obrazovka) a nic
    nenapovi, co se deje. Zaznamenano na notebooku HP, kde runtime nebyl
    nikdy doinstalovany. Proto to overujeme drive, nez okno otevreme.
    """
    if not IS_WIN:
        return "n/a"
    try:
        import winreg
    except ImportError:
        return None
    # Pozor: GUID obsahuje slozene zavorky, takze zadny .format() ani f-string -
    # bralo by je jako zastupne symboly. Skladame prostym spojenim.
    konec = "Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_GUID
    mista = [
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + konec),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + konec),
        (winreg.HKEY_CURRENT_USER,  "SOFTWARE\\" + konec),
    ]
    for root, cesta in mista:
        try:
            with winreg.OpenKey(root, cesta) as key:
                verze, _ = winreg.QueryValueEx(key, "pv")
                if verze and verze != "0.0.0.0":
                    return verze
        except OSError:
            continue
    return None


def handle_missing_webview2():
    """Runtime chybi. NEBLOKUJEME praci - UI otevreme v prohlizeci, aby technik
    mohl testovat hned - a zaroven nabidneme doinstalovani."""
    print("[launcher] WebView2 Runtime NENALEZEN -> nahradni rezim v prohlizeci")
    import webbrowser
    webbrowser.open(URL)
    message_box(
        "Na tomto počítači chybí komponenta Microsoft WebView2.\n\n"
        "Bez ní se okno aplikace nezobrazí (zůstane bílé).\n\n"
        "Aplikace se proto otevřela ve vašem prohlížeči a můžete\n"
        "rovnou pracovat.\n\n"
        "Trvalé řešení: doinstalujte WebView2 (zdarma, od Microsoftu).\n"
        "Po kliknutí na OK se otevře stránka se stažením.\n\n"
        "Po instalaci aplikaci restartujte — otevře se už ve vlastním okně.",
        "Chybí Microsoft WebView2", 0x30)
    try:
        webbrowser.open(WEBVIEW2_URL)
    except Exception as exc:
        print(f"[launcher] stranku se stazenim nelze otevrit: {exc}")


def check_port_free(port=PORT):
    """Neposlouchá už nekdo na nasem portu? Kdyz ano, prohlizec by se pripojil
    k CIZI aplikaci a okno by zustalo bile nebo ukazalo neco jineho."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def start_server(server):
    def _run():
        try:
            server.app.run(host="127.0.0.1", port=PORT,
                           debug=False, threaded=True, use_reloader=False)
        except Exception as exc:
            print(f"[launcher] server spadl: {exc}")

    threading.Thread(target=_run, daemon=True).start()

    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(URL + "/api/licence-status", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


# ─── 4) Ukonceni ─────────────────────────────────────────────────────
_shutdown_done = threading.Event()


def shutdown():
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    print("[launcher] ukoncuji…")
    try:
        import scan_quota
        scan_quota.send_offline()
    except Exception as exc:
        print(f"[launcher] offline hlaseni selhalo: {exc}")
    sys.stdout.flush()


# ─── 5) Ikona okna (jen Windows) ─────────────────────────────────────
def apply_window_icon():
    if not IS_WIN:
        return
    ico = _resource("icon.ico")
    if not ico:
        print("[launcher] icon.ico nenalezen, ikonu nenastavuji")
        return
    try:
        u32 = ctypes.windll.user32
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "iSupply.Scan.Desktop")
        except Exception:
            pass
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x00000010, 0x00000040
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        hicon_big = u32.LoadImageW(None, ico, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
        hicon_small = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if not hicon_big:
            hicon_big = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon_big:
            return
        for _ in range(40):
            hwnd = u32.FindWindowW(None, APP_NAME)
            if hwnd:
                u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small or hicon_big)
                print("[launcher] ikona okna nastavena")
                return
            time.sleep(0.25)
    except Exception as exc:
        print(f"[launcher] ikonu se nepodarilo nastavit: {exc}")


# ─── 6) Hlavni beh ───────────────────────────────────────────────────
def main():
    os.chdir(_base_dir())
    print(f"[launcher] verze={LAUNCHER_VERSION}")
    print(f"[launcher] start · platforma={sys.platform} · frozen={getattr(sys,'frozen',False)}")

    try:
        import server
    except Exception as exc:
        message_box(f"Aplikaci se nepodařilo spustit.\n\n{exc}\n\n"
                    f"Podrobnosti v logu:\n{LOG_PATH}", "Chyba při startu", 0x10)
        sys.exit(1)

    # ── Licence ──────────────────────────────────────────────────────
    # Rozlisujeme DVE ruzne situace, ktere driv koncily stejne (chybovou
    # hlaskou a ukoncenim):
    #
    #   a) Licence jeste NEBYLA zadana - typicky prvni spusteni u zakaznika,
    #      ktery si stahl EXE z webu. Tady se NESMI koncit: appka ma
    #      nastartovat a ukazat aktivacni okno, kde si zakaznik zalozi ucet
    #      a zada klic z e-mailu. Driv dostal hlasku "Vytvorte soubor
    #      licence.key", coz je slepa ulicka - obycejny zakaznik nema jak.
    #
    #   b) Licence zadana JE, ale server ji odmitl (vyprsela, byla zrusena,
    #      je na jinem pocitaci). Tady konec smysl dava.
    lic_ok, lic_msg = server.check_license()
    print(f"  Licence: {lic_msg}")
    if not lic_ok:
        try:
            ma_klic = server.load_license_key() is not None
        except Exception:
            ma_klic = False

        if ma_klic:
            message_box(
                f"{lic_msg}\n\nCo s tím:\n"
                "  1. Ověř, že klíč sedí s tím z e-mailu po nákupu\n"
                "  2. Zkontroluj připojení k internetu\n"
                "  3. Licence může být aktivní na jiném počítači\n\n"
                "Podpora: info@isupply.cz",
                "Licence není platná", 0x10)
            sys.exit(1)

        print("  Licence zatim neni - spoustim aktivacni okno.")

    # Windows potrebuje Apple Mobile Device Support; macOS ma usbmuxd v systemu.
    if IS_WIN:
        try:
            server._check_apple_driver()
        except Exception as exc:
            print(f"[launcher] kontrola ovladace: {exc}")
    else:
        print("[launcher] macOS: usbmuxd je vestaveny, ovladac se neresi")

    if not check_port_free():
        message_box(
            f"Na portu {PORT} už něco poslouchá.\n\n"
            "Nejspíš už jedna instance iSupply Scan běží, nebo port zabral\n"
            "jiný program. Zavřete druhou instanci a zkuste to znovu.\n\n"
            "Kdyby problém trval, restartujte počítač.",
            "Port je obsazený", 0x30)

    server.init_db()
    threading.Thread(target=server.usb_monitor_thread, daemon=True).start()

    if not start_server(server):
        message_box(f"Server se nepodařilo nastartovat na portu {PORT}.\n\n"
                    "Nejspíš už jedna instance běží, nebo port blokuje jiný program.",
                    "Port je obsazený", 0x10)
        sys.exit(1)

    print(f"  UI: {URL}")

    # ── Nouzovy rezim: otevrit v prohlizeci misto vlastniho okna ──────
    # Na nekterych strojich (zaznamenano na HP) zustane okno WebView2 bile:
    # server bezi, ale obsah se do nej nedostane. Byva to zastaraly nebo
    # poskozeny WebView2, ovladac grafiky, nebo firemni proxy bez vyjimky
    # pro localhost. Aby technik nezustal stat, staci vedle aplikace vytvorit
    # prazdny soubor PROHLIZEC.txt - UI se pak otevre v defaultnim prohlizeci.
    if os.path.exists(os.path.join(_base_dir(), "PROHLIZEC.txt")):
        print("[launcher] nalezen PROHLIZEC.txt -> otviram UI v prohlizeci")
        import webbrowser
        webbrowser.open(URL)
        message_box(
            "Aplikace běží v prohlížeči.\n\n"
            f"Adresa: {URL}\n\n"
            "Toto okno nechte otevřené — po jeho zavření se aplikace ukončí.",
            "iSupply Scan — režim prohlížeče", 0x40)
        shutdown()
        return

    # Bila obrazovka byva casto o hardwarovou akceleraci. Vypnout ji jde
    # promennou prostredi, kterou WebView2 cte pri startu; nastavujeme ji
    # jen kdyz si o to nekdo rekne souborem BEZ_GPU.txt, protoze jinak by
    # to zbytecne zpomalilo vsechny ostatni stroje.
    if os.path.exists(os.path.join(_base_dir(), "BEZ_GPU.txt")):
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            "--disable-gpu --disable-software-rasterizer")
        print("[launcher] nalezen BEZ_GPU.txt -> WebView2 bez hardwarove akcelerace")

    # ── WebView2 ─────────────────────────────────────────────────────
    # Okno vykresluje WebView2 (zabudovany Edge). Kdyz chybi, okno by se
    # otevrelo PRAZDNE a nic by neporadilo, co delat - presne to se stalo na
    # notebooku HP. Kontrolujeme proto DRIV, nez okno zkusime otevrit, a misto
    # bile plochy nabidneme reseni + rovnou spustime UI v prohlizeci.
    if IS_WIN:
        _wv = webview2_version()
        if _wv:
            print(f"[launcher] WebView2 Runtime {_wv}")
        else:
            handle_missing_webview2()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            shutdown()
            return

    try:
        import webview
    except ImportError:
        print("[launcher] pywebview neni k dispozici, otviram prohlizec")
        import webbrowser
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        shutdown()
        return

    # Okno se otevira co nejvetsi. Pri 1480x940 se karty slotu nevesly a
    # technik v nich musel scrollovat, coz je u nastroje, kde ma vsech deset
    # slotu videt najednou, k nicemu.
    sirka, vyska = 1760, 1060
    try:
        import ctypes as _c
        _u = _c.windll.user32 if IS_WIN else None
        if _u:
            _u.SetProcessDPIAware()
            sirka = max(1280, min(int(_u.GetSystemMetrics(0) * 0.94), 2400))
            vyska = max(800, min(int(_u.GetSystemMetrics(1) * 0.92), 1500))
    except Exception:
        pass

    window = webview.create_window(
        APP_NAME, URL, width=sirka, height=vyska,
        min_size=(1200, 780), text_select=True)
    try:
        window.events.closed += shutdown
    except Exception:
        pass

    if IS_WIN:
        threading.Thread(target=apply_window_icon, daemon=True).start()

    try:
        webview.start()
    except Exception as exc:
        print(f"[launcher] okno se nepodarilo otevrit: {exc}")
        hint = ("Na tomto počítači nejspíš chybí WebView2 Runtime.\n"
                "Stáhni ho zdarma na:\ndeveloper.microsoft.com/microsoft-edge/webview2\n\n"
                if IS_WIN else "Okno se nepodařilo otevřít.\n\n")
        message_box("Nepodařilo se otevřít okno aplikace.\n\n" + hint +
                    "Aplikace se zatím otevře v prohlížeči.", "Chyba okna", 0x30)
        import webbrowser
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()

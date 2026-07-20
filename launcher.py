"""
iSupply Scan — spousteni aplikace v jednom okne.

Nahrazuje dosavadni rezim "cerne okno CMD + prohlizec":
  · Flask bezi na pozadi ve vlakne tehoz procesu
  · UI se otevre v nativnim okne pres WebView2 (soucast Windows 10/11)
  · zadna konzole, zadny prohlizec, jedna polozka na hlavnim panelu

Vystup, ktery drive chodil do konzole, se zapisuje do souboru
`isupply-scan.log` vedle aplikace.

Build: PyInstaller s prepinacem --noconsole a vstupnim bodem launcher.py
"""

import ctypes
import os
import sys
import threading
import time

APP_NAME = "iSupply Scan"
PORT = 5000
URL = f"http://127.0.0.1:{PORT}"


def _resource(name):
    """Cesta k pribalenemu souboru — v EXE lezi v docasnem _MEIPASS."""
    for root in (getattr(sys, "_MEIPASS", None), _base_dir()):
        if not root:
            continue
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    return None


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────────────────────────────────────────────────
# 1) Log do souboru
#    Bez konzole je sys.stdout None a kazdy print by shodil aplikaci.
#    Presmerovani MUSI probehnout jeste pred importem server.py.
# ─────────────────────────────────────────────────────────────────────
LOG_PATH = os.path.join(_base_dir(), "isupply-scan.log")


class _Tee:
    """Zapisuje do souboru a zaroven do konzole, kdyz nejaka je."""

    def __init__(self, stream, path):
        self.stream = stream
        self.file = None
        try:
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.remove(path)          # log nesmi rust donekonecna
            self.file = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            pass

    def write(self, data):
        if self.file:
            try:
                self.file.write(data)
            except Exception:
                pass
        if self.stream:
            try:
                self.stream.write(data)
            except Exception:
                pass

    def flush(self):
        for t in (self.file, self.stream):
            if t:
                try:
                    t.flush()
                except Exception:
                    pass

    def isatty(self):
        return False


sys.stdout = _Tee(sys.stdout, LOG_PATH)
sys.stderr = _Tee(sys.stderr, LOG_PATH)

print("\n" + "=" * 60)
print(f"  {APP_NAME} — start {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)


# ─────────────────────────────────────────────────────────────────────
# 2) Dialogy, protoze konzole uz neexistuje
# ─────────────────────────────────────────────────────────────────────
def message_box(text, title=APP_NAME, style=0x40):
    """0x10 = chyba, 0x30 = varovani, 0x40 = informace."""
    try:
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), style)
    except Exception:
        print(f"[dialog] {title}: {text}")


# ─────────────────────────────────────────────────────────────────────
# 3) Server na pozadi
# ─────────────────────────────────────────────────────────────────────
def start_server(server):
    def _run():
        try:
            server.app.run(host="127.0.0.1", port=PORT,
                           debug=False, threaded=True, use_reloader=False)
        except Exception as exc:
            print(f"[launcher] server spadl: {exc}")

    threading.Thread(target=_run, daemon=True).start()

    import urllib.request
    for _ in range(60):                     # az 15 s na nastartovani
        try:
            urllib.request.urlopen(URL + "/api/licence-status", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


# ─────────────────────────────────────────────────────────────────────
# 4) Ukonceni — nahlasit serveru, ze uz nejsme online
# ─────────────────────────────────────────────────────────────────────
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




# ─────────────────────────────────────────────────────────────────────
# 6) Ikona okna
#    V EXE ji nastavi uz PyInstaller (--icon), ale pri spusteni
#    ze zdrojaku by okno melo ikonu Pythonu. Tohle to srovna v obou
#    pripadech a zaroven doplni ikonu na hlavni panel.
# ─────────────────────────────────────────────────────────────────────
def apply_window_icon():
    ico = _resource("icon.ico")
    if not ico:
        print("[launcher] icon.ico nenalezen, ikonu nenastavuji")
        return
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

        # Vlastni AppUserModelID -> Windows nesloucí okno s Pythonem
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "iSupply.Scan.Desktop")
        except Exception:
            pass

        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x00000010, 0x00000040
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

        hicon_big = u32.LoadImageW(None, ico, IMAGE_ICON, 256, 256,
                                   LR_LOADFROMFILE)
        hicon_small = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32,
                                     LR_LOADFROMFILE)
        if not hicon_big:
            hicon_big = u32.LoadImageW(None, ico, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon_big:
            return

        # Okno vznika az po startu webview, proto par pokusu
        for _ in range(40):
            hwnd = u32.FindWindowW(None, APP_NAME)
            if hwnd:
                u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL,
                                 hicon_small or hicon_big)
                print("[launcher] ikona okna nastavena")
                return
            time.sleep(0.25)
        print("[launcher] okno pro nastaveni ikony nenalezeno")
    except Exception as exc:
        print(f"[launcher] ikonu se nepodarilo nastavit: {exc}")


# ─────────────────────────────────────────────────────────────────────
# 7) Hlavni beh
# ─────────────────────────────────────────────────────────────────────
def main():
    os.chdir(_base_dir())

    try:
        import server
    except Exception as exc:
        message_box(f"Aplikaci se nepodařilo spustit.\n\n{exc}\n\n"
                    f"Podrobnosti najdeš v souboru:\n{LOG_PATH}",
                    "Chyba při startu", 0x10)
        sys.exit(1)

    # ── Licence ──
    lic_ok, lic_msg = server.check_license()
    print(f"  Licence: {lic_msg}")
    if not lic_ok:
        message_box(
            f"{lic_msg}\n\n"
            "Co s tím:\n"
            "  1. Zkontroluj soubor 'licence.key' vedle aplikace\n"
            "  2. Ověř, že klíč sedí s tím z e-mailu po nákupu\n"
            "  3. Zkontroluj připojení k internetu\n\n"
            "Podpora: info@isupply.cz",
            "Licence není platná", 0x10)
        sys.exit(1)

    # ── Start sluzeb ──
    try:
        server._check_apple_driver()
    except Exception as exc:
        print(f"[launcher] kontrola ovladace: {exc}")

    server.init_db()
    threading.Thread(target=server.usb_monitor_thread, daemon=True).start()

    if not start_server(server):
        message_box(f"Server se nepodařilo nastartovat na portu {PORT}.\n\n"
                    "Nejspíš už jedna instance aplikace běží,\n"
                    "nebo port blokuje jiný program.",
                    "Port je obsazený", 0x10)
        sys.exit(1)

    print(f"  UI: {URL}")

    # ── Okno ──
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

    window = webview.create_window(
        APP_NAME, URL,
        width=1480, height=940,
        min_size=(1100, 700),
        text_select=True,
    )
    try:
        window.events.closed += shutdown
    except Exception:
        pass

    threading.Thread(target=apply_window_icon, daemon=True).start()

    try:
        # Databaze naskenovanych telefonu zije v localStorage. Vychozi
        # private_mode=True v pywebview uloziste po zavreni okna ZAHODI,
        # proto persistentni slozka v LOCALAPPDATA (prezije i presun EXE).
        _storage = os.path.join(
            os.environ.get('LOCALAPPDATA') or _base_dir(),
            'iSupply Scan', 'webview_data')
        try:
            os.makedirs(_storage, exist_ok=True)
        except Exception:
            _storage = None
        try:
            if _storage:
                webview.start(private_mode=False, storage_path=_storage)
            else:
                webview.start(private_mode=False)
        except TypeError:
            # starsi pywebview tyto parametry nezna
            webview.start()
    except Exception as exc:
        print(f"[launcher] okno se nepodarilo otevrit: {exc}")
        message_box(
            "Nepodařilo se otevřít okno aplikace.\n\n"
            "Na tomto počítači nejspíš chybí WebView2 Runtime.\n"
            "Stáhni ho zdarma na:\n"
            "developer.microsoft.com/microsoft-edge/webview2\n\n"
            "Aplikace se zatím otevře v prohlížeči.",
            "Chybí WebView2", 0x30)
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

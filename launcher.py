"""
iSupply Scan – Launcher
"""
import sys
import os
import threading
import time
import webbrowser
import subprocess

def get_base():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def open_browser():
    time.sleep(3)
    webbrowser.open('http://localhost:5000')

def main():
    base = get_base()
    os.chdir(base)

    server = os.path.join(base, 'server.py')
    if not os.path.exists(server):
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            f'server.py not found in:\n{base}',
            'iSupply Scan – Error',
            0x10
        )
        return

    # Otevri prohlizec
    threading.Thread(target=open_browser, daemon=True).start()

    # Spust server
    python = sys.executable
    os.execv(python, [python, server])

if __name__ == '__main__':
    main()

@echo off
title iSupply Scan - Build EXE
color 0A
cd /d "%~dp0"

echo.
echo  ================================================
echo   iSupply Scan - Build Windows EXE (okno, bez konzole)
echo  ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 ( echo [CHYBA] Python neni v PATH! & pause & exit /b 1 )

echo  [1/4] Pouzivam LOKALNI HTML soubory ze slozky (NESTAHUJI z GitHubu)...
if not exist "iphone-diagnostic.html" ( echo [CHYBA] Ve slozce chybi iphone-diagnostic.html & pause & exit /b 1 )
if not exist "isupply_admin.html"     ( echo [CHYBA] Ve slozce chybi isupply_admin.html & pause & exit /b 1 )
if not exist "support.html"           ( echo [CHYBA] Ve slozce chybi support.html & pause & exit /b 1 )
if not exist "server.py"              ( echo [CHYBA] Ve slozce chybi server.py & pause & exit /b 1 )
if not exist "launcher.py"            ( echo [CHYBA] Ve slozce chybi launcher.py & pause & exit /b 1 )
if not exist "scan_quota.py"          ( echo [CHYBA] Ve slozce chybi scan_quota.py & pause & exit /b 1 )
if not exist "icon.ico"               ( echo [CHYBA] Ve slozce chybi icon.ico & pause & exit /b 1 )
echo  OK - buildim PRESNE ty soubory, ktere mas ve slozce (zadny GitHub, zadna cache)

echo  [2/4] Instalace zavislosti...
python -m pip install pyinstaller==6.3.0 flask flask-cors PyJWT pymobiledevice3 readchar requests pywebview --quiet
echo  OK

echo  [3/4] Cisteni stareho buildu...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
echo  OK

echo  [4/4] Build EXE...
python -m PyInstaller ^
  --onefile ^
  --noconsole ^
  --name "iSupply Scan" ^
  --icon "icon.ico" ^
  --add-data "icon.ico;." ^
  --add-data "iS.png;." ^
  --add-data "iphone-diagnostic.html;." ^
  --add-data "isupply_admin.html;." ^
  --add-data "support.html;." ^
  --add-data "photo_2026-07-01_01-43-29.jpg;." ^
  --collect-all flask ^
  --collect-all flask_cors ^
  --collect-all jwt ^
  --collect-all pymobiledevice3 ^
  --collect-all readchar ^
  --hidden-import pymobiledevice3.services.mobile_activation ^
  --copy-metadata readchar ^
  --copy-metadata pymobiledevice3 ^
  --hidden-import encodings ^
  --hidden-import encodings.utf_8 ^
  --hidden-import encodings.ascii ^
  --hidden-import encodings.latin_1 ^
  --hidden-import winreg ^
  --hidden-import scan_quota ^
  --collect-all webview ^
  --hidden-import webview.platforms.edgechromium ^
  --collect-all clr_loader ^
  --collect-all pythonnet ^
  --hidden-import requests ^
  launcher.py

if exist "dist\iSupply Scan.exe" (
    copy "iphone-diagnostic.html" "dist\" >nul
    copy "isupply_admin.html"     "dist\" >nul
    copy "support.html"           "dist\" >nul
    copy "photo_2026-07-01_01-43-29.jpg" "dist\" >nul
    if exist "licence.key" copy "licence.key" "dist\" >nul

    echo.
    echo  ================================================
    echo   HOTOVO! dist\iSupply Scan.exe
    echo  ================================================
    start dist\
) else (
    echo  [CHYBA] Build selhal - viz chyby vyse
)
pause

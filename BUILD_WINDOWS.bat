@echo off
title iSupply Scan – Build EXE
color 0A
cd /d "%~dp0"

echo.
echo  ================================================
echo   iSupply Scan – Build Windows EXE
echo  ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 ( echo [CHYBA] Python neni v PATH! & pause & exit /b 1 )

echo  [1/2] Instalace zavislosti...
python -m pip install pyinstaller==6.3.0 flask flask-cors PyJWT pymobiledevice3 --quiet
echo  OK

echo  [2/2] Build EXE...
python -m PyInstaller ^
  --onefile ^
  --console ^
  --name "iSupply Scan" ^
  --add-data "iphone-diagnostic.html;." ^
  --add-data "isupply_admin.html;." ^
  --add-data "support.html;." ^
  --add-data "photo_2026-07-01_01-43-29.jpg;." ^
  --collect-all flask ^
  --collect-all flask_cors ^
  --collect-all jwt ^
  --collect-all pymobiledevice3 ^
  --hidden-import encodings ^
  --hidden-import encodings.utf_8 ^
  --hidden-import encodings.ascii ^
  --hidden-import encodings.latin_1 ^
  --hidden-import winreg ^
  --hidden-import win32api ^
  --hidden-import win32con ^
  server.py

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

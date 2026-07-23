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

echo  [1/5] Kontrola souboru ve slozce (zadny GitHub, zadna cache)...
if not exist "iphone-diagnostic.html" ( echo [CHYBA] Ve slozce chybi iphone-diagnostic.html & pause & exit /b 1 )
if not exist "isupply_admin.html"     ( echo [CHYBA] Ve slozce chybi isupply_admin.html & pause & exit /b 1 )
if not exist "support.html"           ( echo [CHYBA] Ve slozce chybi support.html & pause & exit /b 1 )
if not exist "server.py"              ( echo [CHYBA] Ve slozce chybi server.py & pause & exit /b 1 )
if not exist "launcher.py"            ( echo [CHYBA] Ve slozce chybi launcher.py & pause & exit /b 1 )
if not exist "scan_quota.py"          ( echo [CHYBA] Ve slozce chybi scan_quota.py & pause & exit /b 1 )
if not exist "model_colors.json"      ( echo [CHYBA] Ve slozce chybi model_colors.json - bez nej appka nezna barvy & pause & exit /b 1 )
if not exist "icon.ico"               ( echo [CHYBA] Ve slozce chybi icon.ico & pause & exit /b 1 )
echo  OK

echo.
echo  [2/5] Kontrola verzi zdrojaku...
rem  Rychla pojistka proti nejcastejsi chybe: buildit se starym souborem.
rem  Kdyz znacka chybi, ve slozce lezi stara verze a build nema smysl.
findstr /C:"LAUNCHER_VERSION" launcher.py >nul || (
    echo  [CHYBA] launcher.py je STARA verze ^(chybi LAUNCHER_VERSION^).
    echo          Nahrad ho aktualnim souborem a spust build znovu.
    pause & exit /b 1
)
findstr /C:"_data_dir" server.py >nul || (
    echo  [CHYBA] server.py je STARA verze ^(chybi _data_dir^).
    echo          Nahrad ho aktualnim souborem a spust build znovu.
    pause & exit /b 1
)
echo  OK - zdrojaky vypadaji aktualne

echo.
echo  [3/5] Instalace zavislosti...
rem  cryptography je potreba na overeni podpisu licencniho tokenu (RS256).
python -m pip install pyinstaller==6.3.0 flask flask-cors PyJWT cryptography pymobiledevice3 readchar requests pywebview --quiet
echo  OK

echo.
echo  [4/5] Cisteni stareho buildu...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
echo  OK

echo.
echo  [5/5] Build EXE...
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
  --add-data "model_colors.json;." ^
  --add-data "photo_2026-07-01_01-43-29.jpg;." ^
  --collect-all flask ^
  --collect-all flask_cors ^
  --collect-all jwt ^
  --collect-all cryptography ^
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
    rem  Kopie vedle EXE slouzi JEN TOBE: umoznuji opravit HTML nebo barvy
    rem  bez rebuildu. Zakaznikovi staci samotny .exe - vsechno je uvnitr.
    copy "iphone-diagnostic.html" "dist\" >nul
    copy "isupply_admin.html"     "dist\" >nul
    copy "support.html"           "dist\" >nul
    copy "model_colors.json"      "dist\" >nul
    copy "photo_2026-07-01_01-43-29.jpg" "dist\" >nul

    echo.
    echo  ================================================
    echo   HOTOVO! dist\iSupply Scan.exe
    echo  ================================================
    echo.
    echo   NA WEB NAHRAVEJ POUZE SOUBOR:  dist\iSupply Scan.exe
    echo   Vsechno potrebne je zabalene uvnitr nej.
    echo.

    if exist "licence.key" (
        echo   ------------------------------------------------
        echo    POZOR: ve slozce projektu lezi TVOJE licence.key
        echo    Do dist\ se ZAMERNE nekopiruje, aby ses o ni
        echo    omylem nepodelil se zakazniky.
        echo.
        echo    Pro vlastni testovani si ji tam zkopiruj rucne:
        echo        copy licence.key dist\
        echo.
        echo    Pro test PRUCHODU ZAKAZNIKA ji tam NEDAVEJ -
        echo    aplikace pak spravne nabidne aktivacni okno.
        echo   ------------------------------------------------
        echo.
    )
    start dist\
) else (
    echo  [CHYBA] Build selhal - viz chyby vyse
)
pause

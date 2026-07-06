@echo off
title iSupply Scan Server
color 0F
echo.
echo  ================================================
echo   iSupply Scan - Spousteni serveru
echo  ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [CHYBA] Python neni nainstalovan!
    echo  https://www.python.org/downloads/
    pause & exit /b 1
)

echo  [1/3] Instalace zavislosti...
pip install flask flask-cors pymobiledevice3 --quiet
echo  OK

echo  [2/3] Kontrola Apple Mobile Device Service...
sc query "Apple Mobile Device Service" >nul 2>&1
if errorlevel 1 (
    echo  POZOR: Apple Mobile Device Service nebezi!
    echo  Nainstalujte iTunes nebo Apple Devices z Microsoft Store.
) else (
    echo  OK
)

echo  [3/3] Spousteni serveru...
echo.
echo  ================================================
echo   Diagnostika:  http://localhost:5000
echo   Admin panel:  http://localhost:5000/admin
echo  ================================================
echo.
echo  Zavreni tohoto okna = vypnuti serveru
echo.

timeout /t 2 >nul
start http://localhost:5000

python server.py
pause

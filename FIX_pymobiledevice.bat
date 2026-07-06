@echo off
title iSupply - Fix pymobiledevice3
color 0F
echo.
echo  Zjistuji ktery Python bezi...
echo.

python -c "import sys; print('Python:', sys.executable)"
echo.

echo  Instaluji pymobiledevice3 do spravneho Pythonu...
python -m pip install pymobiledevice3 --upgrade
echo.

echo  Overuji instalaci...
python -c "import pymobiledevice3; print('OK - pymobiledevice3 verze:', pymobiledevice3.__version__)"

if errorlevel 1 (
    echo.
    echo  CHYBA - zkousim alternativni instalaci...
    python -m pip install --user pymobiledevice3
    python -c "import pymobiledevice3; print('OK:', pymobiledevice3.__version__)"
)

echo.
echo  Hotovo! Zavri toto okno a spust START.bat
echo.
pause

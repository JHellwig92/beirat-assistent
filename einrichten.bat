@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title Beirat-Assistent - Einrichtung
cd /d "%~dp0"

echo ============================================================
echo   Beirat-Assistent - einmalige Einrichtung
echo ============================================================
echo.

rem ---- Python suchen ----
set "PYEXE="
py -3 -V >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
)
if not defined PYEXE (
    python -V >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
    )
)
if defined PYEXE goto :python_da

echo Python ist noch nicht installiert.
echo Es wird jetzt automatisch heruntergeladen und installiert
echo (einmalig, dauert einige Minuten - bitte warten) ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $env:TEMP\python-setup.exe"
if not exist "%TEMP%\python-setup.exe" (
    echo FEHLER: Der Download hat nicht geklappt.
    echo Bitte Internetverbindung pruefen und einrichten.bat erneut starten.
    pause
    exit /b 1
)
echo Installation laeuft ...
"%TEMP%\python-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
if not exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    echo FEHLER: Die Python-Installation hat nicht geklappt.
    echo Bitte einrichten.bat erneut starten oder Hilfe holen.
    pause
    exit /b 1
)
set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
echo Python wurde installiert.
echo.

:python_da
echo [1/3] Programmierumgebung wird vorbereitet ...
if exist ".venv" (
    echo     Alte Umgebung wird entfernt und neu erstellt ...
    rmdir /s /q ".venv"
)
"%PYEXE%" -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo FEHLER: Die Umgebung konnte nicht erstellt werden.
    pause
    exit /b 1
)

echo [2/3] Benoetigte Komponenten werden installiert
echo       (einmalig, dauert einige Minuten - bitte warten) ...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo FEHLER: Die Installation hat nicht geklappt.
    echo Bitte Internetverbindung pruefen und einrichten.bat erneut starten.
    pause
    exit /b 1
)

echo [3/3] Persoenliche Einrichtung ...
echo.
.venv\Scripts\python.exe einrichten.py
echo.
pause

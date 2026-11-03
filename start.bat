@echo off
rem Alles steht in EINEM Klammerblock: Windows liest ihn komplett ein, bevor er
rem ausgefuehrt wird. So kann ein automatisches Update diese Datei gefahrlos ersetzen.
(
    chcp 65001 >nul
    set "PYTHONIOENCODING=utf-8"
    title Beirat-Assistent
    cd /d "%~dp0"
    if not exist ".venv\Scripts\python.exe" (
        echo Bitte zuerst einmalig "einrichten.bat" doppelklicken.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" starten.py
    pause
    exit /b
)

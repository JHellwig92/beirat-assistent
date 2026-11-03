@echo off
(
    chcp 65001 >nul
    set "PYTHONIOENCODING=utf-8"
    title Ollama pruefen
    cd /d "%~dp0"
    echo Pruefe die lokale KI Ollama ...
    echo.
    ".venv\Scripts\python.exe" ollama_client.py
    echo.
    echo Bitte ein Foto oder Bildschirmfoto von diesem Fenster schicken.
    pause
    exit /b
)

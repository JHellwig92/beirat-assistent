@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

echo ============================================================
echo   Beirat-Assistent - Update wird eingespielt
echo ============================================================
echo.
echo Wichtig: Diese Datei muss im BeiratAssistent-Ordner liegen
echo (dort, wo auch start.bat und .venv liegen) - NICHT auf dem
echo USB-Stick ausfuehren.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [FEHLER] Keine .venv in diesem Ordner gefunden.
    echo.
    echo Bitte zuerst alle aktualisierten Dateien vom USB-Stick in
    echo den BeiratAssistent-Ordner kopieren ^(Ueberschreiben bestaetigen^),
    echo und ERST DANACH diese Datei dort per Doppelklick ausfuehren.
    echo.
    pause
    exit /b 1
)

echo Installiere neue/aktualisierte Pakete ...
echo.
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [FEHLER] Installation fehlgeschlagen. Bitte Bescheid geben.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Unterlagen werden neu eingelesen
echo ============================================================
echo Nach diesem Update werden alle Unterlagen einmalig neu
echo eingelesen. Eingescannte Dokumente liest dabei die KI.
echo Das kann eine Weile dauern - bitte das Fenster offen lassen.
echo Falls es unterbrochen wird: einfach erneut starten, es geht
echo dort weiter, wo es aufgehoert hat.
echo.
".venv\Scripts\python.exe" -c "import pdf_indexer; pdf_indexer.erststart_indexierung()"
if errorlevel 1 (
    echo.
    echo [HINWEIS] Das Einlesen wurde nicht ganz abgeschlossen. Der Rest
    echo wird beim naechsten Start des Assistenten automatisch nachgeholt.
)

echo.
echo ============================================================
echo   Fertig! Der Beirat-Assistent kann jetzt wie gewohnt ueber
echo   start.bat gestartet werden.
echo ============================================================
echo.
pause

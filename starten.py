"""Wird von start.bat aufgerufen: erst nach Updates schauen, dann die App starten.
(Die Logik steht hier statt in start.bat, damit Updates sie gefahrlos ersetzen können.)"""
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent


def main() -> int:
    try:
        import aktualisieren
        aktualisieren.pruefe_und_aktualisiere()
    except Exception as e:
        print(f"(Update-Prüfung fehlgeschlagen: {str(e)[:120]} — es geht normal weiter.)")

    print("=" * 60)
    print("  Beirat-Assistent startet ...")
    print("  Gleich öffnet sich das Browserfenster.")
    print()
    print("  Dieses schwarze Fenster bitte geöffnet lassen!")
    print("  Zum Beenden: dieses Fenster einfach schließen.")
    print("=" * 60)
    print()
    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", str(APP / "app.py"),
         "--server.port", "8531", "--browser.gatherUsageStats", "false"],
        cwd=APP,
    )


if __name__ == "__main__":
    sys.exit(main())

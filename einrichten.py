"""Einmalige Einrichtung — wird von einrichten.bat aufgerufen."""
import sys
from pathlib import Path

APP_ORDNER = Path(__file__).parent
ENV_PFAD = APP_ORDNER / ".env"


def _frage(text: str, standard: str = "") -> str:
    hinweis = f" [{standard}]" if standard else ""
    antwort = input(f"{text}{hinweis}: ").strip().strip('"').strip("'")
    return antwort or standard


def _lade_bestehende_werte() -> dict:
    werte = {}
    if ENV_PFAD.exists():
        for zeile in ENV_PFAD.read_text(encoding="utf-8").splitlines():
            if "=" in zeile and not zeile.lstrip().startswith("#"):
                schluessel, wert = zeile.split("=", 1)
                werte[schluessel.strip()] = wert.strip()
    return werte


def main():
    print()
    print("=" * 60)
    print("  Beirat-Assistent — persönliche Einrichtung")
    print("=" * 60)
    print()

    alt = _lade_bestehende_werte()

    # 1. API-Schlüssel
    print("Schritt 1 von 3: Der Google-KI-Schlüssel (kostenlos)")
    print("  1. Im Browser öffnen:  https://aistudio.google.com/apikey")
    print("  2. Mit dem Google-Konto anmelden")
    print("  3. Auf »API-Schlüssel erstellen« klicken")
    print("  4. Den Schlüssel kopieren und hier einfügen (Rechtsklick → Einfügen)")
    print()
    while True:
        key = _frage("Google-KI-Schlüssel", alt.get("GEMINI_API_KEY", ""))
        if key and not key.startswith("AIzaSy..."):
            break
        print("  [!] Bitte einen gültigen Schlüssel eingeben.")

    # 2. Dokumenten-Ordner
    print()
    print("Schritt 2 von 3: Der Ordner mit den Unterlagen")
    print("  (PDF-, Word- und Textdateien; Unterordner werden mitgelesen)")
    print()
    while True:
        ordner = _frage("Pfad zum Dokumenten-Ordner", alt.get("PDF_ORDNER", ""))
        if ordner and Path(ordner).is_dir():
            break
        print("  [!] Diesen Ordner gibt es nicht. Tipp: Im Datei-Explorer oben in")
        print("      die Adresszeile klicken, den Pfad kopieren und hier einfügen.")

    # 3. Land
    print()
    land = _frage("Schritt 3 von 3: Land (DE oder AT)", alt.get("LAND", "DE")).upper()
    if land not in ("DE", "AT"):
        land = "DE"

    ENV_PFAD.write_text(
        f"GEMINI_API_KEY={key}\nPDF_ORDNER={ordner}\nLAND={land}\n",
        encoding="utf-8",
    )
    print()
    print("✓ Einstellungen gespeichert.")

    # Verbindung testen
    print()
    print("Die Verbindung zur KI wird getestet …")
    import config  # erst jetzt importieren, damit die neue .env gelesen wird
    from gemini_ki import teste_verbindung
    ok, meldung = teste_verbindung()
    print(("✓ " if ok else "[!] ") + meldung)
    if not ok:
        print("    Die Einrichtung kann trotzdem fortgesetzt werden —")
        print("    bei Problemen einfach einrichten.bat noch einmal ausführen.")

    # Erstindexierung
    print()
    print("Die Unterlagen werden jetzt eingelesen (einmalig, kann dauern) …")
    print()
    import pdf_indexer
    pdf_indexer.erststart_indexierung()

    print()
    print("=" * 60)
    print("  Fertig! Ab jetzt einfach »start.bat« doppelklicken.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(1)

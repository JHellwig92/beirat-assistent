import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Konsolenausgabe darf an Umlauten/Sonderzeichen nie scheitern (Windows cp1252)
for _strom in (sys.stdout, sys.stderr):
    if _strom and hasattr(_strom, "reconfigure"):
        try:
            _strom.reconfigure(errors="replace")
        except Exception:
            pass

_env_pfad = Path(__file__).parent / ".env"
load_dotenv(_env_pfad)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELL = os.getenv("GEMINI_MODELL", "gemini-2.5-flash")
# Mehrsprachiges Suchmodell (versteht Deutsch deutlich besser als das
# englische Standardmodell von ChromaDB). Gleicher API-Schlüssel.
GEMINI_EMBED_MODELL = os.getenv("GEMINI_EMBED_MODELL", "gemini-embedding-001")
EMBED_DIMENSIONEN = 768

# Tempo beim Einlesen — knapp unter den Limits der kostenlosen Gemini-Stufe
# (Stand 2025: 2.5 Flash ca. 10 Anfragen/Min., Embedding ca. 100 Anfragen und
# 30.000 Tokens/Min.). Mit bezahltem Zugang in der .env KI_TEMPOLIMIT=aus setzen.
KI_TEMPOLIMIT = os.getenv("KI_TEMPOLIMIT", "an").strip().lower() not in ("aus", "0", "nein", "false")
TEMPO_TEXT_ANFRAGEN = 8
TEMPO_EMBED_ANFRAGEN = 80
TEMPO_EMBED_TOKENS = 25_000
PDF_ORDNER = Path(os.getenv("PDF_ORDNER", "."))
LAND = os.getenv("LAND", "DE")

APP_ORDNER = Path(__file__).parent
DATEN_ORDNER = APP_ORDNER / "beirat_daten"
CHROMA_PFAD = DATEN_ORDNER / "chroma_db"
VERLAUF_PFAD = DATEN_ORDNER / "verlauf.json"
WISSENSSTAND_PFAD = DATEN_ORDNER / "wissensstand.md"
MANIFEST_PFAD = DATEN_ORDNER / "dokument_manifest.json"
LOG_PFAD = DATEN_ORDNER / "einlesen.log"
ORDNER_LISTE_PFAD = DATEN_ORDNER / "ordner.json"      # zusätzliche Ordner + Ein/Aus
OCR_CACHE_ORDNER = DATEN_ORDNER / "ocr_cache"         # erkannter Text aus Scans
GESPRAECH_QUELLEN_PFAD = DATEN_ORDNER / "gespraech_quellen.json"
GESPRAECH_DATEIEN_ORDNER = DATEN_ORDNER / "gespraech_dateien"  # hochgeladene Dateien

UNTERSTUETZTE_TYPEN = {".pdf", ".docx", ".txt"}
MAX_CHUNK_LAENGE = 900
CHUNK_UEBERLAPPUNG = 150
MAX_PDF_ERGEBNISSE = 10      # Textstellen, die pro Frage an die KI gehen
MAX_PRO_DATEI = 3            # höchstens so viele davon aus derselben Datei
SUCH_KANDIDATEN = 40         # Vorauswahl je Suchverfahren vor dem Zusammenführen
GESPRAECH_ERGEBNISSE = 6     # zusätzliche Textstellen aus den Quellen nur für dieses Gespräch
MAX_VERGLEICH_ZEICHEN = 300_000   # je Dokument beim Dokumentenvergleich (~75.000 Wörter)
OCR_SEITEN_PRO_ANFRAGE = 8   # Scan-Seiten, die gemeinsam an Gemini gehen
MAX_WEB_ERGEBNISSE = 4

DATEN_ORDNER.mkdir(exist_ok=True)
OCR_CACHE_ORDNER.mkdir(exist_ok=True)

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
# Suchmodell lokal über Ollama (kein Tageslimit, nichts geht dafür an Google).


def _ollama_adresse() -> str:
    """OLLAMA_URL aus der .env; sonst die Adresse, auf der Ollama selbst lauscht
    (Umgebungsvariable OLLAMA_HOST, z. B. von einem anderen Programm gesetzt)."""
    if os.getenv("OLLAMA_URL"):
        return os.getenv("OLLAMA_URL")
    host = (os.getenv("OLLAMA_HOST") or "").strip().rstrip("/")
    if not host:
        return "http://127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    schema, rest = host.split("://", 1)
    if rest.startswith(":"):
        rest = "127.0.0.1" + rest
    name, _, port = rest.rpartition(":") if rest.count(":") == 1 else (rest, "", "")
    if not name:
        name, port = rest, ""
    if name in ("0.0.0.0", "", "[::]", "::"):
        name = "127.0.0.1"
    return f"{schema}://{name}:{port or '11434'}"


OLLAMA_URL = _ollama_adresse()
OLLAMA_EMBED_MODELL = os.getenv("OLLAMA_EMBED_MODELL", "qwen3-embedding:0.6b")
# Texterkennung eingescannter Seiten: "lokal" (Ollama, kein Limit, nichts geht an
# Google; Gemini nur als Ersatz, falls Ollama fehlt) oder "gemini".
TEXTERKENNUNG = "gemini" if os.getenv("TEXTERKENNUNG", "lokal").strip().lower() == "gemini" else "lokal"
OLLAMA_OCR_MODELL = os.getenv("OLLAMA_OCR_MODELL", "qwen3-vl:4b")

# Tempo der Texterkennung — knapp unter dem Minutenlimit der kostenlosen
# Gemini-Stufe. Mit bezahltem Zugang in der .env KI_TEMPOLIMIT=aus setzen.
KI_TEMPOLIMIT = os.getenv("KI_TEMPOLIMIT", "an").strip().lower() not in ("aus", "0", "nein", "false")
TEMPO_TEXT_ANFRAGEN = 8
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
OCR_SEITEN_PRO_ANFRAGE = 8   # Scan-Seiten, die gemeinsam an Gemini gehen (nur Ersatzweg)
OCR_DPI = 150                # Auflösung der Seitenbilder für die lokale Texterkennung
MAX_WEB_ERGEBNISSE = 4

DATEN_ORDNER.mkdir(exist_ok=True)
OCR_CACHE_ORDNER.mkdir(exist_ok=True)

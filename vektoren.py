"""Suchvektoren: lokal berechnet mit Ollama (qwen3-embedding), gespeichert in ChromaDB.

- Kein Tageslimit, nichts geht dafür an Google.
- Läuft Ollama nicht, wird es gestartet; fehlt das Modell, wird es geladen.
- Ist Ollama gar nicht verfügbar, funktioniert die Suche trotzdem —
  dann nur über Stichworte.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
import textspeicher

# Empfehlung von Qwen: Suchanfragen mit kurzer Aufgabenbeschreibung kodieren,
# Dokument-Abschnitte ohne.
_ANFRAGE_PRAEFIX = ("Instruct: Given a question, retrieve passages from property owners' "
                    "association documents that answer it\nQuery: ")
_ALTE_COLLECTIONS = ("beirat_dokumente", "beirat_dokumente_v2")

_lock = threading.RLock()
_collection = None


class OllamaNichtVerfuegbar(RuntimeError):
    pass


# ── Ollama ────────────────────────────────────────────────────────────────────

def _anfrage(pfad: str, daten: dict | None = None, timeout: float = 30):
    url = config.OLLAMA_URL.rstrip("/") + pfad
    if daten is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(daten).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _laeuft() -> bool:
    try:
        _anfrage("/api/version", timeout=3)
        return True
    except Exception:
        return False


def _starte_ollama() -> bool:
    """Startet Ollama im Hintergrund (Windows: wie beim Anmelden, ohne Fenster)."""
    kandidaten = []
    lokal = os.environ.get("LOCALAPPDATA")
    if lokal:
        kandidaten.append([str(Path(lokal) / "Programs" / "Ollama" / "ollama app.exe")])
        kandidaten.append([str(Path(lokal) / "Programs" / "Ollama" / "ollama.exe"), "serve"])
    if shutil.which("ollama"):
        kandidaten.append([shutil.which("ollama"), "serve"])
    flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0   # DETACHED | NO_WINDOW
    for befehl in kandidaten:
        if not Path(befehl[0]).exists():
            continue
        try:
            subprocess.Popen(befehl, creationflags=flags, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=os.name != "nt")
        except Exception:
            continue
        for _ in range(30):
            time.sleep(0.5)
            if _laeuft():
                return True
    return False


def _modell_vorhanden() -> bool:
    namen = {m.get("name", "") for m in _anfrage("/api/tags", timeout=10).get("models", [])}
    gesucht = config.OLLAMA_EMBED_MODELL
    return gesucht in namen or (":" not in gesucht and f"{gesucht}:latest" in namen)


def bereit(meldung=None) -> tuple[bool, str]:
    """Sorgt dafür, dass Ollama läuft und das Suchmodell da ist."""
    if not _laeuft():
        if meldung:
            meldung("Ollama wird gestartet …")
        if not _starte_ollama():
            return False, ("Ollama läuft nicht und ließ sich nicht starten. Die Suche nutzt "
                           "bis dahin nur Stichworte.")
    try:
        if not _modell_vorhanden():
            if meldung:
                meldung(f"Suchmodell {config.OLLAMA_EMBED_MODELL} wird einmalig heruntergeladen (ca. 640 MB) …")
            _anfrage("/api/pull", {"model": config.OLLAMA_EMBED_MODELL, "stream": False}, timeout=3600)
    except Exception as e:
        return False, f"Suchmodell konnte nicht geladen werden: {str(e)[:150]}"
    return True, "bereit"


def bette_ein(texte: list[str], anfrage: bool = False, timeout: float = 300) -> list[list[float]]:
    if anfrage:
        texte = [_ANFRAGE_PRAEFIX + t for t in texte]
    try:
        antwort = _anfrage("/api/embed", {
            "model": config.OLLAMA_EMBED_MODELL, "input": texte, "truncate": True,
            "keep_alive": "10m",
        }, timeout=timeout)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise OllamaNichtVerfuegbar(str(e)) from e
    return antwort["embeddings"]


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def _collection_name() -> str:
    roh = "vektoren_" + config.OLLAMA_EMBED_MODELL
    return "".join(z if z.isalnum() else "_" for z in roh)[:60]


def collection():
    """Vektor-Sammlung für das aktuelle Suchmodell. Wurde das Modell gewechselt,
    werden alle Abschnitte zum Neuberechnen markiert."""
    global _collection
    with _lock:
        if _collection is None:
            import chromadb
            client = chromadb.PersistentClient(path=str(config.CHROMA_PFAD))
            _collection = client.get_or_create_collection(
                name=_collection_name(), metadata={"hnsw:space": "cosine"}, embedding_function=None,
            )
            if textspeicher.meta("vektor_modell") != config.OLLAMA_EMBED_MODELL:
                textspeicher.setze_vektoren_zurueck()
                textspeicher.setze_meta("vektor_modell", config.OLLAMA_EMBED_MODELL)
        return _collection


def raeume_alte_auf():
    """Entfernt Vektor-Sammlungen früherer Versionen — erst aufrufen, wenn der
    Textspeicher neu befüllt ist."""
    import chromadb
    client = chromadb.PersistentClient(path=str(config.CHROMA_PFAD))
    for name in _ALTE_COLLECTIONS:
        try:
            client.delete_collection(name)
        except Exception:
            pass


def speichere(ids: list[str], vektoren: list[list[float]], metas: list[dict]):
    collection().upsert(ids=ids, embeddings=vektoren, metadatas=metas)


def entferne(ids: list[str]):
    if not ids:
        return
    for s in range(0, len(ids), 500):
        try:
            collection().delete(ids=ids[s : s + 500])
        except Exception:
            pass


def suche(vektor: list[float], ordner: list[str], anzahl: int) -> list[str]:
    col = collection()
    menge = col.count()
    if not menge:
        return []
    res = col.query(query_embeddings=[vektor], n_results=min(anzahl, menge),
                    where={"ordner": {"$in": ordner}}, include=[])
    return res["ids"][0]

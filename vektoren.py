"""Suchvektoren: lokal berechnet mit Ollama (qwen3-embedding), gespeichert in ChromaDB.

- Kein Tageslimit, nichts geht dafür an Google.
- Läuft Ollama nicht, wird es gestartet; fehlt das Modell, wird es geladen.
- Ist Ollama gar nicht verfügbar, funktioniert die Suche trotzdem —
  dann nur über Stichworte.
"""
import threading

import config
import ollama_client
import textspeicher

# Empfehlung von Qwen: Suchanfragen mit kurzer Aufgabenbeschreibung kodieren,
# Dokument-Abschnitte ohne.
_ANFRAGE_PRAEFIX = ("Instruct: Given a question, retrieve passages from property owners' "
                    "association documents that answer it\nQuery: ")
_ALTE_COLLECTIONS = ("beirat_dokumente", "beirat_dokumente_v2")

_lock = threading.RLock()
_collection = None


# ── Ollama ────────────────────────────────────────────────────────────────────

OllamaNichtVerfuegbar = ollama_client.OllamaNichtVerfuegbar


def bereit(meldung=None) -> tuple[bool, str]:
    """Sorgt dafür, dass Ollama läuft und das Suchmodell da ist."""
    ok, text = ollama_client.bereit(config.OLLAMA_EMBED_MODELL, "640 MB", meldung)
    if not ok:
        text += " Die Suche nutzt bis dahin nur Stichworte."
    return ok, text


def bette_ein(texte: list[str], anfrage: bool = False, timeout: float = 300) -> list[list[float]]:
    """Suchanfragen (anfrage=True) entladen das Modell sofort wieder."""
    if anfrage:
        return ollama_client.bette_ein([_ANFRAGE_PRAEFIX + t for t in texte], timeout, keep_alive=0)
    return ollama_client.bette_ein(texte, timeout)


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

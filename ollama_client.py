"""Gemeinsame Anbindung an Ollama (lokale KI auf dem eigenen Rechner).

Genutzt für die Suchvektoren (qwen3-embedding) und die Texterkennung von Scans
(qwen3-vl). Läuft Ollama nicht, wird es gestartet; fehlt ein Modell, wird es
automatisch heruntergeladen.

Die Modelle bleiben nur geladen, solange sie arbeiten: Nach jedem Arbeitsgang
(Einlesen, Vergleich, Suchanfrage) werden sie sofort entladen, damit Grafikkarte
und Arbeitsspeicher wieder frei sind. Bricht etwas ab, entlädt Ollama sie
spätestens nach ARBEITS_PAUSE von selbst.
"""
import base64
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


class OllamaNichtVerfuegbar(RuntimeError):
    pass


ARBEITS_PAUSE = "2m"   # so lange darf ein Modell zwischen zwei Anfragen geladen bleiben

_bereit: set[str] = set()      # Modelle, die in dieser Sitzung schon geprüft sind
_benutzt: set[str] = set()     # Modelle, die evtl. noch geladen sind
_fehlschlag: dict[str, float] = {}   # Modell → Zeitpunkt des letzten Fehlschlags
_lock = threading.Lock()
WARTEZEIT_NACH_FEHLER = 300    # Sekunden, bevor ein erneuter Start/Download versucht wird


def anfrage(pfad: str, daten: dict | None = None, timeout: float = 30):
    url = config.OLLAMA_URL.rstrip("/") + pfad
    if daten is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(daten).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise OllamaNichtVerfuegbar(str(e)) from e


def laeuft() -> bool:
    try:
        anfrage("/api/version", timeout=3)
        return True
    except Exception:
        return False


def starte() -> bool:
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
            if laeuft():
                return True
    return False


def _modell_vorhanden(modell: str) -> bool:
    namen = {m.get("name", "") for m in anfrage("/api/tags", timeout=10).get("models", [])}
    return modell in namen or (":" not in modell and f"{modell}:latest" in namen)


def bereit(modell: str, groesse: str = "", meldung=None) -> tuple[bool, str]:
    """Sorgt dafür, dass Ollama läuft und das Modell da ist."""
    with _lock:
        if modell in _bereit and laeuft():
            return True, "bereit"
        if time.time() - _fehlschlag.get(modell, 0) < WARTEZEIT_NACH_FEHLER:
            return False, "Lokale KI (Ollama) gerade nicht verfügbar."
    if not laeuft():
        if meldung:
            meldung("Ollama wird gestartet …")
        if not starte():
            _fehlschlag[modell] = time.time()
            return False, "Ollama läuft nicht und ließ sich nicht starten."
    try:
        if not _modell_vorhanden(modell):
            if meldung:
                meldung(f"KI-Modell {modell} wird einmalig heruntergeladen"
                        f"{f' (ca. {groesse})' if groesse else ''} …")
            anfrage("/api/pull", {"model": modell, "stream": False}, timeout=7200)
    except Exception as e:
        _fehlschlag[modell] = time.time()
        return False, f"KI-Modell {modell} konnte nicht geladen werden: {str(e)[:150]}"
    with _lock:
        _bereit.add(modell)
        _fehlschlag.pop(modell, None)
    return True, "bereit"


def vergiss(modell: str):
    """Nach einem Fehler: Modell gilt als nicht verfügbar (erneuter Versuch
    frühestens nach WARTEZEIT_NACH_FEHLER)."""
    with _lock:
        _bereit.discard(modell)
        _fehlschlag[modell] = time.time()


def entlade(*modelle: str):
    """Gibt Grafikkarte/Arbeitsspeicher sofort frei. Andere Modelle (z. B. von
    anderen Programmen) bleiben unberührt."""
    for modell in modelle or tuple(_benutzt):
        with _lock:
            if modell not in _benutzt:
                continue
            _benutzt.discard(modell)
        try:
            if modell == config.OLLAMA_EMBED_MODELL:
                anfrage("/api/embed", {"model": modell, "input": [], "keep_alive": 0}, timeout=15)
            else:
                anfrage("/api/generate", {"model": modell, "keep_alive": 0}, timeout=15)
        except Exception:
            pass


def bette_ein(texte: list[str], timeout: float = 300, keep_alive=ARBEITS_PAUSE) -> list[list[float]]:
    """keep_alive=0: Modell gleich nach der Anfrage wieder entladen."""
    with _lock:
        _benutzt.add(config.OLLAMA_EMBED_MODELL)
    antwort = anfrage("/api/embed", {
        "model": config.OLLAMA_EMBED_MODELL, "input": texte, "truncate": True, "keep_alive": keep_alive,
    }, timeout=timeout)
    if keep_alive == 0:
        with _lock:
            _benutzt.discard(config.OLLAMA_EMBED_MODELL)
    return antwort["embeddings"]


def lies_bild(bild: bytes, anweisung: str, timeout: float = 900) -> str:
    """Lässt das lokale Vision-Modell ein Bild lesen (Texterkennung)."""
    with _lock:
        _benutzt.add(config.OLLAMA_OCR_MODELL)
    antwort = anfrage("/api/generate", {
        "model": config.OLLAMA_OCR_MODELL,
        "prompt": anweisung,
        "images": [base64.b64encode(bild).decode()],
        "stream": False,
        "think": False,
        "keep_alive": ARBEITS_PAUSE,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 4096},
    }, timeout=timeout)
    return (antwort.get("response") or "").strip()

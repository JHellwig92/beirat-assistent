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


def _protokolliere(text: str):
    try:
        import gemini_ki
        gemini_ki.protokolliere(f"OLLAMA: {text}")
    except Exception:
        pass


def _installationsordner() -> list[Path]:
    """Mögliche Ollama-Ordner: Standardpfade, PATH und Windows-Registrierung."""
    ordner = []
    for var, unterpfad in (("LOCALAPPDATA", "Programs/Ollama"), ("ProgramFiles", "Ollama"),
                           ("ProgramW6432", "Ollama"), ("ProgramFiles(x86)", "Ollama")):
        if os.environ.get(var):
            ordner.append(Path(os.environ[var]) / unterpfad)
    if shutil.which("ollama"):
        ordner.append(Path(shutil.which("ollama")).parent)
    if os.name == "nt":
        try:
            import winreg
            for wurzel in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    basis = winreg.OpenKey(wurzel, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(basis)[0]):
                    try:
                        k = winreg.OpenKey(basis, winreg.EnumKey(basis, i))
                        if str(winreg.QueryValueEx(k, "DisplayName")[0]).lower().startswith("ollama"):
                            ordner.append(Path(winreg.QueryValueEx(k, "InstallLocation")[0]))
                    except OSError:
                        continue
        except Exception as e:
            _protokolliere(f"Registrierung nicht lesbar: {e}")
    eindeutig = []
    for o in ordner:
        if o not in eindeutig:
            eindeutig.append(o)
    return eindeutig


_letzte_ursache = ""


def starte() -> bool:
    """Startet Ollama im Hintergrund (Windows: wie beim Anmelden, ohne Fenster).
    Der erste Start nach dem Hochfahren kann bis zu einer Minute dauern."""
    global _letzte_ursache
    befehle = []
    for o in _installationsordner():
        if (o / "ollama app.exe").exists():
            befehle.append([str(o / "ollama app.exe")])
        for name in ("ollama.exe", "ollama"):
            if (o / name).exists():
                befehle.append([str(o / name), "serve"])
                break
    if not befehle:
        _letzte_ursache = "Ollama wurde auf diesem Rechner nicht gefunden"
        _protokolliere(f"{_letzte_ursache}. Gesucht in: "
                       + ", ".join(str(o) for o in _installationsordner()))
        return False
    flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0   # DETACHED | NO_WINDOW
    for befehl in befehle:
        try:
            subprocess.Popen(befehl, creationflags=flags, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=os.name != "nt")
        except Exception as e:
            _protokolliere(f"Start fehlgeschlagen {befehl}: {e}")
            continue
        _protokolliere(f"gestartet: {befehl}, warte auf {config.OLLAMA_URL}")
        for _ in range(120):          # bis zu 60 s
            time.sleep(0.5)
            if laeuft():
                _protokolliere("läuft")
                return True
        _protokolliere(f"keine Antwort nach 60 s unter {config.OLLAMA_URL}: {befehl}")
    _letzte_ursache = f"Ollama wurde gestartet, antwortet aber nicht unter {config.OLLAMA_URL}"
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
            return False, (f"Lokale KI (Ollama) läuft nicht: {_letzte_ursache}. "
                           "Details in beirat_daten\\einlesen.log.")
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


def diagnose():
    """Für ollama_pruefen.bat: zeigt Schritt für Schritt, woran es hakt."""
    print(f"Adresse:           {config.OLLAMA_URL}")
    print(f"OLLAMA_HOST:       {os.environ.get('OLLAMA_HOST') or '(nicht gesetzt)'}")
    ordner = _installationsordner()
    gefunden = [o for o in ordner if (o / "ollama.exe").exists() or (o / "ollama app.exe").exists()
                or (o / "ollama").is_file()]
    print("Installiert in:    " + (", ".join(map(str, gefunden)) or "NICHT GEFUNDEN"))
    if not gefunden:
        print("  Gesucht in:      " + ", ".join(map(str, ordner)))
    if laeuft():
        print("Läuft:             ja")
    else:
        print("Läuft:             nein — starte (bis zu 1 Minute) ...")
        print("Gestartet:         " + ("ja" if starte() else f"NEIN — {_letzte_ursache}"))
    if laeuft():
        try:
            print(f"Version:           {anfrage('/api/version', timeout=5).get('version')}")
            namen = [m.get("name") for m in anfrage("/api/tags", timeout=10).get("models", [])]
            print("Modelle:           " + (", ".join(namen) or "(keine)"))
            for m in (config.OLLAMA_EMBED_MODELL, config.OLLAMA_OCR_MODELL):
                print(f"  {m:24} {'vorhanden' if m in namen else 'fehlt (wird automatisch geladen)'}")
        except Exception as e:
            print(f"Fehler:            {e}")


if __name__ == "__main__":
    diagnose()

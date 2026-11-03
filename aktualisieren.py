"""Automatische Updates von GitHub — läuft bei jedem Start (über starten.py).

Holt die neueste FREIGEGEBENE Version (GitHub-Release) und ersetzt nur die
Programmdateien. Niemals angefasst werden:
    .env            (API-Schlüssel, Ordner)
    beirat_daten/   (Index, Verlauf, Wissensstand, Gesprächsquellen)
    .venv/          (installierte Pakete)

Ohne Internet oder bei Problemen startet der Assistent einfach mit der
bisherigen Version. Geht beim Einspielen etwas schief, werden die alten
Dateien wiederhergestellt. Abschalten: AUTO_UPDATE=aus in der .env.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "JHellwig92/beirat-assistent"
APP = Path(__file__).resolve().parent
VERSION_DATEI = APP / "version.txt"
GESCHUETZT = {".env", "beirat_daten", ".venv", ".git"}
SICHERUNG = APP / "beirat_daten" / "vorherige_version"


def _hole(url: str, timeout: int) -> bytes:
    anfrage = urllib.request.Request(url, headers={
        "User-Agent": "beirat-assistent-update",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:
        return antwort.read()


def installierte_version() -> str:
    try:
        return VERSION_DATEI.read_text(encoding="utf-8").strip()
    except Exception:
        return "0"


def _auto_update_aus() -> bool:
    try:
        from dotenv import dotenv_values
        wert = dotenv_values(APP / ".env").get("AUTO_UPDATE", "an")
    except Exception:
        wert = "an"
    return str(wert).strip().lower() in ("aus", "0", "nein", "false")


def _dateien_aus_zip(daten: bytes) -> dict[str, bytes]:
    """GitHub packt alles in einen Oberordner (owner-repo-commit/) — der wird entfernt."""
    dateien = {}
    with zipfile.ZipFile(io.BytesIO(daten)) as zf:
        for eintrag in zf.infolist():
            if eintrag.is_dir():
                continue
            teile = eintrag.filename.split("/", 1)
            if len(teile) < 2 or not teile[1]:
                continue
            rel = teile[1]
            if ".." in rel.split("/") or rel.startswith("/"):
                continue
            dateien[rel] = zf.read(eintrag)
    return dateien


def _wiederherstellen(geaendert: list[str], neu_angelegt: list[str]):
    for rel in geaendert:
        try:
            shutil.copy2(SICHERUNG / rel, APP / rel)
        except Exception:
            pass
    for rel in neu_angelegt:
        try:
            (APP / rel).unlink()
        except Exception:
            pass


def pruefe_und_aktualisiere() -> bool:
    """Gibt True zurück, wenn ein Update eingespielt wurde."""
    if _auto_update_aus():
        print(f"(Automatische Updates sind ausgeschaltet — Version {installierte_version()}.)")
        return False
    try:
        info = json.loads(_hole(f"https://api.github.com/repos/{REPO}/releases/latest", 8))
        neue_version = str(info["tag_name"]).lstrip("vV")
    except Exception:
        print(f"(Update-Prüfung übersprungen — keine Verbindung zu GitHub. "
              f"Es läuft Version {installierte_version()}.)")
        return False
    if neue_version == installierte_version():
        print(f"✓ Version {neue_version} ist aktuell (nach Updates geschaut, keine neuen).")
        return False

    print("=" * 60)
    print(f"  Neue Version {neue_version} wird installiert ...")
    print("=" * 60)
    notizen = (info.get("body") or "").strip()
    if notizen:
        print("Neu in dieser Version:")
        for zeile in notizen.splitlines()[:15]:
            print(f"  {zeile}")
        print()

    try:
        dateien = _dateien_aus_zip(_hole(info["zipball_url"], 180))
    except Exception as e:
        print(f"[!] Download fehlgeschlagen ({str(e)[:100]}). Es geht mit der bisherigen Version weiter.")
        return False
    if "app.py" not in dateien:
        print("[!] Das Update ist unvollständig. Es geht mit der bisherigen Version weiter.")
        return False

    alte_requirements = (APP / "requirements.txt").read_bytes() if (APP / "requirements.txt").exists() else b""
    shutil.rmtree(SICHERUNG, ignore_errors=True)
    geaendert, neu_angelegt = [], []
    try:
        for rel, inhalt in sorted(dateien.items()):
            if rel.split("/")[0] in GESCHUETZT:
                continue
            ziel = APP / rel
            if ziel.exists():
                if ziel.read_bytes() == inhalt:
                    continue
                (SICHERUNG / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ziel, SICHERUNG / rel)
                geaendert.append(rel)
            else:
                neu_angelegt.append(rel)
            ziel.parent.mkdir(parents=True, exist_ok=True)
            zwischen = ziel.with_name(ziel.name + ".update")
            zwischen.write_bytes(inhalt)
            os.replace(zwischen, ziel)

        if dateien.get("requirements.txt", alte_requirements) != alte_requirements:
            print("Neue Programm-Bausteine werden installiert (kann ein paar Minuten dauern) ...")
            erg = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet",
                 "--disable-pip-version-check"],
                cwd=APP,
            )
            if erg.returncode != 0:
                raise RuntimeError("Installation der Programm-Bausteine fehlgeschlagen")
    except Exception as e:
        print(f"[!] Update fehlgeschlagen ({str(e)[:150]}).")
        print("    Die bisherige Version wird wiederhergestellt ...")
        _wiederherstellen(geaendert, neu_angelegt)
        return False

    VERSION_DATEI.write_text(neue_version, encoding="utf-8")
    print(f"✓ Version {neue_version} ist installiert.")
    print()
    return True


if __name__ == "__main__":
    pruefe_und_aktualisiere()

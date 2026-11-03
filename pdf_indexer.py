"""Einlesen der Unterlagen (PDF/DOCX/TXT) aus einem oder mehreren Ordnern.

Zwei Stufen, damit der Assistent nie mit leerer Datenbank dasteht:

  Stufe 1 (beim Start, schnell): Dateien lesen, Textabschnitte in den
      Textspeicher schreiben → sofort per Stichwortsuche auffindbar.
      Scans werden dabei nur aus dem Zwischenspeicher genommen; noch nicht
      erkannte Scans kommen in eine Warteschlange.

  Stufe 2 (im Hintergrund, während man schon arbeiten kann):
      a) Texterkennung der wartenden Scans über Gemini (Tageslimit → morgen weiter)
      b) Suchvektoren lokal über Ollama (qwen3-embedding, kein Limit)

Neben dem Hauptordner (PDF_ORDNER aus der .env) kann der Nutzer weitere Ordner
hinzufügen und jeden Ordner ein- und ausschalten. Eingelesen werden immer alle
Ordner; das Ein/Aus wirkt nur als Filter bei der Suche.
"""
import hashlib
import json
import os
import shutil
import threading
import time
from datetime import date
from pathlib import Path

import pymupdf as fitz  # PyMuPDF (der alte Importname "fitz" wird abgeschafft)
from docx import Document

import config
import gemini_ki
import textspeicher
import vektoren

MANIFEST_VERSION = 3
MIN_TEXT_PRO_SEITE = 60
MAX_BYTES_PRO_ANFRAGE = 15 * 1024 * 1024
VEKTOR_PAKET = 16
MAX_OCR_VERSUCHE = 3

# Wird bei jeder Änderung am Text erhöht — die Stichwortsuche baut ihren
# Zwischenspeicher dann neu auf.
INDEX_VERSION = 0

_manifest_lock = threading.RLock()

textspeicher.init()


class ScanWartet(Exception):
    """Interne Markierung: Datei enthält Scan-Seiten, die noch erkannt werden müssen."""


# ── Ordner-Verwaltung ─────────────────────────────────────────────────────────

def _norm(pfad) -> str:
    """Vergleichbare Form eines Pfads (Windows: Groß/Klein egal)."""
    return os.path.normcase(os.path.abspath(str(pfad)))


def _hauptordner() -> str | None:
    roh = os.getenv("PDF_ORDNER", "").strip()
    return os.path.abspath(roh) if roh else None


def _lade_ordner_datei() -> dict:
    if config.ORDNER_LISTE_PFAD.exists():
        try:
            return json.loads(config.ORDNER_LISTE_PFAD.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"haupt_aktiv": True, "zusatz": []}


def _speichere_ordner_datei(daten: dict):
    config.ORDNER_LISTE_PFAD.write_text(
        json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def lade_ordner() -> list[dict]:
    """Alle registrierten Ordner: [{pfad, aktiv, haupt, erreichbar}], Hauptordner zuerst."""
    daten = _lade_ordner_datei()
    liste = []
    haupt = _hauptordner()
    if haupt:
        liste.append({"pfad": haupt, "aktiv": daten.get("haupt_aktiv", True), "haupt": True})
    for z in daten.get("zusatz", []):
        liste.append({"pfad": z["pfad"], "aktiv": z.get("aktiv", True), "haupt": False})
    for o in liste:
        o["erreichbar"] = Path(o["pfad"]).is_dir()
    return liste


def aktive_ordner() -> list[str]:
    return [o["pfad"] for o in lade_ordner() if o["aktiv"]]


def setze_ordner_aktiv(pfad: str, aktiv: bool):
    daten = _lade_ordner_datei()
    haupt = _hauptordner()
    if haupt and _norm(pfad) == _norm(haupt):
        daten["haupt_aktiv"] = aktiv
    for z in daten.get("zusatz", []):
        if _norm(z["pfad"]) == _norm(pfad):
            z["aktiv"] = aktiv
    _speichere_ordner_datei(daten)


def fuege_ordner_hinzu(pfad: str) -> tuple[bool, str]:
    """Registriert einen zusätzlichen Ordner (eingeschaltet). Gibt (ok, meldung) zurück."""
    pfad = os.path.abspath(pfad.strip().strip('"'))
    if not Path(pfad).is_dir():
        return False, f"Diesen Ordner gibt es nicht: {pfad}"
    neu = Path(_norm(pfad))
    for o in lade_ordner():
        vorhanden = Path(_norm(o["pfad"]))
        if neu == vorhanden:
            return False, "Dieser Ordner ist schon in der Liste."
        if neu.is_relative_to(vorhanden):
            return False, f"Dieser Ordner liegt bereits innerhalb von „{o['pfad']}“ und wird schon mitgelesen."
        if vorhanden.is_relative_to(neu):
            return False, (f"In diesem Ordner liegt bereits „{o['pfad']}“. Bitte den "
                           "anderen Ordner zuerst entfernen oder einen anderen wählen.")
    daten = _lade_ordner_datei()
    daten.setdefault("zusatz", []).append({"pfad": pfad, "aktiv": True})
    _speichere_ordner_datei(daten)
    return True, f"Ordner „{Path(pfad).name}“ wurde hinzugefügt."


def entferne_ordner(pfad: str):
    """Entfernt einen zusätzlichen Ordner samt seinen Einträgen aus dem Index.
    Die Dateien selbst bleiben unangetastet."""
    global INDEX_VERSION
    daten = _lade_ordner_datei()
    daten["zusatz"] = [z for z in daten.get("zusatz", []) if _norm(z["pfad"]) != _norm(pfad)]
    _speichere_ordner_datei(daten)
    vektoren.entferne(textspeicher.entferne_ordner(pfad))
    with _manifest_lock:
        manifest = _lade_manifest()
        manifest["dateien"] = {
            p: m for p, m in manifest["dateien"].items() if _norm(m.get("ordner", "")) != _norm(pfad)
        }
        _speichere_manifest(manifest)
    INDEX_VERSION += 1


def anzahl_eingelesen() -> int:
    """0 = noch nie eingelesen (z. B. direkt nach dem Update)."""
    return len(_lade_manifest()["dateien"])


def dateien_pro_ordner() -> dict[str, int]:
    zaehler: dict[str, int] = {}
    for m in _lade_manifest()["dateien"].values():
        if m.get("status") in ("ok", "ocr_offen"):
            k = _norm(m.get("ordner", ""))
            zaehler[k] = zaehler.get(k, 0) + 1
    return zaehler


# ── Dokument lesen ────────────────────────────────────────────────────────────

def _teile_text(text: str, pfad: Path, seite: int) -> list[dict]:
    chunks = []
    text = text.strip()
    if len(text) < MIN_TEXT_PRO_SEITE:
        return chunks
    schritt = config.MAX_CHUNK_LAENGE - config.CHUNK_UEBERLAPPUNG
    for start in range(0, len(text), schritt):
        stueck = text[start : start + config.MAX_CHUNK_LAENGE].strip()
        if len(stueck) < MIN_TEXT_PRO_SEITE:
            continue
        chunks.append({"text": stueck, "datei": pfad.name, "pfad": str(pfad), "seite": seite})
    return chunks


def _datei_hash(pfad: Path) -> str:
    h = hashlib.sha1()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _seiten_als_pdf(doc, seitennummern: list[int]) -> bytes:
    teil = fitz.open()
    for nr in seitennummern:
        teil.insert_pdf(doc, from_page=nr - 1, to_page=nr - 1)
    daten = teil.tobytes(garbage=3, deflate=True)
    teil.close()
    return daten


def _erkenne_gruppe(doc, gruppe: list[int]) -> dict[int, str]:
    """Schickt eine Gruppe von Scan-Seiten an Gemini. Zu große Gruppen werden
    geteilt; eine einzelne, riesige Seite wird als Bild verkleinert gesendet."""
    daten = _seiten_als_pdf(doc, gruppe)
    if len(daten) > MAX_BYTES_PRO_ANFRAGE:
        if len(gruppe) > 1:
            mitte = len(gruppe) // 2
            return {**_erkenne_gruppe(doc, gruppe[:mitte]), **_erkenne_gruppe(doc, gruppe[mitte:])}
        bild = doc[gruppe[0] - 1].get_pixmap(dpi=150).tobytes("jpeg")
        texte = gemini_ki.erkenne_text(bild, 1, mime="image/jpeg")
    else:
        texte = gemini_ki.erkenne_text(daten, len(gruppe))
    return dict(zip(gruppe, texte))


def _texterkennung(pfad: Path, doc, scan_seiten: list[int], ocr: bool, meldung=None) -> dict[int, str]:
    """Erkannter Text der Scan-Seiten — aus dem Zwischenspeicher oder (wenn ocr=True)
    frisch von Gemini. Bei ocr=False und leerem Zwischenspeicher: ScanWartet."""
    cache_datei = config.OCR_CACHE_ORDNER / f"{_datei_hash(pfad)}.json"
    if cache_datei.exists():
        try:
            gespeichert = json.loads(cache_datei.read_text(encoding="utf-8"))
            return {int(k): v for k, v in gespeichert["seiten"].items()}
        except Exception:
            pass
    if not ocr or not config.GEMINI_API_KEY:
        raise ScanWartet()

    ergebnis: dict[int, str] = {}
    schritt = config.OCR_SEITEN_PRO_ANFRAGE
    for start in range(0, len(scan_seiten), schritt):
        gruppe = scan_seiten[start : start + schritt]
        if meldung:
            meldung(f"Texterkennung {pfad.name}: Seite {gruppe[0]}–{gruppe[-1]} von {len(doc)}")
        ergebnis.update(_erkenne_gruppe(doc, gruppe))

    cache_datei.write_text(
        json.dumps({"datei": str(pfad), "seiten": ergebnis}, ensure_ascii=False), encoding="utf-8"
    )
    return ergebnis


def _lese_pdf(pfad: Path, ocr: bool = True, meldung=None) -> tuple[list[tuple[int, str]], bool]:
    """Gibt ([(seite, text)], scan_wartet) zurück."""
    doc = fitz.open(str(pfad))
    try:
        texte: dict[int, str] = {}
        scan_seiten: list[int] = []
        for nr, seite in enumerate(doc, 1):
            text = seite.get_text()
            texte[nr] = text
            # Seite ohne (nennenswerten) Text, aber mit Bild oder Zeichnung → Scan
            if len(text.strip()) < MIN_TEXT_PRO_SEITE and (seite.get_images() or seite.get_drawings()):
                scan_seiten.append(nr)

        wartet = False
        if scan_seiten:
            try:
                for nr, text in _texterkennung(pfad, doc, scan_seiten, ocr, meldung).items():
                    texte[nr] = text
            except ScanWartet:
                wartet = True
        return sorted(texte.items()), wartet
    finally:
        doc.close()


def _lese_docx(pfad: Path) -> list[tuple[int, str]]:
    """Liest Absätze UND Tabellen in Dokument-Reihenfolge."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(pfad))
    zeilen = []
    for element in doc.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(element, doc).text.strip()
            if text:
                zeilen.append(text)
        elif tag == "tbl":
            for reihe in Table(element, doc).rows:
                zellen = []
                for zelle in reihe.cells:
                    t = zelle.text.strip()
                    if t and (not zellen or zellen[-1] != t):   # verbundene Zellen nur einmal
                        zellen.append(t)
                if zellen:
                    zeilen.append(" | ".join(zellen))
    return [(0, "\n".join(zeilen))]


def _lese_txt(pfad: Path) -> list[tuple[int, str]]:
    return [(0, pfad.read_text(encoding="utf-8-sig", errors="ignore"))]


def _lese(pfad: Path, ocr: bool, meldung=None) -> tuple[list[tuple[int, str]], bool]:
    suffix = pfad.suffix.lower()
    if suffix == ".pdf":
        return _lese_pdf(pfad, ocr, meldung)
    if suffix == ".docx":
        return _lese_docx(pfad), False
    if suffix == ".txt":
        return _lese_txt(pfad), False
    return [], False


def lese_seiten(pfad: Path, meldung=None) -> list[tuple[int, str]]:
    """Vollständiger Text einer Datei als [(seitennummer, text)] — Seite 0 = ohne
    Seitenangabe (Word/Text). Scans werden dabei sofort erkannt (für den Vergleich)."""
    return _lese(pfad, True, meldung)[0]


def _unterordner(pfad: Path, ordner: str) -> str:
    if Path(_norm(pfad)).is_relative_to(_norm(ordner)):
        rel = Path(_norm(pfad)).relative_to(_norm(ordner))
        return str(rel.parent) if str(rel.parent) != "." else ""
    return ""


def _indexiere_datei(pfad: Path, ordner: str, ocr: bool, meldung=None) -> tuple[int, bool]:
    """Liest eine Datei in den Textspeicher. Gibt (abschnitte, scan_wartet) zurück.
    Wartende Scans kommen in die Warteschlange; der Text-Teil ist trotzdem schon da."""
    global INDEX_VERSION
    seiten, wartet = _lese(pfad, ocr, meldung)
    chunks = []
    for nr, text in seiten:
        chunks.extend(_teile_text(text, pfad, nr))
    weg = textspeicher.ersetze_datei(str(pfad), ordner, chunks, _unterordner(pfad, ordner))
    vektoren.entferne(weg)
    if wartet:
        textspeicher.ocr_einreihen(str(pfad), ordner)
    else:
        textspeicher.ocr_erledigt(str(pfad), ordner)
    INDEX_VERSION += 1
    return len(chunks), wartet


def _entferne_datei(pfad: str, ordner: str):
    global INDEX_VERSION
    vektoren.entferne(textspeicher.entferne_datei(pfad, ordner))
    INDEX_VERSION += 1


# ── Manifest (Änderungserkennung) ─────────────────────────────────────────────

def _lade_manifest() -> dict:
    with _manifest_lock:
        if config.MANIFEST_PFAD.exists():
            try:
                daten = json.loads(config.MANIFEST_PFAD.read_text(encoding="utf-8"))
                if daten.get("version") == MANIFEST_VERSION:
                    return daten
            except Exception:
                pass
        # Älteres Format (vor dem Update) oder beschädigt → alles neu einlesen
        return {"version": MANIFEST_VERSION, "dateien": {}}


def _speichere_manifest(manifest: dict):
    with _manifest_lock:
        zwischen = config.MANIFEST_PFAD.with_suffix(".tmp")
        zwischen.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(zwischen, config.MANIFEST_PFAD)


def _manifest_eintrag(pfad: str, info: dict | None):
    """Setzt (oder löscht bei None) einen Eintrag — sicher auch aus dem Hintergrund-Thread."""
    with _manifest_lock:
        m = _lade_manifest()
        if info is None:
            m["dateien"].pop(pfad, None)
        else:
            m["dateien"][pfad] = info
        _speichere_manifest(m)


def _scan_ordner(ordner: str) -> dict:
    gefunden = {}
    for pfad in Path(ordner).rglob("*"):
        if (pfad.suffix.lower() in config.UNTERSTUETZTE_TYPEN
                and not pfad.name.startswith("~$")      # Word-Sperrdateien
                and pfad.is_file()):
            stat = pfad.stat()
            gefunden[str(pfad)] = {"mtime": stat.st_mtime, "size": stat.st_size, "ordner": ordner}
    return gefunden


# ── Quellen nur für ein Gespräch ──────────────────────────────────────────────
# Dateien oder Ordner, die nur in einem bestimmten Gespräch durchsucht werden.
# Sie stehen im selben Textspeicher, aber mit ordner = "gespraech:<id>" — dadurch
# findet sie nur dieses Gespräch. Hochgeladene Dateien werden nach
# beirat_daten/gespraech_dateien/<id>/ kopiert, damit sie auch beim späteren
# Fortsetzen des Gesprächs noch da sind und per Klick geöffnet werden können.

def gespraechs_tag(gespraechs_id: str) -> str:
    return f"gespraech:{gespraechs_id}"


def _lade_gespraech_quellen() -> dict:
    if config.GESPRAECH_QUELLEN_PFAD.exists():
        try:
            return json.loads(config.GESPRAECH_QUELLEN_PFAD.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _speichere_gespraech_quellen(daten: dict):
    config.GESPRAECH_QUELLEN_PFAD.write_text(
        json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def gespraechs_quellen(gespraechs_id: str) -> list[dict]:
    """[{name, pfad, typ ('datei'|'ordner'), dateien: [pfade], abschnitte}]"""
    return _lade_gespraech_quellen().get(gespraechs_id, [])


def speichere_upload(gespraechs_id: str, dateiname: str, inhalt: bytes) -> Path:
    """Legt eine im Browser hochgeladene Datei dauerhaft beim Gespräch ab."""
    ziel_ordner = config.GESPRAECH_DATEIEN_ORDNER / gespraechs_id
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    ziel = ziel_ordner / Path(dateiname).name
    ziel.write_bytes(inhalt)
    return ziel


def fuege_gespraechs_quelle_hinzu(gespraechs_id: str, pfad: str, fortschritt=None, meldung=None) -> dict:
    """Liest eine Datei oder einen ganzen Ordner für ein Gespräch ein — Scans werden
    sofort erkannt (man braucht sie ja gerade). Ist das Tageslimit erreicht, kommen
    sie in die Warteschlange. Gibt {"ok", "ohne_text", "fehler", "scan_wartet"} zurück."""
    quelle = Path(pfad)
    tag = gespraechs_tag(gespraechs_id)
    if quelle.is_dir():
        dateien = sorted(p for p in quelle.rglob("*")
                         if p.suffix.lower() in config.UNTERSTUETZTE_TYPEN
                         and not p.name.startswith("~$") and p.is_file())
    else:
        dateien = [quelle]

    ergebnis = {"ok": [], "ohne_text": [], "fehler": [], "scan_wartet": []}
    eingelesen, abschnitte = [], 0
    for i, datei in enumerate(dateien):
        if fortschritt:
            fortschritt(i, len(dateien), datei.name)
        try:
            try:
                n, wartet = _indexiere_datei(datei, tag, ocr=True, meldung=meldung)
            except gemini_ki.TagesKontingentErschoepft:
                n, wartet = _indexiere_datei(datei, tag, ocr=False, meldung=meldung)
        except Exception as e:
            print(f"  [!] Fehler bei {datei.name}: {str(e)[:200]}")
            gemini_ki.protokolliere(f"DATEI FEHLGESCHLAGEN {datei}: {e}")
            ergebnis["fehler"].append(datei.name)
            continue
        if wartet:
            ergebnis["scan_wartet"].append(datei.name)
        if n or wartet:
            eingelesen.append(str(datei))
            abschnitte += n
            if n:
                ergebnis["ok"].append(datei.name)
        else:
            ergebnis["ohne_text"].append(datei.name)

    if eingelesen:
        daten = _lade_gespraech_quellen()
        liste = [q for q in daten.get(gespraechs_id, []) if _norm(q["pfad"]) != _norm(pfad)]
        liste.append({
            "name": quelle.name, "pfad": str(quelle),
            "typ": "ordner" if quelle.is_dir() else "datei",
            "dateien": eingelesen, "abschnitte": abschnitte,
        })
        daten[gespraechs_id] = liste
        _speichere_gespraech_quellen(daten)
    return ergebnis


def entferne_gespraechs_quelle(gespraechs_id: str, pfad: str):
    daten = _lade_gespraech_quellen()
    tag = gespraechs_tag(gespraechs_id)
    bleibt = []
    for q in daten.get(gespraechs_id, []):
        if _norm(q["pfad"]) == _norm(pfad):
            for datei in q["dateien"]:
                _entferne_datei(datei, tag)
                _loesche_upload_kopie(gespraechs_id, datei)
        else:
            bleibt.append(q)
    if bleibt:
        daten[gespraechs_id] = bleibt
    else:
        daten.pop(gespraechs_id, None)
    _speichere_gespraech_quellen(daten)


def entferne_alle_gespraechs_quellen(gespraechs_id: str):
    """Beim Löschen eines Gesprächs: Index-Einträge und hochgeladene Kopien weg."""
    global INDEX_VERSION
    vektoren.entferne(textspeicher.entferne_ordner(gespraechs_tag(gespraechs_id)))
    daten = _lade_gespraech_quellen()
    if daten.pop(gespraechs_id, None) is not None:
        _speichere_gespraech_quellen(daten)
    shutil.rmtree(config.GESPRAECH_DATEIEN_ORDNER / gespraechs_id, ignore_errors=True)
    INDEX_VERSION += 1


def _loesche_upload_kopie(gespraechs_id: str, datei: str):
    """Löscht eine Datei nur, wenn sie eine hochgeladene Kopie ist — nie Originale."""
    upload_ordner = Path(_norm(config.GESPRAECH_DATEIEN_ORDNER / gespraechs_id))
    if Path(_norm(datei)).is_relative_to(upload_ordner):
        try:
            Path(datei).unlink()
        except Exception:
            pass


def _gespraechs_quellen_nachziehen(meldung=None):
    """Nach einem Neuaufbau: Gesprächsquellen, die im Textspeicher fehlen, neu einlesen
    (Scans aus dem Zwischenspeicher, sonst Warteschlange)."""
    for gid, quellen in _lade_gespraech_quellen().items():
        tag = gespraechs_tag(gid)
        for q in quellen:
            for datei in q["dateien"]:
                if Path(datei).is_file() and not textspeicher.hat_abschnitte(datei, tag):
                    try:
                        _indexiere_datei(Path(datei), tag, ocr=False, meldung=meldung)
                    except Exception as e:
                        gemini_ki.protokolliere(f"GESPRÄCHSQUELLE FEHLGESCHLAGEN {datei}: {e}")


def alle_dokumente(gespraechs_id: str | None = None) -> list[dict]:
    """Alle eingelesenen Dateien zur Auswahl (z. B. für den Vergleich):
    [{name, pfad, herkunft}] — Gesprächsquellen zuerst."""
    liste = []
    if gespraechs_id:
        for q in gespraechs_quellen(gespraechs_id):
            for d in q["dateien"]:
                liste.append({"name": Path(d).name, "pfad": d, "herkunft": "dieses Gespräch"})
    ordner_namen = {_norm(o["pfad"]): Path(o["pfad"]).name for o in lade_ordner()}
    for p, m in sorted(_lade_manifest()["dateien"].items(), key=lambda x: Path(x[0]).name.lower()):
        if m.get("status") in ("ok", "ocr_offen"):
            ordner = m.get("ordner", "")
            liste.append({"name": Path(p).name, "pfad": p,
                          "herkunft": ordner_namen.get(_norm(ordner), Path(ordner).name)})
    return liste


# ── Stufe 1: Unterlagen prüfen und in den Textspeicher lesen ──────────────────

def pruefe_und_aktualisiere(fortschritt=None, meldung=None, nur_ordner: str | None = None) -> dict:
    """Prüft alle registrierten Ordner auf Änderungen und liest neue/geänderte
    Dateien in den Textspeicher (schnell, ohne KI). Scans ohne Zwischenspeicher
    kommen in die Warteschlange für Stufe 2.

    fortschritt(i, gesamt, dateiname) — vor jeder Datei
    meldung(text)                     — Detailmeldungen
    nur_ordner                        — nur diesen Ordner prüfen (nach dem Hinzufügen)
    """
    alt = _lade_manifest()["dateien"]
    ordner_liste = lade_ordner()
    bekannte = {_norm(o["pfad"]) for o in ordner_liste}
    status = {"neu": 0, "aktualisiert": 0, "geloescht": 0,
              "ohne_text": [], "fehler": [], "unerreichbar": []}

    # 1. Einträge von Ordnern, die nicht mehr registriert sind → entfernen
    for p, m in list(alt.items()):
        if _norm(m.get("ordner", "")) not in bekannte:
            _entferne_datei(p, m.get("ordner", ""))
            _manifest_eintrag(p, None)
            status["geloescht"] += 1

    # 2. Aktuellen Stand der erreichbaren Ordner ermitteln
    zu_tun: list[tuple[Path, dict, bool]] = []
    for o in ordner_liste:
        if nur_ordner and _norm(o["pfad"]) != _norm(nur_ordner):
            continue
        if not o["erreichbar"]:
            # z. B. USB-Stick abgezogen: vorhandene Einträge bleiben erhalten
            status["unerreichbar"].append(o["pfad"])
            continue
        jetzt = _scan_ordner(o["pfad"])
        for p, m in list(alt.items()):
            if _norm(m.get("ordner", "")) == _norm(o["pfad"]) and p not in jetzt:
                _entferne_datei(p, m.get("ordner", ""))
                _manifest_eintrag(p, None)
                status["geloescht"] += 1
        for p, info in jetzt.items():
            vorher = alt.get(p)
            if vorher is None:
                zu_tun.append((Path(p), info, True))
            elif ((vorher["mtime"], vorher["size"]) != (info["mtime"], info["size"])
                  or vorher.get("status") == "fehler"):
                zu_tun.append((Path(p), info, False))

    # 3. Neue/geänderte Dateien einlesen (Textdateien zuerst — Scans dauern länger)
    zu_tun.sort(key=lambda x: x[0].suffix.lower() == ".pdf")
    if zu_tun:
        gemini_ki.protokolliere(f"EINLESEN START: {len(zu_tun)} Datei(en)")
    for i, (pfad, info, ist_neu) in enumerate(zu_tun):
        if fortschritt:
            fortschritt(i, len(zu_tun), pfad.name)
        try:
            anzahl, wartet = _indexiere_datei(pfad, info["ordner"], ocr=False, meldung=meldung)
            info.update(status="ocr_offen" if wartet else ("ok" if anzahl else "ohne_text"),
                        abschnitte=anzahl)
        except Exception as e:
            print(f"  [!] Fehler bei {pfad.name}: {str(e)[:200]}")
            gemini_ki.protokolliere(f"DATEI FEHLGESCHLAGEN {pfad}: {e}")
            info.update(status="fehler", abschnitte=0, fehlertext=str(e)[:300])
        _manifest_eintrag(str(pfad), info)
        status["neu" if ist_neu else "aktualisiert"] += 1

    if not nur_ordner:
        _gespraechs_quellen_nachziehen(meldung)
        vektoren.raeume_alte_auf()   # Textspeicher ist jetzt befüllt

    for p, m in _lade_manifest()["dateien"].items():
        if m.get("status") == "ohne_text":
            status["ohne_text"].append(Path(p).name)
        elif m.get("status") == "fehler":
            status["fehler"].append(Path(p).name)
    status["gesamt"], _ = textspeicher.zaehle()
    if zu_tun:
        gemini_ki.protokolliere(
            f"EINLESEN ENDE: neu {status['neu']}, geändert {status['aktualisiert']}, "
            f"Fehler {len(status['fehler'])}, Scans in Warteschlange {textspeicher.ocr_anzahl()}")
    return status


# ── Stufe 2: Texterkennung und Suchvektoren im Hintergrund ───────────────────

HINTERGRUND = {
    "laeuft": False,
    "aufgabe": "",          # was gerade passiert (für die Anzeige)
    "hinweis": "",          # z. B. Ollama nicht erreichbar, Tageslimit
}
_hintergrund_lock = threading.Lock()
_thread: threading.Thread | None = None


def _setze(**werte):
    with _hintergrund_lock:
        HINTERGRUND.update(werte)


def fortschritt_stufe2() -> dict:
    gesamt, mit_vektor = textspeicher.zaehle()
    with _hintergrund_lock:
        return {**HINTERGRUND, "abschnitte": gesamt, "mit_vektor": mit_vektor,
                "scans_offen": textspeicher.ocr_anzahl()}


def _ocr_heute_gesperrt() -> bool:
    return textspeicher.meta("ocr_gesperrt_am") == date.today().isoformat()


def _ocr_schritt(meldung=None) -> bool:
    """Erkennt EINE wartende Datei. Gibt False zurück, wenn nichts zu tun ist."""
    if _ocr_heute_gesperrt() or not config.GEMINI_API_KEY:
        return False
    eintrag = textspeicher.ocr_naechster()
    if not eintrag:
        return False
    pfad, ordner, versuche = eintrag
    if not Path(pfad).is_file():
        textspeicher.ocr_erledigt(pfad, ordner)
        return True
    _setze(aufgabe=f"Texterkennung: {Path(pfad).name}")
    try:
        anzahl, wartet = _indexiere_datei(Path(pfad), ordner, ocr=True, meldung=meldung)
    except gemini_ki.TagesKontingentErschoepft:
        textspeicher.setze_meta("ocr_gesperrt_am", date.today().isoformat())
        _setze(hinweis="Tageslimit der kostenlosen KI für die Texterkennung erreicht — "
                       "die übrigen Scans werden morgen gelesen.")
        gemini_ki.protokolliere("TEXTERKENNUNG: Tageslimit erreicht, Rest morgen")
        return False
    except Exception as e:
        gemini_ki.protokolliere(f"TEXTERKENNUNG FEHLGESCHLAGEN {pfad}: {e}")
        textspeicher.ocr_fehlversuch(pfad, ordner)
        if versuche + 1 >= MAX_OCR_VERSUCHE:
            textspeicher.ocr_erledigt(pfad, ordner)
            _status_im_manifest(pfad, ordner, "fehler")
        return True
    if not wartet:
        _status_im_manifest(pfad, ordner, "ok" if anzahl else "ohne_text", anzahl)
    return True


def _status_im_manifest(pfad: str, ordner: str, status: str, abschnitte: int | None = None):
    if ordner.startswith("gespraech:"):
        return
    with _manifest_lock:
        info = _lade_manifest()["dateien"].get(pfad)
        if info:
            info["status"] = status
            if abschnitte is not None:
                info["abschnitte"] = abschnitte
            _manifest_eintrag(pfad, info)


_ollama_ok = False


def _vektor_schritt(meldung=None) -> int:
    """Berechnet Suchvektoren für das nächste Paket. Gibt die Anzahl zurück,
    0 = nichts zu tun, -1 = Ollama nicht verfügbar."""
    global _ollama_ok
    paket = textspeicher.ohne_vektor(VEKTOR_PAKET)
    if not paket:
        return 0
    if not _ollama_ok:
        _setze(aufgabe="Suchmodell wird vorbereitet …")
        ok, text = vektoren.bereit(lambda t: _setze(aufgabe=t))
        if not ok:
            _setze(hinweis=text, aufgabe="")
            return -1
        _ollama_ok = True
        _setze(hinweis="")
    gesamt, mit = textspeicher.zaehle()
    _setze(aufgabe=f"Suchindex: {mit} von {gesamt} Abschnitten")
    try:
        vek = vektoren.bette_ein([t for _, t, _, _ in paket])
    except vektoren.OllamaNichtVerfuegbar as e:
        _ollama_ok = False
        _setze(hinweis="Verbindung zu Ollama unterbrochen — die Suche nutzt bis dahin nur Stichworte.")
        gemini_ki.protokolliere(f"OLLAMA: {e}")
        return -1
    ids = [cid for cid, _, _, _ in paket]
    vektoren.speichere(ids, vek, [{"pfad": p, "ordner": o} for _, _, p, o in paket])
    verschwunden = textspeicher.markiere_vektor([(cid, t) for cid, t, _, _ in paket])
    vektoren.entferne(verschwunden)   # Datei wurde währenddessen entfernt
    if meldung and (mit // 160) != ((mit + len(paket)) // 160):
        meldung(f"Suchindex: {mit + len(paket)} von {gesamt} Abschnitten")
    return len(paket)


def arbeite_stufe2(meldung=None, stop: threading.Event | None = None) -> dict:
    """Erledigt Texterkennung und Suchvektoren, bis alles fertig ist (oder ein Limit
    greift bzw. Ollama fehlt). Kann jederzeit abgebrochen werden — es geht beim
    nächsten Mal dort weiter."""
    _setze(laeuft=True)
    try:
        while not (stop and stop.is_set()):
            if _ocr_schritt(meldung):
                continue
            n = _vektor_schritt(meldung)
            if n <= 0:
                break
    finally:
        _setze(laeuft=False, aufgabe="")
    return fortschritt_stufe2()


def starte_hintergrund():
    """Startet Stufe 2 in einem Hintergrund-Thread, falls Arbeit da ist und er
    nicht schon läuft. Wird bei jedem Seitenaufbau der App aufgerufen."""
    global _thread
    with _hintergrund_lock:
        if _thread and _thread.is_alive():
            return
    f = fortschritt_stufe2()
    scans = f["scans_offen"] and not _ocr_heute_gesperrt()
    vektoren_fehlen = f["mit_vektor"] < f["abschnitte"]
    if not (scans or vektoren_fehlen):
        return
    if not scans and f["hinweis"] and HINTERGRUND.get("_letzter_versuch", 0) > time.time() - 60:
        return   # Ollama fehlte gerade erst — nicht bei jedem Klick neu versuchen
    HINTERGRUND["_letzter_versuch"] = time.time()
    _thread = threading.Thread(target=arbeite_stufe2, name="beirat-hintergrund", daemon=True)
    _thread.start()


# ── Konsole (einrichten.py, update_nach_kopieren.bat) ─────────────────────────

def erststart_indexierung():
    """Einlesen mit Fortschritt im Konsolenfenster. Stufe 1 komplett, dann Stufe 2
    so weit wie möglich. Das Fenster darf jederzeit geschlossen werden — der Rest
    läuft später im Hintergrund der App weiter."""
    ordner = lade_ordner()
    if not ordner:
        print("[!] Kein Unterlagen-Ordner eingerichtet (PDF_ORDNER in der .env).")
        return
    for o in ordner:
        if not o["erreichbar"]:
            print(f"[!] Ordner nicht erreichbar (wird übersprungen): {o['pfad']}")

    def fortschritt(i, gesamt, name):
        print(f"  [{i + 1:3d}/{gesamt}] {name[:60]}", flush=True)

    def meldung(text):
        print(f"        {text}", flush=True)

    print("Stufe 1: Unterlagen lesen ...")
    status = pruefe_und_aktualisiere(fortschritt, meldung)
    print(f"✓ {status['gesamt']} Textabschnitte gespeichert — ab jetzt per Stichwort durchsuchbar.")
    if status["fehler"]:
        print(f"[!] Nicht einlesbar: {', '.join(status['fehler'])}")
    print()
    print("Stufe 2: Scans lesen und Suchindex aufbauen ...")
    print("(Dieses Fenster darf jederzeit geschlossen werden — der Rest läuft dann")
    print(" automatisch im Hintergrund weiter, während der Assistent benutzt wird.)")
    print()
    f = arbeite_stufe2(meldung)
    print()
    print(f"✓ Suchindex: {f['mit_vektor']} von {f['abschnitte']} Abschnitten fertig.")
    if f["scans_offen"]:
        print(f"    {f['scans_offen']} Scan(s) warten noch auf die Texterkennung.")
    if f["hinweis"]:
        print(f"[!] {f['hinweis']}")
    if status["fehler"] or f["hinweis"]:
        print(f"    Genaue Fehlermeldungen (für die Fehlersuche): {config.LOG_PFAD}")

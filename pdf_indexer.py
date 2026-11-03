"""Einlesen der Unterlagen (PDF/DOCX/TXT) aus einem oder mehreren Ordnern in ChromaDB.

- Eingescannte PDF-Seiten ohne Textebene werden über Gemini gelesen (Texterkennung);
  das Ergebnis wird in beirat_daten/ocr_cache gespeichert, damit jede Datei nur
  einmal erkannt werden muss.
- Suchvektoren kommen vom mehrsprachigen Gemini-Embedding-Modell.
- Neben dem Hauptordner (PDF_ORDNER aus der .env) kann der Nutzer weitere Ordner
  hinzufügen und jeden Ordner ein- und ausschalten. Eingelesen werden immer alle
  Ordner; das Ein/Aus wirkt nur als Filter bei der Suche — Umschalten geht dadurch
  sofort, ohne erneutes Einlesen.
"""
import hashlib
import json
import os
from pathlib import Path

import chromadb
import pymupdf as fitz  # PyMuPDF (der alte Importname "fitz" wird abgeschafft)
from docx import Document

import config
import gemini_ki

COLLECTION_NAME = "beirat_dokumente_v2"
_ALTE_COLLECTION = "beirat_dokumente"   # englisches Standardmodell, vor dem Update
MANIFEST_VERSION = 2
MIN_TEXT_PRO_SEITE = 60
MAX_BYTES_PRO_ANFRAGE = 15 * 1024 * 1024

# Wird bei jeder Änderung am Index erhöht — die Stichwortsuche baut ihren
# Zwischenspeicher dann neu auf.
INDEX_VERSION = 0


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


def entferne_ordner(collection, pfad: str):
    """Entfernt einen zusätzlichen Ordner samt seinen Einträgen aus dem Index.
    Die Dateien selbst bleiben unangetastet."""
    global INDEX_VERSION
    daten = _lade_ordner_datei()
    daten["zusatz"] = [z for z in daten.get("zusatz", []) if _norm(z["pfad"]) != _norm(pfad)]
    _speichere_ordner_datei(daten)
    try:
        collection.delete(where={"ordner": {"$eq": pfad}})
    except Exception:
        pass
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
        if m.get("status") == "ok":
            zaehler[_norm(m.get("ordner", ""))] = zaehler.get(_norm(m.get("ordner", "")), 0) + 1
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


def _texterkennung(pfad: Path, doc, scan_seiten: list[int], meldung=None) -> dict[int, str]:
    """Liefert den erkannten Text der Scan-Seiten — aus dem Zwischenspeicher oder
    frisch von Gemini. Fehler (außer leeren Seiten) werden weitergereicht, damit die
    Datei beim nächsten Start erneut versucht wird."""
    cache_datei = config.OCR_CACHE_ORDNER / f"{_datei_hash(pfad)}.json"
    if cache_datei.exists():
        try:
            gespeichert = json.loads(cache_datei.read_text(encoding="utf-8"))
            return {int(k): v for k, v in gespeichert["seiten"].items()}
        except Exception:
            pass

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


def _lese_pdf(pfad: Path, meldung=None) -> list[tuple[int, str]]:
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

        if scan_seiten and config.GEMINI_API_KEY:
            for nr, text in _texterkennung(pfad, doc, scan_seiten, meldung).items():
                texte[nr] = text
        return sorted(texte.items())
    finally:
        doc.close()


def _lese_docx(pfad: Path) -> list[tuple[int, str]]:
    """Liest Absätze UND Tabellen in Dokument-Reihenfolge (vorher fehlten Tabellen,
    z. B. bei To-do-Listen)."""
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


def lese_seiten(pfad: Path, meldung=None) -> list[tuple[int, str]]:
    """Vollständiger Text einer Datei als [(seitennummer, text)] — Seite 0 = ohne
    Seitenangabe (Word/Text). Scans werden dabei erkannt (bzw. aus dem Cache geholt)."""
    suffix = pfad.suffix.lower()
    if suffix == ".pdf":
        return _lese_pdf(pfad, meldung)
    if suffix == ".docx":
        return _lese_docx(pfad)
    if suffix == ".txt":
        return _lese_txt(pfad)
    return []


def lese_datei(pfad: Path, meldung=None) -> list[dict]:
    chunks = []
    for nr, text in lese_seiten(pfad, meldung):
        chunks.extend(_teile_text(text, pfad, nr))
    return chunks


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def erstelle_client():
    client = chromadb.PersistentClient(path=str(config.CHROMA_PFAD))
    try:
        client.delete_collection(_ALTE_COLLECTION)   # einmalige Aufräumaktion nach dem Update
    except Exception:
        pass
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
        embedding_function=None,   # Vektoren kommen von Gemini (gemini_ki.bette_ein)
    )
    return client, collection


def _chunk_id(ordner: str, pfad: str, index: int) -> str:
    # Ordner gehört dazu: dieselbe Datei kann zusätzlich als Gesprächsquelle
    # eingelesen sein, ohne den Eintrag aus dem Unterlagen-Ordner zu überschreiben.
    return hashlib.md5(f"{ordner}::{pfad}::{index}".encode()).hexdigest()


def _entferne_datei(collection, pfad: str, ordner: str):
    try:
        collection.delete(where={"$and": [{"pfad": {"$eq": pfad}}, {"ordner": {"$eq": ordner}}]})
    except Exception:
        pass


def _indexiere_datei(collection, pfad: Path, ordner: str, meldung=None) -> int:
    """Liest eine Datei ein und ersetzt ihre bisherigen Einträge. Gibt die Anzahl
    der Textabschnitte zurück (0 = kein lesbarer Text)."""
    chunks = lese_datei(pfad, meldung)
    if not chunks:
        _entferne_datei(collection, str(pfad), ordner)
        return 0

    if meldung:
        meldung(f"Suchindex für {pfad.name} ({len(chunks)} Abschnitte)")
    texte = [c["text"] for c in chunks]
    vektoren = gemini_ki.bette_ein(texte, meldung=meldung)

    rel = Path(_norm(pfad)).relative_to(_norm(ordner)) if Path(_norm(pfad)).is_relative_to(_norm(ordner)) else Path(pfad.name)
    unterordner = str(rel.parent) if str(rel.parent) != "." else ""

    _entferne_datei(collection, str(pfad), ordner)
    ids = [_chunk_id(ordner, str(pfad), i) for i in range(len(chunks))]
    metadaten = [
        {"datei": c["datei"], "pfad": c["pfad"], "seite": c["seite"],
         "unterordner": unterordner, "ordner": ordner}
        for c in chunks
    ]
    for s in range(0, len(chunks), 500):
        collection.upsert(
            ids=ids[s : s + 500], documents=texte[s : s + 500],
            embeddings=vektoren[s : s + 500], metadatas=metadaten[s : s + 500],
        )
    return len(chunks)


# ── Manifest (Änderungserkennung) ─────────────────────────────────────────────

def _lade_manifest() -> dict:
    if config.MANIFEST_PFAD.exists():
        try:
            daten = json.loads(config.MANIFEST_PFAD.read_text(encoding="utf-8"))
            if daten.get("version") == MANIFEST_VERSION:
                return daten
        except Exception:
            pass
    # Altes Format (vor dem Update) oder beschädigt → alles neu einlesen
    return {"version": MANIFEST_VERSION, "dateien": {}}


def _speichere_manifest(manifest: dict):
    zwischen = config.MANIFEST_PFAD.with_suffix(".tmp")
    zwischen.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(zwischen, config.MANIFEST_PFAD)


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
# Sie stehen im selben Suchindex, aber mit ordner = "gespraech:<id>" — dadurch
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


def fuege_gespraechs_quelle_hinzu(collection, gespraechs_id: str, pfad: str,
                                  fortschritt=None, meldung=None) -> dict:
    """Liest eine Datei oder einen ganzen Ordner für ein Gespräch ein.
    Gibt {"ok": [...], "ohne_text": [...], "fehler": [...]} (Dateinamen) zurück."""
    global INDEX_VERSION
    quelle = Path(pfad)
    tag = gespraechs_tag(gespraechs_id)
    if quelle.is_dir():
        dateien = sorted(p for p in quelle.rglob("*")
                         if p.suffix.lower() in config.UNTERSTUETZTE_TYPEN
                         and not p.name.startswith("~$") and p.is_file())
    else:
        dateien = [quelle]

    ergebnis = {"ok": [], "ohne_text": [], "fehler": []}
    eingelesen, abschnitte = [], 0
    for i, datei in enumerate(dateien):
        if fortschritt:
            fortschritt(i, len(dateien), datei.name)
        try:
            n = _indexiere_datei(collection, datei, tag, meldung)
        except Exception as e:
            print(f"  [!] Fehler bei {datei.name}: {str(e)[:200]}")
            gemini_ki.protokolliere(f"DATEI FEHLGESCHLAGEN {datei}: {e}")
            ergebnis["fehler"].append(datei.name)
            continue
        if n:
            ergebnis["ok"].append(datei.name)
            eingelesen.append(str(datei))
            abschnitte += n
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
        INDEX_VERSION += 1
    return ergebnis


def entferne_gespraechs_quelle(collection, gespraechs_id: str, pfad: str):
    global INDEX_VERSION
    daten = _lade_gespraech_quellen()
    tag = gespraechs_tag(gespraechs_id)
    bleibt = []
    for q in daten.get(gespraechs_id, []):
        if _norm(q["pfad"]) == _norm(pfad):
            for datei in q["dateien"]:
                _entferne_datei(collection, datei, tag)
                _loesche_upload_kopie(gespraechs_id, datei)
        else:
            bleibt.append(q)
    if bleibt:
        daten[gespraechs_id] = bleibt
    else:
        daten.pop(gespraechs_id, None)
    _speichere_gespraech_quellen(daten)
    INDEX_VERSION += 1


def entferne_alle_gespraechs_quellen(collection, gespraechs_id: str):
    """Beim Löschen eines Gesprächs: Index-Einträge und hochgeladene Kopien weg."""
    global INDEX_VERSION
    try:
        collection.delete(where={"ordner": {"$eq": gespraechs_tag(gespraechs_id)}})
    except Exception:
        pass
    daten = _lade_gespraech_quellen()
    if daten.pop(gespraechs_id, None) is not None:
        _speichere_gespraech_quellen(daten)
    import shutil
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
        if m.get("status") == "ok":
            ordner = m.get("ordner", "")
            liste.append({"name": Path(p).name, "pfad": p,
                          "herkunft": ordner_namen.get(_norm(ordner), Path(ordner).name)})
    return liste


# ── Öffentliche API ───────────────────────────────────────────────────────────

def pruefe_und_aktualisiere(collection, fortschritt=None, meldung=None, nur_ordner: str | None = None) -> dict:
    """Prüft alle registrierten Ordner auf Änderungen und aktualisiert den Index.

    fortschritt(i, gesamt, dateiname) — vor jeder Datei
    meldung(text)                     — Detailmeldungen (Texterkennung usw.)
    nur_ordner                        — nur diesen Ordner prüfen (nach dem Hinzufügen)

    Bricht bei aufgebrauchtem Tageskontingent sauber ab; die restlichen Dateien
    werden beim nächsten Start eingelesen. Der Fortschritt wird nach jeder Datei
    gespeichert — ein Abbruch (Fenster geschlossen) verliert nichts.
    """
    global INDEX_VERSION
    manifest = _lade_manifest()
    alt = manifest["dateien"]
    ordner_liste = lade_ordner()
    bekannte = {_norm(o["pfad"]) for o in ordner_liste}

    status = {"neu": 0, "aktualisiert": 0, "geloescht": 0, "offen": 0,
              "ohne_text": [], "fehler": [], "unerreichbar": [], "kontingent": False}

    # 1. Einträge von Ordnern, die nicht mehr registriert sind → entfernen
    for p in [p for p, m in alt.items() if _norm(m.get("ordner", "")) not in bekannte]:
        _entferne_datei(collection, p, alt[p].get("ordner", ""))
        del alt[p]
        status["geloescht"] += 1

    # 2. Aktuellen Stand der erreichbaren Ordner einlesen
    zu_tun: list[tuple[Path, dict, bool]] = []
    for o in ordner_liste:
        if nur_ordner and _norm(o["pfad"]) != _norm(nur_ordner):
            continue
        if not o["erreichbar"]:
            # z. B. USB-Stick abgezogen: vorhandene Einträge bleiben erhalten
            status["unerreichbar"].append(o["pfad"])
            continue
        jetzt = _scan_ordner(o["pfad"])
        for p in [p for p, m in alt.items()
                  if _norm(m.get("ordner", "")) == _norm(o["pfad"]) and p not in jetzt]:
            _entferne_datei(collection, p, alt[p].get("ordner", ""))
            del alt[p]
            status["geloescht"] += 1
        for p, info in jetzt.items():
            vorher = alt.get(p)
            if vorher is None:
                zu_tun.append((Path(p), info, True))
            elif (vorher["mtime"], vorher["size"]) != (info["mtime"], info["size"]) \
                    or vorher.get("status") == "fehler":
                zu_tun.append((Path(p), info, False))
    if status["geloescht"]:
        _speichere_manifest(manifest)
        INDEX_VERSION += 1

    # 3. Neue/geänderte/fehlgeschlagene Dateien einlesen
    if zu_tun:
        gemini_ki.protokolliere(f"EINLESEN START: {len(zu_tun)} Datei(en)")
    for i, (pfad, info, ist_neu) in enumerate(zu_tun):
        if fortschritt:
            fortschritt(i, len(zu_tun), pfad.name)
        try:
            anzahl = _indexiere_datei(collection, pfad, info["ordner"], meldung)
            info.update(status="ok" if anzahl else "ohne_text", abschnitte=anzahl)
        except gemini_ki.TagesKontingentErschoepft:
            status["kontingent"] = True
            status["offen"] = len(zu_tun) - i
            break
        except Exception as e:
            print(f"  [!] Fehler bei {pfad.name}: {str(e)[:200]}")
            gemini_ki.protokolliere(f"DATEI FEHLGESCHLAGEN {pfad}: {e}")
            info.update(status="fehler", abschnitte=0, fehlertext=str(e)[:300])
        alt[str(pfad)] = info
        status["neu" if ist_neu else "aktualisiert"] += 1
        _speichere_manifest(manifest)
        INDEX_VERSION += 1

    for p, m in alt.items():
        if m.get("status") == "ohne_text":
            status["ohne_text"].append(Path(p).name)
        elif m.get("status") == "fehler":
            status["fehler"].append(Path(p).name)
    status["gesamt"] = collection.count()
    if zu_tun:
        gemini_ki.protokolliere(
            f"EINLESEN ENDE: neu {status['neu']}, geändert {status['aktualisiert']}, "
            f"offen {status['offen']}, Fehler {len(status['fehler'])}, Tageslimit {status['kontingent']}")
    return status


def erststart_indexierung():
    """Einlesen mit Fortschritt im Konsolenfenster — für einrichten.py und
    update_nach_kopieren.bat (das erste Einlesen nach dem Update kann dauern)."""
    _, collection = erstelle_client()
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

    status = pruefe_und_aktualisiere(collection, fortschritt, meldung)
    print()
    print(f"✓ {status['gesamt']} Textabschnitte im Suchindex.")
    if status["kontingent"]:
        print(f"[!] Das Tageslimit der kostenlosen KI-Stufe ist erreicht. {status['offen']} Datei(en)")
        print("    werden automatisch beim nächsten Start (morgen) eingelesen.")
    if status["ohne_text"]:
        print(f"    Ohne lesbaren Text: {', '.join(status['ohne_text'])}")
    if status["fehler"]:
        print(f"[!] Nicht einlesbar (wird beim nächsten Start erneut versucht): {', '.join(status['fehler'])}")
    if status["fehler"] or status["kontingent"]:
        print(f"    Genaue Fehlermeldungen (für die Fehlersuche): {config.LOG_PFAD}")

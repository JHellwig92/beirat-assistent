"""Textspeicher (SQLite): alle Textabschnitte der Unterlagen.

Stufe 1 des Einlesens schreibt die Abschnitte sofort hierher — damit sind sie
per Stichwortsuche auffindbar, noch bevor die Suchvektoren (Stufe 2, im
Hintergrund) berechnet sind. Die Spalte `vektor` merkt sich, ob ein Abschnitt
schon einen Suchvektor hat. Außerdem: Warteschlange für die Texterkennung.

Jede Funktion öffnet ihre eigene Verbindung — so kann der Hintergrund-Thread
gefahrlos parallel arbeiten.
"""
import hashlib
import sqlite3
from contextlib import closing

import config

_PFAD = config.DATEN_ORDNER / "abschnitte.sqlite3"


def _verbindung():
    con = sqlite3.connect(_PFAD, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with closing(_verbindung()) as con, con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS abschnitte (
                id TEXT PRIMARY KEY, pfad TEXT, ordner TEXT, datei TEXT,
                seite INTEGER, unterordner TEXT, text TEXT, vektor INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_datei ON abschnitte(pfad, ordner);
            CREATE INDEX IF NOT EXISTS idx_vektor ON abschnitte(vektor);
            CREATE TABLE IF NOT EXISTS ocr_warteschlange (
                pfad TEXT, ordner TEXT, versuche INTEGER DEFAULT 0, PRIMARY KEY (pfad, ordner));
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)


def abschnitt_id(ordner: str, pfad: str, index: int) -> str:
    # Ordner gehört dazu: dieselbe Datei kann zusätzlich als Gesprächsquelle
    # eingelesen sein, ohne den Eintrag aus dem Unterlagen-Ordner zu überschreiben.
    return hashlib.md5(f"{ordner}::{pfad}::{index}".encode()).hexdigest()


def ersetze_datei(pfad: str, ordner: str, chunks: list[dict], unterordner: str = "") -> list[str]:
    """Ersetzt alle Abschnitte einer Datei. Gibt die IDs zurück, die es nicht mehr
    gibt (deren Suchvektoren müssen weg). Geänderte Abschnitte bekommen vektor=0."""
    neue = {}
    for i, c in enumerate(chunks):
        neue[abschnitt_id(ordner, pfad, i)] = c
    with closing(_verbindung()) as con, con:
        alt = dict(con.execute(
            "SELECT id, text FROM abschnitte WHERE pfad=? AND ordner=?", (pfad, ordner)))
        weg = [i for i in alt if i not in neue]
        con.executemany("DELETE FROM abschnitte WHERE id=?", [(i,) for i in weg])
        for cid, c in neue.items():
            if alt.get(cid) == c["text"]:
                continue   # unverändert → Vektor bleibt gültig
            con.execute(
                "INSERT OR REPLACE INTO abschnitte VALUES (?,?,?,?,?,?,?,0)",
                (cid, pfad, ordner, c["datei"], c["seite"], unterordner, c["text"]))
    return weg


def entferne_datei(pfad: str, ordner: str) -> list[str]:
    with closing(_verbindung()) as con, con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM abschnitte WHERE pfad=? AND ordner=?", (pfad, ordner))]
        con.execute("DELETE FROM abschnitte WHERE pfad=? AND ordner=?", (pfad, ordner))
        con.execute("DELETE FROM ocr_warteschlange WHERE pfad=? AND ordner=?", (pfad, ordner))
    return ids


def entferne_ordner(ordner: str) -> list[str]:
    with closing(_verbindung()) as con, con:
        ids = [r[0] for r in con.execute("SELECT id FROM abschnitte WHERE ordner=?", (ordner,))]
        con.execute("DELETE FROM abschnitte WHERE ordner=?", (ordner,))
        con.execute("DELETE FROM ocr_warteschlange WHERE ordner=?", (ordner,))
    return ids


def hat_abschnitte(pfad: str, ordner: str) -> bool:
    with closing(_verbindung()) as con:
        return con.execute("SELECT 1 FROM abschnitte WHERE pfad=? AND ordner=? LIMIT 1",
                           (pfad, ordner)).fetchone() is not None


def alle() -> list[tuple[str, str, dict]]:
    """[(id, text, meta)] — für die Stichwortsuche."""
    with closing(_verbindung()) as con:
        return [
            (r[0], r[6], {"pfad": r[1], "ordner": r[2], "datei": r[3], "seite": r[4], "unterordner": r[5]})
            for r in con.execute("SELECT id, pfad, ordner, datei, seite, unterordner, text FROM abschnitte")
        ]


def ohne_vektor(anzahl: int) -> list[tuple[str, str, str, str]]:
    """Nächste Abschnitte ohne Suchvektor: [(id, text, pfad, ordner)].
    Gesprächsquellen zuerst — die braucht jemand gerade."""
    with closing(_verbindung()) as con:
        return con.execute(
            "SELECT id, text, pfad, ordner FROM abschnitte WHERE vektor=0 "
            "ORDER BY (ordner LIKE 'gespraech:%') DESC LIMIT ?", (anzahl,)).fetchall()


def markiere_vektor(paare: list[tuple[str, str]]) -> list[str]:
    """Setzt vektor=1, aber nur wenn der Text inzwischen nicht geändert wurde.
    Gibt die IDs zurück, die es nicht mehr gibt (Datei zwischendurch entfernt)."""
    verschwunden = []
    with closing(_verbindung()) as con, con:
        for cid, text in paare:
            cur = con.execute("UPDATE abschnitte SET vektor=1 WHERE id=? AND text=?", (cid, text))
            if cur.rowcount == 0 and con.execute(
                    "SELECT 1 FROM abschnitte WHERE id=?", (cid,)).fetchone() is None:
                verschwunden.append(cid)
    return verschwunden


def zaehle() -> tuple[int, int]:
    """(Abschnitte gesamt, davon mit Suchvektor)"""
    with closing(_verbindung()) as con:
        return con.execute("SELECT COUNT(*), COALESCE(SUM(vektor), 0) FROM abschnitte").fetchone()


def setze_vektoren_zurueck():
    with closing(_verbindung()) as con, con:
        con.execute("UPDATE abschnitte SET vektor=0")


def meta(k: str, standard: str | None = None) -> str | None:
    with closing(_verbindung()) as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else standard


def setze_meta(k: str, v: str):
    with closing(_verbindung()) as con, con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))


# ── Warteschlange Texterkennung ───────────────────────────────────────────────

def ocr_einreihen(pfad: str, ordner: str):
    with closing(_verbindung()) as con, con:
        con.execute("INSERT OR IGNORE INTO ocr_warteschlange (pfad, ordner) VALUES (?,?)", (pfad, ordner))


def ocr_naechster() -> tuple[str, str, int] | None:
    """Gesprächsquellen zuerst."""
    with closing(_verbindung()) as con:
        return con.execute(
            "SELECT pfad, ordner, versuche FROM ocr_warteschlange "
            "ORDER BY (ordner LIKE 'gespraech:%') DESC, versuche LIMIT 1").fetchone()


def ocr_fehlversuch(pfad: str, ordner: str):
    with closing(_verbindung()) as con, con:
        con.execute("UPDATE ocr_warteschlange SET versuche=versuche+1 WHERE pfad=? AND ordner=?",
                    (pfad, ordner))


def ocr_erledigt(pfad: str, ordner: str):
    with closing(_verbindung()) as con, con:
        con.execute("DELETE FROM ocr_warteschlange WHERE pfad=? AND ordner=?", (pfad, ordner))


def ocr_anzahl() -> int:
    with closing(_verbindung()) as con:
        return con.execute("SELECT COUNT(*) FROM ocr_warteschlange").fetchone()[0]

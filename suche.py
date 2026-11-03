"""Dokumentsuche: Bedeutungssuche (lokale Qwen-Vektoren über Ollama) + Stichwortsuche
(BM25), zusammengeführt, gefiltert nach eingeschalteten Ordnern und gestreut über
mehrere Dateien — damit nicht ein einziges großes Dokument alle Plätze belegt.

Die Stichwortsuche deckt immer alle Abschnitte ab. Die Bedeutungssuche nur die,
für die schon ein Vektor berechnet ist — fehlt Ollama, läuft die Suche trotzdem.
"""
import math
import re
from collections import Counter

import config
import pdf_indexer
import textspeicher
import vektoren

_STOPPWOERTER = set("""
aber alle allem allen aller alles also als auch auf aus bei beim bin bis bitte da damit dann
das dass dem den denn der des die dies diese diesem diesen dieser dieses doch dort du durch
ein eine einem einen einer eines er es etwa euch für gegen gibt hab habe haben hat hatte hier
ich ihm ihn ihr ihre im in ist ja jede jedem jeden jeder jedes kann kein keine können man
mehr mein meine mich mir mit muss müssen nach nein nicht nichts noch nun nur ob oder ohne
schon sein seine sich sie sind so soll sollen über um und uns unser unter vom von vor war
waren was weil welche welchem welchen welcher wenn wer werden wie wieder wir wird wo wurde
wurden zu zum zur zwischen bzw sowie dazu worden wann warum wieso weshalb welches
""".split())

_WORT = re.compile(r"[a-zäöüß0-9]+")


def _woerter(text: str) -> list[str]:
    """Kleinschreibung, Stoppwörter raus, grobes Kürzen auf den Wortstamm
    (Rolltor/Rolltore/Rolltoren → rollto)."""
    return [w[:6] for w in _WORT.findall(text.lower()) if len(w) > 2 and w not in _STOPPWOERTER]


class _StichwortIndex:
    """Klassisches BM25 über alle Textabschnitte, im Arbeitsspeicher."""

    def __init__(self):
        alles = textspeicher.alle()
        self.ids = [a[0] for a in alles]
        self.texte = [a[1] for a in alles]
        self.metas = [a[2] for a in alles]
        self.position = {cid: i for i, cid in enumerate(self.ids)}
        self.tf = [Counter(_woerter(t)) for t in self.texte]
        self.laengen = [sum(c.values()) for c in self.tf]
        self.mittel = (sum(self.laengen) / len(self.laengen)) if self.laengen else 1
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(self.tf)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def suche(self, frage: str, erlaubt: set[str], anzahl: int) -> list[str]:
        begriffe = set(_woerter(frage))
        if not begriffe:
            return []
        k1, b = 1.5, 0.75
        punkte = []
        for i, tf in enumerate(self.tf):
            if self.metas[i]["ordner"] not in erlaubt:
                continue
            s = 0.0
            for w in begriffe:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.laengen[i] / self.mittel))
            if s > 0:
                punkte.append((s, i))
        punkte.sort(reverse=True)
        return [self.ids[i] for _, i in punkte[:anzahl]]


_cache: dict = {"version": None, "index": None}


def _stichwort_index() -> _StichwortIndex:
    if _cache["version"] != pdf_indexer.INDEX_VERSION or _cache["index"] is None:
        _cache["version"] = pdf_indexer.INDEX_VERSION
        _cache["index"] = _StichwortIndex()
    return _cache["index"]


def _rangliste(idx: _StichwortIndex, frage: str, vektor, ordner: list[str]) -> list[str]:
    """Bedeutungs- und Stichwortsuche in den angegebenen Ordnern, zusammengeführt
    per Reciprocal Rank Fusion (wer in beiden Listen weit oben steht, gewinnt)."""
    listen = [idx.suche(frage, set(ordner), config.SUCH_KANDIDATEN)]
    if vektor is not None:
        try:
            listen.append(vektoren.suche(vektor, ordner, config.SUCH_KANDIDATEN))
        except Exception as e:
            print(f"[!] Bedeutungssuche fehlgeschlagen: {str(e)[:150]}")
    punkte: dict[str, float] = {}
    for liste in listen:
        for rang, cid in enumerate(liste):
            if cid in idx.position:          # nur Abschnitte, die es (noch) gibt
                punkte[cid] = punkte.get(cid, 0.0) + 1.0 / (60 + rang)
    return sorted(punkte, key=punkte.get, reverse=True)


def _auswahl(idx: _StichwortIndex, rangliste: list[str], anzahl: int, max_pro_datei: int,
             gesehen: set) -> list[dict]:
    """Beste Stellen, aber höchstens `max_pro_datei` aus derselben Datei und ohne Dubletten."""
    ergebnis, pro_datei = [], Counter()
    for cid in rangliste:
        if len(ergebnis) >= anzahl:
            break
        i = idx.position[cid]
        text, meta = idx.texte[i], idx.metas[i]
        if pro_datei[meta["pfad"]] >= max_pro_datei:
            continue
        fingerabdruck = " ".join(text.split())[:300]   # gleiche Datei zweimal abgelegt
        if fingerabdruck in gesehen:
            continue
        gesehen.add(fingerabdruck)
        pro_datei[meta["pfad"]] += 1
        ergebnis.append({"text": text, **meta})
    return ergebnis


def suche_dokumente(frage: str, gespraechs_id: str | None = None) -> list[dict]:
    """Textstellen für eine Frage: zuerst aus den Quellen nur für dieses Gespräch
    (eigene Plätze, damit sie nicht von den großen Unterlagen verdrängt werden),
    dann aus den eingeschalteten Unterlagen-Ordnern."""
    idx = _stichwort_index()
    if not idx.ids:
        return []
    aktive = pdf_indexer.aktive_ordner()
    hat_gespraechsquellen = bool(gespraechs_id and pdf_indexer.gespraechs_quellen(gespraechs_id))
    if not aktive and not hat_gespraechsquellen:
        return []

    vektor = None
    try:
        vektor = vektoren.bette_ein([frage], anfrage=True, timeout=20)[0]
    except Exception as e:
        # Ollama nicht verfügbar → nur Stichwortsuche
        print(f"[!] Bedeutungssuche nicht verfügbar, nutze nur Stichworte: {str(e)[:150]}")

    gesehen: set = set()
    ergebnis = []
    if hat_gespraechsquellen:
        tag = pdf_indexer.gespraechs_tag(gespraechs_id)
        ergebnis += _auswahl(idx, _rangliste(idx, frage, vektor, [tag]),
                             config.GESPRAECH_ERGEBNISSE, config.GESPRAECH_ERGEBNISSE, gesehen)
    if aktive:
        ergebnis += _auswahl(idx, _rangliste(idx, frage, vektor, aktive),
                             config.MAX_PDF_ERGEBNISSE, config.MAX_PRO_DATEI, gesehen)
    return ergebnis

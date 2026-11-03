"""Dokumentsuche: Bedeutungssuche (Gemini-Vektoren) + Stichwortsuche (BM25),
zusammengeführt, gefiltert nach eingeschalteten Ordnern und gestreut über
mehrere Dateien — damit nicht ein einziges großes Dokument alle Plätze belegt."""
import math
import re
from collections import Counter

import config
import gemini_ki
import pdf_indexer

_STOPPWOERTER = set("""
aber alle allem allen aller alles also als auch auf aus bei beim bin bis bitte da damit dann
das dass dem den denn der des die dies diese diesem diesen dieser dieses doch dort du durch
ein eine einem einen einer eines er es etwa euch für gegen gibt hab habe haben hat hatte hier
ich ihm ihn ihr ihre im in ist ja jede jedem jeden jeder jedes kann kein keine können man
mehr mein meine mich mir mit muss müssen nach nein nicht nichts noch nun nur ob oder ohne
schon sein seine sich sie sind so soll sollen über um und uns unser unter vom von vor war
waren was weil welche welchem welchen welcher wenn wer werden wie wieder wir wird wo wurde
wurden zu zum zur zwischen bzw sowie dazu wurde worden wann warum wieso weshalb welches
""".split())

_WORT = re.compile(r"[a-zäöüß0-9]+")


def _woerter(text: str) -> list[str]:
    """Kleinschreibung, Stoppwörter raus, grobes Kürzen auf den Wortstamm
    (Rolltor/Rolltore/Rolltoren → rollto)."""
    return [w[:6] for w in _WORT.findall(text.lower()) if len(w) > 2 and w not in _STOPPWOERTER]


class _StichwortIndex:
    """Klassisches BM25 über alle Textabschnitte, im Arbeitsspeicher."""

    def __init__(self, collection):
        alles = collection.get(include=["documents", "metadatas"])
        self.ids = alles["ids"]
        self.texte = alles["documents"]
        self.metas = alles["metadatas"]
        self.tf = [Counter(_woerter(t)) for t in self.texte]
        self.laengen = [sum(c.values()) for c in self.tf]
        self.mittel = (sum(self.laengen) / len(self.laengen)) if self.laengen else 1
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(self.tf)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def suche(self, frage: str, erlaubt: set[str], anzahl: int) -> list[int]:
        begriffe = set(_woerter(frage))
        if not begriffe:
            return []
        k1, b = 1.5, 0.75
        punkte = []
        for i, tf in enumerate(self.tf):
            if self.metas[i].get("ordner", "") not in erlaubt:
                continue
            s = 0.0
            for w in begriffe:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.laengen[i] / self.mittel))
            if s > 0:
                punkte.append((s, i))
        punkte.sort(reverse=True)
        return [i for _, i in punkte[:anzahl]]


_cache: dict = {"version": None, "index": None}


def _stichwort_index(collection) -> _StichwortIndex:
    if _cache["version"] != pdf_indexer.INDEX_VERSION or _cache["index"] is None:
        _cache["index"] = _StichwortIndex(collection)
        _cache["version"] = pdf_indexer.INDEX_VERSION
    return _cache["index"]


def _rangliste(collection, frage: str, vektor, ordner: list[str]) -> list[tuple[str, dict]]:
    """Bedeutungs- und Stichwortsuche in den angegebenen Ordnern, zusammengeführt
    per Reciprocal Rank Fusion (wer in beiden Listen weit oben steht, gewinnt)."""
    ranglisten: list[list[tuple[str, str, dict]]] = []
    if vektor is not None:
        try:
            res = collection.query(
                query_embeddings=[vektor],
                n_results=min(config.SUCH_KANDIDATEN, collection.count()),
                where={"ordner": {"$in": ordner}},
            )
            ranglisten.append(list(zip(res["ids"][0], res["documents"][0], res["metadatas"][0])))
        except Exception as e:
            print(f"[!] Bedeutungssuche fehlgeschlagen: {str(e)[:150]}")

    idx = _stichwort_index(collection)
    treffer = idx.suche(frage, set(ordner), config.SUCH_KANDIDATEN)
    ranglisten.append([(idx.ids[i], idx.texte[i], idx.metas[i]) for i in treffer])

    punkte: dict[str, float] = {}
    daten: dict[str, tuple[str, dict]] = {}
    for liste in ranglisten:
        for rang, (cid, text, meta) in enumerate(liste):
            punkte[cid] = punkte.get(cid, 0.0) + 1.0 / (60 + rang)
            daten[cid] = (text, meta)
    return [daten[cid] for cid in sorted(punkte, key=punkte.get, reverse=True)]


def _auswahl(rangliste, anzahl: int, max_pro_datei: int, gesehen: set) -> list[dict]:
    """Beste Stellen, aber höchstens `max_pro_datei` aus derselben Datei und ohne Dubletten."""
    ergebnis, pro_datei = [], Counter()
    for text, meta in rangliste:
        if len(ergebnis) >= anzahl:
            break
        if pro_datei[meta["pfad"]] >= max_pro_datei:
            continue
        fingerabdruck = " ".join(text.split())[:300]   # gleiche Datei zweimal abgelegt
        if fingerabdruck in gesehen:
            continue
        gesehen.add(fingerabdruck)
        pro_datei[meta["pfad"]] += 1
        ergebnis.append({"text": text, **meta})
    return ergebnis


def suche_dokumente(collection, frage: str, gespraechs_id: str | None = None) -> list[dict]:
    """Textstellen für eine Frage: zuerst aus den Quellen nur für dieses Gespräch
    (eigene Plätze, damit sie nicht von den großen Unterlagen verdrängt werden),
    dann aus den eingeschalteten Unterlagen-Ordnern."""
    if collection.count() == 0:
        return []
    aktive = pdf_indexer.aktive_ordner()
    hat_gespraechsquellen = bool(gespraechs_id and pdf_indexer.gespraechs_quellen(gespraechs_id))
    if not aktive and not hat_gespraechsquellen:
        return []

    try:
        vektor = gemini_ki.bette_ein([frage], fuer_suchanfrage=True)[0]
    except Exception as e:
        # Bedeutungssuche nicht verfügbar (z. B. Limit) → nur Stichwortsuche
        print(f"[!] Bedeutungssuche fehlgeschlagen, nutze nur Stichworte: {str(e)[:150]}")
        vektor = None

    gesehen: set = set()
    ergebnis = []
    if hat_gespraechsquellen:
        tag = pdf_indexer.gespraechs_tag(gespraechs_id)
        ergebnis += _auswahl(_rangliste(collection, frage, vektor, [tag]),
                             config.GESPRAECH_ERGEBNISSE, config.GESPRAECH_ERGEBNISSE, gesehen)
    if aktive:
        ergebnis += _auswahl(_rangliste(collection, frage, vektor, aktive),
                             config.MAX_PDF_ERGEBNISSE, config.MAX_PRO_DATEI, gesehen)
    return ergebnis

"""Dokumentenvergleich: Gemini liest zwei bis fünf Dokumente vollständig und stellt
sie gegenüber (z. B. Protokolle mehrerer Jahre, mehrere Angebote, alte/neue Fassung
einer Hausordnung)."""
from pathlib import Path

import config
import pdf_indexer
from gemini_ki import frage_ki, mit_wiederholung

VERGLEICH_ANWEISUNG = """Du bist der Assistent eines Verwaltungsbeirats einer
Wohnungseigentümergemeinschaft (WEG) in Deutschland und vergleichst mehrere Dokumente
(Dokument A, B, C … — so viele, wie dir vorliegen).

REGELN:
1. Stütze dich ausschließlich auf die vorliegenden Dokumente. Erfinde nichts dazu.
2. Belege Aussagen mit Fundstellen: [A, S. 3], [B, S. 5] usw. Hat ein Dokument
   keine Seitenzahlen, nur [A], [B] usw.
3. Sind die Dokumente sehr unterschiedlicher Art (z. B. Rechnung und Hausordnung),
   sage das am Anfang ehrlich und vergleiche nur, was sinnvoll vergleichbar ist.
4. Antworte auf Deutsch, klar und gut verständlich, in Markdown.

AUFBAU DER ANTWORT:
## Kurzfazit
(2–4 Sätze: Worum geht es, was ist der wichtigste Unterschied?)
## Gemeinsamkeiten
## Unterschiede
(als Markdown-Tabelle mit einer Spalte je Dokument: | Punkt | Dokument A | Dokument B | …
— Beträge, Fristen, Zuständigkeiten, Beschlüsse, Regelungen; nur, was sich wirklich
unterscheidet)
## Neu, weggefallen oder geändert
(nur falls es erkennbar Fassungen/Jahrgänge desselben Dokuments sind — dann in
zeitlicher Reihenfolge)
## Widersprüche und Auffälligkeiten
## Was bedeutet das für den Beirat?
(konkrete Hinweise, was zu prüfen oder anzusprechen wäre)

Bei rechtlichen Themen weise am Ende in einem kurzen Satz darauf hin, dass der
Vergleich keine Rechtsberatung ersetzt."""


MAX_DOKUMENTE = 5
GESAMT_ZEICHEN = 600_000   # alle Dokumente zusammen (~150.000 Wörter)
BUCHSTABEN = "ABCDE"


def volltext(pfad: str, meldung=None, grenze: int | None = None) -> tuple[str, bool]:
    """Ganzer Text eines Dokuments mit Seitenmarken. Gibt (text, gekuerzt) zurück."""
    grenze = grenze or config.MAX_VERGLEICH_ZEICHEN
    teile = []
    for nr, text in pdf_indexer.lese_seiten(Path(pfad), meldung):
        text = text.strip()
        if text:
            teile.append(f"[Seite {nr}]\n{text}" if nr else text)
    gesamt = "\n\n".join(teile)
    if len(gesamt) > grenze:
        return gesamt[:grenze], True
    return gesamt, False


def vergleiche(pfade: list[str], schwerpunkt: str = "", meldung=None) -> str:
    """Vergleicht 2 bis 5 Dokumente. Jedes bekommt einen Buchstaben (A, B, C …)."""
    pfade = list(pfade)[:MAX_DOKUMENTE]
    grenze = min(config.MAX_VERGLEICH_ZEICHEN, GESAMT_ZEICHEN // max(len(pfade), 1))
    dokumente = []   # (buchstabe, name, text, gekuerzt)
    try:
        for buchstabe, pfad in zip(BUCHSTABEN, pfade):
            name = Path(pfad).name
            if meldung:
                meldung(f"Lese {name} …")
            text, gekuerzt = volltext(pfad, meldung, grenze)
            dokumente.append((buchstabe, name, text, gekuerzt))
    finally:
        pdf_indexer.ki_freigeben()   # lokale Texterkennung entladen

    leer = [name for _, name, text, _ in dokumente if not text.strip()]
    if leer:
        return ("In folgenden Dokumenten konnte ich keinen lesbaren Text finden, "
                f"deshalb ist kein Vergleich möglich: {', '.join(leer)}")

    prompt = (
        (f"SCHWERPUNKT DES BEIRATS: {schwerpunkt}\n"
         "Gehe auf diesen Schwerpunkt besonders ausführlich ein.\n\n" if schwerpunkt.strip() else "")
        + "\n\n".join(f"=== DOKUMENT {b}: {name} ===\n{text}" for b, name, text, _ in dokumente)
    )
    if meldung:
        meldung("Die KI vergleicht die Dokumente …")
    antwort = mit_wiederholung(lambda: frage_ki(prompt, system=VERGLEICH_ANWEISUNG), versuche=3)

    hinweise = ["  \n".join(f"**Dokument {b}:** {name}" for b, name, _, _ in dokumente)]
    for _, name, _, gekuerzt in dokumente:
        if gekuerzt:
            hinweise.append(f"*Hinweis: {name} ist sehr lang — verglichen wurde nur der vordere Teil.*")
    return "\n\n".join(hinweise) + "\n\n" + antwort

"""Dokumentenvergleich: Gemini liest zwei Dokumente vollständig und stellt sie
gegenüber (z. B. zwei Protokolle, zwei Angebote, alte/neue Fassung einer Hausordnung)."""
from pathlib import Path

import config
import pdf_indexer
from gemini_ki import frage_ki, mit_wiederholung

VERGLEICH_ANWEISUNG = """Du bist der Assistent eines Verwaltungsbeirats einer
Wohnungseigentümergemeinschaft (WEG) in Deutschland und vergleichst zwei Dokumente.

REGELN:
1. Stütze dich ausschließlich auf die beiden Dokumente. Erfinde nichts dazu.
2. Belege Aussagen mit Fundstellen: [A, S. 3] bzw. [B, S. 5]. Hat ein Dokument
   keine Seitenzahlen, nur [A] bzw. [B].
3. Sind die Dokumente sehr unterschiedlicher Art (z. B. Rechnung und Hausordnung),
   sage das am Anfang ehrlich und vergleiche nur, was sinnvoll vergleichbar ist.
4. Antworte auf Deutsch, klar und gut verständlich, in Markdown.

AUFBAU DER ANTWORT:
## Kurzfazit
(2–4 Sätze: Worum geht es in beiden, was ist der wichtigste Unterschied?)
## Gemeinsamkeiten
## Unterschiede
(als Markdown-Tabelle: | Punkt | Dokument A | Dokument B |  — Beträge, Fristen,
Zuständigkeiten, Beschlüsse, Regelungen; nur, was sich wirklich unterscheidet)
## Neu, weggefallen oder geändert
(nur falls es erkennbar zwei Fassungen/Jahrgänge desselben Dokuments sind)
## Widersprüche und Auffälligkeiten
## Was bedeutet das für den Beirat?
(konkrete Hinweise, was zu prüfen oder anzusprechen wäre)

Bei rechtlichen Themen weise am Ende in einem kurzen Satz darauf hin, dass der
Vergleich keine Rechtsberatung ersetzt."""


def volltext(pfad: str, meldung=None) -> tuple[str, bool]:
    """Ganzer Text eines Dokuments mit Seitenmarken. Gibt (text, gekuerzt) zurück."""
    teile = []
    for nr, text in pdf_indexer.lese_seiten(Path(pfad), meldung):
        text = text.strip()
        if text:
            teile.append(f"[Seite {nr}]\n{text}" if nr else text)
    gesamt = "\n\n".join(teile)
    if len(gesamt) > config.MAX_VERGLEICH_ZEICHEN:
        return gesamt[: config.MAX_VERGLEICH_ZEICHEN], True
    return gesamt, False


def vergleiche(pfad_a: str, pfad_b: str, schwerpunkt: str = "", meldung=None) -> str:
    name_a, name_b = Path(pfad_a).name, Path(pfad_b).name
    if meldung:
        meldung(f"Lese {name_a} …")
    text_a, gekuerzt_a = volltext(pfad_a, meldung)
    if meldung:
        meldung(f"Lese {name_b} …")
    text_b, gekuerzt_b = volltext(pfad_b, meldung)

    leer = [n for n, t in ((name_a, text_a), (name_b, text_b)) if not t.strip()]
    if leer:
        return ("In folgenden Dokumenten konnte ich keinen lesbaren Text finden, "
                f"deshalb ist kein Vergleich möglich: {', '.join(leer)}")

    prompt = (
        (f"SCHWERPUNKT DES BEIRATS: {schwerpunkt}\n"
         "Gehe auf diesen Schwerpunkt besonders ausführlich ein.\n\n" if schwerpunkt.strip() else "")
        + f"=== DOKUMENT A: {name_a} ===\n{text_a}\n\n"
        + f"=== DOKUMENT B: {name_b} ===\n{text_b}"
    )
    if meldung:
        meldung("Die KI vergleicht die Dokumente …")
    antwort = mit_wiederholung(lambda: frage_ki(prompt, system=VERGLEICH_ANWEISUNG), versuche=3)

    hinweise = [f"**Dokument A:** {name_a}  \n**Dokument B:** {name_b}"]
    for name, gekuerzt in ((name_a, gekuerzt_a), (name_b, gekuerzt_b)):
        if gekuerzt:
            hinweise.append(f"⚠️ *{name} ist sehr lang — verglichen wurde nur der vordere Teil.*")
    return "\n\n".join(hinweise) + "\n\n" + antwort

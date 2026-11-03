"""Anbindung an Google Gemini (google-genai SDK): Chat, Texterkennung, Suchvektoren."""
import re
import time
from datetime import datetime

import config

_client = None


class TagesKontingentErschoepft(RuntimeError):
    """Das Tageslimit der kostenlosen Stufe ist aufgebraucht — erst morgen wieder."""


def _hole_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def protokolliere(text: str):
    """Schreibt Details (z. B. vollständige Fehlermeldungen von Google) nach
    beirat_daten/einlesen.log — zur Fehlersuche."""
    try:
        if config.LOG_PFAD.exists() and config.LOG_PFAD.stat().st_size > 2_000_000:
            config.LOG_PFAD.replace(config.LOG_PFAD.with_suffix(".alt.log"))
        with open(config.LOG_PFAD, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
    except Exception:
        pass


def _ursache(text: str) -> str | None:
    """Ordnet eine Fehlermeldung von Google ein. None = kein vorübergehender Fehler."""
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "Minutenlimit der kostenlosen KI erreicht"
    if any(m in text for m in ("503", "UNAVAILABLE", "overloaded")):
        return "Google-Server gerade überlastet"
    if any(m in text for m in ("500", "INTERNAL", "DEADLINE_EXCEEDED", "timed out",
                                "ConnectError", "ReadTimeout", "RemoteProtocolError")):
        return "Verbindungsproblem"
    return None


def mit_wiederholung(funktion, versuche: int = 6, schritt: str = ""):
    """Führt einen KI-Aufruf aus und wartet bei Minutenlimit/Überlastung geduldig.
    Für das Einlesen der Unterlagen gedacht, wo Warten besser ist als Abbrechen.
    Jede Pause wird mit Ursache und Versuchsnummer angezeigt, der volle
    Fehlertext landet in der Logdatei."""
    for versuch in range(1, versuche + 1):
        try:
            return funktion()
        except Exception as e:
            text = str(e)
            protokolliere(f"FEHLER {schritt or 'KI-Aufruf'} (Versuch {versuch}/{versuche}): {text}")
            if "PerDay" in text or "per day" in text.lower():
                print("    [!] Tageslimit der kostenlosen KI erreicht.", flush=True)
                raise TagesKontingentErschoepft(text) from e
            ursache = _ursache(text)
            if ursache is None or versuch == versuche:
                if ursache:
                    print(f"    [!] {ursache} — nach {versuche} Versuchen aufgegeben, "
                          "wird beim nächsten Start erneut versucht.", flush=True)
                raise
            # Google nennt oft selbst eine Wartezeit ("retryDelay": "37s")
            vorgabe = re.search(r"retry(?:Delay)?\D{0,5}(\d+(?:\.\d+)?)s", text)
            if vorgabe:
                warte = float(vorgabe.group(1)) + 2
            elif "Minutenlimit" in ursache:
                warte = 65   # Minutenfenster sicher abwarten
            else:
                warte = 15 * versuch
            print(f"    … {ursache}{f' ({schritt})' if schritt else ''}. Pause {int(warte)} s, "
                  f"dann Versuch {versuch + 1} von {versuche} — geht automatisch weiter.", flush=True)
            time.sleep(warte)


class _Tempo:
    """Hält Anfragen gleichmäßig unter den Limits der kostenlosen Stufe
    (Anfragen und Text-Tokens je Minute), statt ständig gegen sie zu laufen."""

    def __init__(self, anfragen_pro_minute: int, tokens_pro_minute: int):
        self.anfragen = anfragen_pro_minute
        self.tokens = tokens_pro_minute
        self.verlauf: list[tuple[float, int]] = []   # (zeitpunkt, tokens)

    def warte(self, tokens: int = 0):
        if not config.KI_TEMPOLIMIT:
            return
        tokens = min(tokens, self.tokens)
        while True:
            jetzt = time.monotonic()
            self.verlauf = [(t, n) for t, n in self.verlauf if jetzt - t < 60]
            if (len(self.verlauf) < self.anfragen
                    and sum(n for _, n in self.verlauf) + tokens <= self.tokens):
                self.verlauf.append((jetzt, tokens))
                return
            pause = 60 - (jetzt - self.verlauf[0][0]) + 0.5
            if pause > 5:
                print(f"    (kurze Pause {int(pause)} s, um im kostenlosen Limit zu bleiben)", flush=True)
            time.sleep(max(pause, 0.5))


_tempo_text = _Tempo(config.TEMPO_TEXT_ANFRAGEN, 200_000)
_tempo_embed = _Tempo(config.TEMPO_EMBED_ANFRAGEN, config.TEMPO_EMBED_TOKENS)


def _geschaetzte_tokens(texte: list[str]) -> int:
    return int(sum(len(t) for t in texte) / 3.2) + 10 * len(texte)   # deutsch: eher vorsichtig


def frage_ki(prompt: str, system: str | None = None) -> str:
    """Stellt eine Frage an Gemini und gibt die Antwort als Text zurück."""
    from google.genai import types

    client = _hole_client()
    antwort = client.models.generate_content(
        model=config.GEMINI_MODELL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
        ),
    )
    return (antwort.text or "").strip()


# ── Suchvektoren (Embeddings) ─────────────────────────────────────────────────

_EMBED_PAKET = 20   # klein genug, dass ein Paket nie allein das Minutenlimit sprengt


def bette_ein(texte: list[str], fuer_suchanfrage: bool = False, meldung=None) -> list[list[float]]:
    """Wandelt Texte in Suchvektoren um (mehrsprachiges Gemini-Modell).
    Dokument-Abschnitte und Suchanfragen werden unterschiedlich kodiert —
    das verbessert die Trefferqualität."""
    from google.genai import types

    client = _hole_client()
    aufgabe = "RETRIEVAL_QUERY" if fuer_suchanfrage else "RETRIEVAL_DOCUMENT"
    vektoren: list[list[float]] = []
    for start in range(0, len(texte), _EMBED_PAKET):
        paket = texte[start : start + _EMBED_PAKET]
        teil = f"Abschnitt {start + 1}–{start + len(paket)} von {len(texte)}"
        if meldung and len(texte) > _EMBED_PAKET:
            meldung(f"Suchindex: {teil}")

        def _aufruf(paket=paket):
            if not fuer_suchanfrage:
                _tempo_embed.warte(_geschaetzte_tokens(paket))
            return client.models.embed_content(
                model=config.GEMINI_EMBED_MODELL,
                contents=paket,
                config=types.EmbedContentConfig(
                    task_type=aufgabe,
                    output_dimensionality=config.EMBED_DIMENSIONEN,
                ),
            )

        # Bei einer einzelnen Suchanfrage nicht minutenlang warten
        ergebnis = mit_wiederholung(_aufruf, versuche=2 if fuer_suchanfrage else 6,
                                    schritt=f"Suchindex, {teil}")
        vektoren.extend(list(e.values) for e in ergebnis.embeddings)
    return vektoren


# ── Texterkennung für eingescannte PDFs ──────────────────────────────────────

_OCR_ANWEISUNG = """Du erhältst {anzahl} Seite(n) eines eingescannten Dokuments einer
Wohnungseigentümergemeinschaft. Gib den Text JEDER Seite vollständig und wortgetreu wieder.

Regeln:
- Beginne jede Seite mit einer eigenen Zeile: === Seite N ===  (N = 1 bis {anzahl}, in Reihenfolge)
- Nichts zusammenfassen, nichts weglassen, nichts hinzuerfinden.
- Tabellen als Markdown-Tabelle wiedergeben.
- Bei Plänen, Grundrissen oder Zeichnungen: alle lesbaren Beschriftungen wiedergeben
  (Hausnummern, Wohnungsnummern, Raumbezeichnungen, Flächen, Maße, Legende).
- Handschriftliches so gut wie möglich; Unleserliches als [unleserlich].
- Leere Seite: nur die Kopfzeile und darunter [leer].
- Keine Einleitung, keine Erklärungen, keine Code-Blöcke."""

_SEITEN_MARKE = re.compile(r"^\s*=+\s*Seite\s+(\d+)\s*=+\s*$", re.M | re.I)


def erkenne_text(pdf_bytes: bytes, anzahl_seiten: int, mime: str = "application/pdf") -> list[str]:
    """Lässt Gemini den Text aus gescannten Seiten lesen. Gibt pro Seite einen
    Text zurück (Liste mit genau `anzahl_seiten` Einträgen)."""
    from google.genai import types

    client = _hole_client()

    def _aufruf():
        _tempo_text.warte(300 * anzahl_seiten)
        return client.models.generate_content(
            model=config.GEMINI_MODELL,
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type=mime),
                _OCR_ANWEISUNG.format(anzahl=anzahl_seiten),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=60000,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

    text = (mit_wiederholung(_aufruf, schritt="Texterkennung").text or "").strip()

    seiten = [""] * anzahl_seiten
    marken = list(_SEITEN_MARKE.finditer(text))
    if not marken:
        seiten[0] = text  # keine Seitenmarken — alles der ersten Seite zuordnen
        return seiten
    for i, m in enumerate(marken):
        nr = int(m.group(1))
        ende = marken[i + 1].start() if i + 1 < len(marken) else len(text)
        inhalt = text[m.end() : ende].strip()
        if inhalt == "[leer]":
            inhalt = ""
        if 1 <= nr <= anzahl_seiten:
            seiten[nr - 1] = (seiten[nr - 1] + "\n" + inhalt).strip()
    return seiten


def teste_verbindung() -> tuple[bool, str]:
    """Prüft, ob der API-Schlüssel funktioniert. Gibt (ok, meldung) zurück."""
    try:
        antwort = frage_ki("Antworte nur mit dem Wort: OK")
        if antwort:
            return True, "Verbindung zur KI funktioniert."
        return False, "Die KI hat nicht geantwortet."
    except Exception as e:
        text = str(e)
        if "API_KEY_INVALID" in text or "API key not valid" in text:
            return False, "Der API-Schlüssel ist ungültig. Bitte noch einmal prüfen."
        if "PERMISSION_DENIED" in text:
            return False, "Der Schlüssel hat keine Berechtigung. Bitte in Google AI Studio neu erstellen."
        if "NOT_FOUND" in text or "not found" in text:
            return False, f"Das Modell '{config.GEMINI_MODELL}' wurde nicht gefunden."
        return False, f"Verbindung fehlgeschlagen: {text[:200]}"

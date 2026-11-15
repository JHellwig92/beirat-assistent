"""Beirat-Assistent — Browser-Oberfläche (Streamlit)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

import config
import pdf_export
import pdf_indexer
import suche
import vergleich
import websuche
from gemini_ki import frage_ki

st.set_page_config(page_title="Beirat-Assistent", page_icon=":material/apartment:", layout="centered")

st.markdown("""
<style>
  /* Ruhiges, sachliches Erscheinungsbild (ohne Streamlit-Menü und -Fußzeile) */
  /* Menü und Fußzeile weg — die Werkzeugleiste selbst bleibt, darin sitzt der
     Knopf zum Wieder-Ausklappen der Seitenleiste */
  #MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stToolbarActions"],
  [data-testid="stAppDeployButton"], [data-testid="stDecoration"] { display: none !important; }
  header[data-testid="stHeader"] { background: transparent; }
  .block-container { padding-top: 2.2rem; max-width: 820px; }
  [data-testid="stSidebar"] { border-right: 1px solid #ececf1; }
  [data-testid="stSidebar"] [data-testid="stCaptionContainer"] { letter-spacing: .02em; }
  [data-testid="stSidebar"] button[kind="tertiary"], [data-testid="stSidebar"] button[kind="secondary"] {
      justify-content: flex-start; text-align: left; }
  [data-testid="stSidebar"] button[kind="tertiary"] > div, [data-testid="stSidebar"] button[kind="secondary"] > div {
      justify-content: flex-start; width: 100%; }
  [data-testid="stBottomBlockContainer"] { max-width: 820px; padding-bottom: 1.2rem; }
  [data-testid="stChatInput"] textarea { font-size: 1.05rem; }
  [data-testid="stPopover"] button, [data-testid="stBottomBlockContainer"] .stButton button {
      border-radius: 999px; font-size: .92rem; padding: .25rem .85rem; min-height: 2.1rem; }
  /* Chat wie bei ChatGPT/Claude: eigene Fragen als Blase rechts, keine bunten Symbole */
  [data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] { display: none; }
  [data-testid="stChatMessage"] { background: transparent; padding: .25rem 0; gap: 0; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
      background: #f4f4f5; border-radius: 18px; padding: .55rem 1.1rem; width: fit-content;
      max-width: 85%; margin-left: auto; }
  /* Quellen als kompakte Zeile */
  [class*="st-key-quellen_"] button { font-size: .85rem; padding: .1rem .55rem; min-height: 1.8rem;
      border: 1px solid #e4e4e7; border-radius: 8px; }
  /* Frühere Gespräche: "…" nur beim Darüberfahren */
  [data-testid="stSidebar"] [data-testid="stHorizontalBlock"]:not(:hover) [data-testid="stPopover"] { opacity: 0; }
  [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] { gap: .25rem; }
  [data-testid="stSidebar"] [data-testid="stPopoverButton"] > div > div[aria-hidden="true"] { display: none; }
  [data-testid="stSidebar"] [data-testid="stPopoverButton"] { border: 0; background: transparent; }
</style>
""", unsafe_allow_html=True)

SYSTEM_PROMPT = """Du bist der persönliche Assistent eines Verwaltungsbeirats einer
Wohnungseigentümergemeinschaft (WEG) in Deutschland.

DEINE REGELN — HALTE DICH STRIKT DARAN:
1. Antworte NUR auf Grundlage der bereitgestellten Quellen (Dokument-Auszüge und
   Web-Ergebnisse) sowie des Gesprächsverlaufs. Erfinde nichts dazu.
2. Kennzeichne jede wichtige Aussage mit ihrer Quelle, z. B. [D1] für Dokumente
   oder [W2] für Webquellen. Nutze nur Quellen, die dir vorliegen.
3. Wenn die Quellen die Frage nicht beantworten, sage das ehrlich
   („Dazu finde ich in den Unterlagen nichts") und schlage vor, wo man nachsehen
   könnte. Rate niemals.
4. Du kennst dich mit deutschem Wohnungseigentumsrecht aus (WEG-Gesetz, BGB,
   Rechtsprechung), aber juristische Aussagen stützt du immer auf die Quellen.
5. Antworte auf Deutsch, klar und gut verständlich. Erkläre Fachbegriffe kurz.
6. Bei rechtlichen Themen weise am Ende in einem kurzen Satz darauf hin, dass
   deine Antwort keine Rechtsberatung ersetzt."""

WEBSUCHE_ANWEISUNG = """
Falls die Dokument-Auszüge zur sicheren Beantwortung NICHT ausreichen (z. B. weil
aktuelle Gesetzeslage, Urteile oder Fristen nötig sind), antworte AUSSCHLIESSLICH
mit einer einzigen Zeile im Format:
WEBSUCHE: <passende Suchbegriffe>"""

PDF_FORMAT_ANWEISUNG = """Du bereitest eine bereits fertige, inhaltlich korrekte Antwort eines
WEG-Beirats-Assistenten für den Ausdruck als PDF auf. Verändere NICHT den inhaltlichen Gehalt,
erfinde keine neuen Fakten dazu und lasse keine wichtigen Informationen weg — nur Form, Gliederung
und optische Gestaltung passt du gemäß dem folgenden Wunsch an.

Gib AUSSCHLIESSLICH gültiges HTML zurück — keine Markdown-Syntax, keine Code-Blöcke (```), keine
Erklärungen davor oder danach. Erlaubt sind nur diese Tags: <h1> <h2> <h3> <p> <ul> <ol> <li>
<table> <tr> <th> <td> <b> <i> <hr>. Farben, Hervorhebungen, Ausrichtung und Hintergründe
gestaltest du über Inline-CSS im style-Attribut, z. B. style="color:#c71585;" oder
style="text-align:center;background-color:#fce4ec;". Kein <script>, <img>, <iframe> oder
<html>/<head>/<body>-Gerüst — nur der Inhalt selbst. Antworte auf Deutsch."""

PDF_FORMAT_OPTIONEN = {
    "Wie im Chat": None,
    "Als Beschlussvorlage": (
        "Formatiere den Inhalt als Beschlussvorlage für eine Eigentümerversammlung: "
        "Überschrift 'Beschlussvorlage', darunter 'Sachverhalt', 'Beschlussvorschlag' "
        "(als konkreter, abstimmbarer Text) und zum Schluss "
        "'Abstimmungsergebnis: ___ Ja / ___ Nein / ___ Enthaltung' als Platzhalter."
    ),
    "Als Liste / Stichpunkte": (
        "Fasse den Inhalt als klar gegliederte Stichpunktliste zusammen, "
        "mit kurzen, prägnanten Punkten."
    ),
    "Als Brief an Miteigentümer": (
        "Formuliere den Inhalt als Brief an die Miteigentümer: mit Anrede "
        "('Sehr geehrte Damen und Herren,'), Fließtext in Briefform und Grußformel "
        "('Mit freundlichen Grüßen, der Verwaltungsbeirat')."
    ),
    "Eigene Anweisung": None,
}


# ── Verlauf & Wissensstand ────────────────────────────────────────────────────

def lade_verlauf() -> list:
    if not config.VERLAUF_PFAD.exists():
        return []
    try:
        verlauf = json.loads(config.VERLAUF_PFAD.read_text(encoding="utf-8"))
    except Exception:
        return []

    # Ältere Einträge ohne Gespräch-ID (vor Einführung der Fortsetzen-Funktion)
    # bekommen einmalig je eine eigene ID, damit sie als eigenständige,
    # einzeln fortsetzbare Gespräche erscheinen.
    geaendert = False
    for eintrag in verlauf:
        if "gespraechs_id" not in eintrag:
            eintrag["gespraechs_id"] = str(uuid.uuid4())
            geaendert = True
    if geaendert:
        speichere_verlauf(verlauf)
    return verlauf


def speichere_verlauf(verlauf: list):
    config.VERLAUF_PFAD.write_text(
        json.dumps(verlauf, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def loesche_gespraech(gespraechs_id: str):
    """Entfernt alle Verlaufseinträge einer Gespräch-ID unwiderruflich —
    samt den Quellen, die nur für dieses Gespräch hinzugefügt wurden."""
    verlauf = lade_verlauf()
    verlauf = [e for e in verlauf if e["gespraechs_id"] != gespraechs_id]
    speichere_verlauf(verlauf)
    pdf_indexer.entferne_alle_gespraechs_quellen(gespraechs_id)


def speichere_im_verlauf(frage: str, antwort: str, doks: list[dict], webs: list[dict]):
    verlauf = lade_verlauf()
    verlauf.append({
        "zeit": datetime.now().isoformat(timespec="seconds"),
        "gespraechs_id": st.session_state.gespraechs_id,
        "frage": frage,
        "antwort": antwort,
        "quellen_doks": [
            {"datei": d["datei"], "seite": d.get("seite", 0), "pfad": d.get("pfad", "")}
            for d in doks
        ],
        "quellen_web": [{"titel": w["titel"], "url": w["url"]} for w in webs],
    })
    speichere_verlauf(verlauf)
    if len(verlauf) % 10 == 0:
        aktualisiere_wissensstand(verlauf)


def fehlertext_ki(e: Exception, einleitung: str = "Es gab leider einen technischen Fehler") -> str:
    text = str(e)
    if "PerDay" in text or "per day" in text.lower():
        return ("Das Tageslimit der kostenlosen KI-Stufe ist für heute aufgebraucht. "
                "Morgen geht es wieder.")
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return ("Die kostenlose KI-Stufe ist für den Moment ausgeschöpft. "
                "Bitte in einer Minute noch einmal versuchen.")
    if "503" in text or "overloaded" in text.lower() or "unavailable" in text.lower():
        return "Die KI ist gerade stark ausgelastet. Bitte eine Minute warten und es erneut versuchen."
    if "API key not valid" in text or "API_KEY_INVALID" in text:
        return ("Der Google-KI-Schlüssel ist ungültig. Bitte einmal **einrichten.bat** "
                "doppelklicken und den Schlüssel neu eingeben.")
    return f"{einleitung}: {text[:200]}"


def gruppiere_verlauf(verlauf: list) -> list[dict]:
    """Fasst Verlaufseinträge nach Gespräch-ID zusammen, neueste Aktivität zuerst."""
    gruppen: dict[str, list] = {}
    for eintrag in verlauf:
        gruppen.setdefault(eintrag["gespraechs_id"], []).append(eintrag)
    ergebnis = [
        {"id": gid, "eintraege": eintraege, "letzte_zeit": eintraege[-1].get("zeit", "")}
        for gid, eintraege in gruppen.items()
    ]
    ergebnis.sort(key=lambda g: g["letzte_zeit"], reverse=True)
    return ergebnis


def gruppe_zu_chat(gruppe: dict) -> list[dict]:
    """Baut aus einer Verlaufsgruppe wieder eine Chat-Liste für st.session_state.chat."""
    chat = []
    for e in gruppe["eintraege"]:
        chat.append({"rolle": "user", "text": e["frage"]})
        chat.append({
            "rolle": "assistent",
            "text": e["antwort"],
            "doks": e.get("quellen_doks", []),
            "webs": e.get("quellen_web", []),
        })
    return chat


def lade_wissensstand() -> str:
    if config.WISSENSSTAND_PFAD.exists():
        return config.WISSENSSTAND_PFAD.read_text(encoding="utf-8").strip()
    return ""


def aktualisiere_wissensstand(verlauf: list):
    """Fasst nach jeweils 10 Gesprächen das Gelernte dauerhaft zusammen."""
    letzte = verlauf[-10:]
    gespraeche = "\n\n".join(
        f"Frage: {e['frage']}\nAntwort: {e['antwort'][:600]}" for e in letzte
    )
    alter_stand = lade_wissensstand()
    prompt = (
        "Du pflegst den dauerhaften Wissensstand eines WEG-Beirats-Assistenten.\n"
        "Fasse aus dem bisherigen Wissensstand und den neuen Gesprächen die Fakten\n"
        "zusammen, die dauerhaft wichtig bleiben (Besonderheiten der Anlage,\n"
        "wiederkehrende Themen, geklärte Fragen, Eigenheiten der Teilungserklärung).\n"
        "Keine Allgemeinplätze. Gib NUR die aktualisierte Liste in Markdown zurück,\n"
        "maximal 30 Stichpunkte.\n\n"
        f"BISHERIGER WISSENSSTAND:\n{alter_stand or '(noch leer)'}\n\n"
        f"NEUE GESPRÄCHE:\n{gespraeche}"
    )
    try:
        neu = frage_ki(prompt)
        if neu:
            config.WISSENSSTAND_PFAD.write_text(neu, encoding="utf-8")
    except Exception:
        pass  # Wissensstand ist Komfort — Fehler dürfen den Chat nicht stören


# ── Suche & Antwort ───────────────────────────────────────────────────────────

def baue_kontext(doks: list[dict], webs: list[dict]) -> str:
    teile = []
    if doks:
        teile.append("DOKUMENT-AUSZÜGE (aus den eigenen Unterlagen):")
        for i, d in enumerate(doks, 1):
            ort = f", Seite {d['seite']}" if d.get("seite") else ""
            teile.append(f"[D{i}] {d['datei']}{ort}:\n{d['text']}")
    else:
        teile.append("DOKUMENT-AUSZÜGE: (keine passenden Stellen gefunden)")
    if webs:
        teile.append("\nWEB-ERGEBNISSE (Internetsuche):")
        for i, w in enumerate(webs, 1):
            teile.append(f"[W{i}] {w['titel']} — {w['url']}:\n{w['text']}")
    return "\n\n".join(teile)


def baue_gespraechskontext() -> str:
    nachrichten = st.session_state.chat[-6:]
    if not nachrichten:
        return ""
    teile = ["BISHERIGES GESPRÄCH (zur Einordnung von Rückfragen):"]
    for n in nachrichten:
        rolle = "Beirat" if n["rolle"] == "user" else "Assistent"
        teile.append(f"{rolle}: {n['text'][:500]}")
    return "\n".join(teile) + "\n\n"


def beantworte_frage(frage: str, web_modus: str, nur_pfade: list[str] | None):
    doks = suche.suche_dokumente(frage, st.session_state.gespraechs_id, nur_pfade or None)
    webs = []
    if web_modus == "Immer":
        webs = websuche.suche_web(frage)

    wissensstand = lade_wissensstand()
    wissens_block = (
        f"GESPEICHERTER WISSENSSTAND (aus früheren Gesprächen):\n{wissensstand}\n\n"
        if wissensstand else ""
    )
    basis = (
        f"{wissens_block}"
        f"{baue_gespraechskontext()}"
        f"{baue_kontext(doks, webs)}\n\n"
        f"FRAGE DES BEIRATS:\n{frage}"
    )

    anweisung = WEBSUCHE_ANWEISUNG if web_modus == "Automatisch" else ""
    antwort = frage_ki(basis + anweisung, system=SYSTEM_PROMPT)

    if web_modus == "Automatisch" and antwort.upper().lstrip().startswith("WEBSUCHE:"):
        suchbegriffe = antwort.split(":", 1)[1].strip() or frage
        webs = websuche.suche_web(suchbegriffe)
        basis = (
            f"{wissens_block}"
            f"{baue_gespraechskontext()}"
            f"{baue_kontext(doks, webs)}\n\n"
            f"FRAGE DES BEIRATS:\n{frage}\n\n"
            "Eine Internetsuche wurde bereits durchgeführt — beantworte die Frage "
            "jetzt mit den vorhandenen Quellen."
        )
        antwort = frage_ki(basis, system=SYSTEM_PROMPT)

    return antwort, doks, webs


# ── Quellen-Anzeige ───────────────────────────────────────────────────────────

def zeige_quellen(doks: list[dict], webs: list[dict], antwort: str, key_prefix: str):
    d_refs = {int(m) for m in re.findall(r"\[D(\d+)\]", antwort)}
    w_refs = {int(m) for m in re.findall(r"\[W(\d+)\]", antwort)}

    gezeigt_doks = [(i, d) for i, d in enumerate(doks, 1) if not d_refs or i in d_refs]
    gezeigt_webs = [(i, w) for i, w in enumerate(webs, 1) if not w_refs or i in w_refs]
    if not gezeigt_doks and not gezeigt_webs:
        return

    st.caption("Quellen")
    schon_gezeigt = set()
    with st.container(horizontal=True, gap="small", key=f"quellen_{key_prefix}"):
        for i, d in gezeigt_doks:
            merkmal = (d["datei"], d.get("seite", 0))
            if merkmal in schon_gezeigt:
                continue
            schon_gezeigt.add(merkmal)
            ort = f" · S. {d['seite']}" if d.get("seite") else ""
            if d.get("pfad"):
                if st.button(f"[D{i}] {d['datei']}{ort}", key=f"{key_prefix}_d{i}",
                             icon=":material/description:", type="tertiary", help="Dokument öffnen"):
                    try:
                        os.startfile(d["pfad"])
                    except Exception:
                        st.warning(f"Datei konnte nicht geöffnet werden: {d['pfad']}")
            else:
                st.markdown(f":material/description: [D{i}] {d['datei']}{ort}")
        for i, w in gezeigt_webs:
            if w.get("url"):
                st.link_button(f"[W{i}] {(w['titel'] or w['url'])[:60]}", w["url"],
                               icon=":material/link:", type="tertiary")


# ── PDF-Export ────────────────────────────────────────────────────────────────

def gestalte_fuer_pdf(frage: str, antwort: str, anweisung: str) -> str:
    """Lässt die KI den Inhalt direkt als gestaltetes HTML zurückgeben (Farben,
    Gliederung, Hervorhebungen gemäß Nutzerwunsch)."""
    prompt = (
        f"WUNSCH DES BEIRATS: {anweisung}\n\n"
        f"URSPRÜNGLICHE FRAGE: {frage}\n\n"
        f"URSPRÜNGLICHE ANTWORT:\n{antwort}"
    )
    html = frage_ki(prompt, system=PDF_FORMAT_ANWEISUNG)
    return re.sub(r"^```(?:html)?|```$", "", html.strip(), flags=re.MULTILINE).strip()


# ── Unterlagen einlesen & Ordner ──────────────────────────────────────────────

SCAN_HINWEIS = (
    "Eingescannte Dokumente werden dabei von der KI gelesen. Das kann einige "
    "Minuten dauern. Bitte das schwarze Fenster und den Browser geöffnet lassen."
)


@st.fragment(run_every=15)
def hintergrund_anzeige():
    """Fortschritt von Stufe 2 (Scans + Suchindex) — aktualisiert sich alle 15 s selbst.
    Bewusst immer genau EIN Element mit wechselndem Text: Wenn bei der automatischen
    Aktualisierung Elemente hinzukommen oder wegfallen, gerät der Browser manchmal
    durcheinander ("NotFoundError … removeChild")."""
    f = pdf_indexer.fortschritt_stufe2()
    zeilen = []
    if f["abschnitte"] and f["mit_vektor"] < f["abschnitte"]:
        prozent = int(100 * f["mit_vektor"] / f["abschnitte"])
        zeilen.append(f":material/manage_search: **Suchindex:** {f['mit_vektor']} von {f['abschnitte']} Abschnitten ({prozent} %)  \n"
                      "Bis er fertig ist, findet der Assistent Unterlagen über Stichworte — "
                      "fragen geht also schon.")
    if f["scans_offen"]:
        zeilen.append(f":material/document_scanner: {f['scans_offen']} eingescannte Datei(en) warten noch auf die Texterkennung.")
    if f["laeuft"] and f["aufgabe"]:
        zeilen.append(f":material/hourglass_top: {f['aufgabe']}")
    if f["hinweis"]:
        zeilen.append(f":material/info: {f['hinweis']}")
    st.caption("\n\n".join(zeilen) if zeilen else "\u200b")


def fortschritts_box(titel: str, hinweis: str = ""):
    """Kasten mit Titel, Fortschrittsbalken und Detailzeile.
    Gibt (platz, fortschritt(i, gesamt, name), meldung(text)) zurück."""
    platz = st.empty()
    with platz.container(border=True):
        st.markdown(titel)
        if hinweis:
            st.caption(hinweis)
        balken = st.progress(0.0)
        detail = st.empty()

    def fortschritt(i, gesamt, name):
        balken.progress(i / max(gesamt, 1), text=f"Datei {i + 1} von {gesamt}: {name}")

    def meldung(text):
        detail.caption(text)

    return platz, fortschritt, meldung


def indexiere_mit_anzeige(nur_ordner: str | None = None) -> dict:
    """Prüft/liest die Unterlagen ein und zeigt dabei einen Fortschrittsbalken."""
    if nur_ordner:
        titel = f":material/folder_open: **Neuer Ordner „{Path(nur_ordner).name}“ wird eingelesen …**"
    else:
        titel = ":material/folder_open: **Unterlagen werden auf Änderungen geprüft …**"
    hinweis = ""
    if not pdf_indexer.anzahl_eingelesen() or nur_ordner:
        hinweis = ("Die Dateien werden gelesen — das geht meist in ein bis zwei Minuten. "
                   "Eingescannte Dokumente und der Suchindex werden danach im Hintergrund "
                   "fertiggestellt, während du schon fragen kannst.")
    platz, fortschritt, meldung = fortschritts_box(titel, hinweis)
    try:
        status = pdf_indexer.pruefe_und_aktualisiere(fortschritt, meldung, nur_ordner)
    except Exception as e:
        status = {"fehler": str(e)}
    platz.empty()
    return status


# ── Einstellung Internetsuche (bleibt über Neustarts erhalten) ──────────────────────────

WEB_OPTIONEN = {
    "Automatisch": "Nur wenn die Unterlagen nicht ausreichen",
    "Immer": "Zusätzlich zu den Unterlagen, z. B. für Gesetze und Urteile",
    "Nie": "Nur die eigenen Unterlagen",
}


def lade_einstellungen() -> dict:
    werte = {"web_modus": "Automatisch"}
    try:
        werte.update(json.loads(config.EINSTELLUNGEN_PFAD.read_text(encoding="utf-8")))
    except Exception:
        pass
    if werte["web_modus"] not in WEB_OPTIONEN:
        werte["web_modus"] = "Automatisch"
    return werte


def speichere_einstellungen():
    werte = {k: st.session_state[k] for k in ("web_modus",) if k in st.session_state}
    try:
        config.EINSTELLUNGEN_PFAD.write_text(json.dumps(werte), encoding="utf-8")
    except Exception:
        pass


# ── Gespräche wechseln ────────────────────────────────────────────────────────

def wechsle_gespraech(gespraechs_id: str | None = None, chat: list | None = None):
    """Neues Gespräch (ohne Argumente) oder ein früheres fortsetzen."""
    st.session_state.chat = chat or []
    st.session_state.gespraechs_id = gespraechs_id or str(uuid.uuid4())
    st.session_state.nur_pfade = []
    for schluessel in ("zusammenfassung", "gespraech_pdf"):
        st.session_state.pop(schluessel, None)


def oeffne_dialog(name: str):
    st.session_state.dialog = name


# ── Gespräch zu bestimmten Dokumenten ─────────────────────────────────────────

def lies_fuer_gespraech(pfade: list[str], meldung=None) -> tuple[list[str], list[str]]:
    """Liest Dateien/Ordner vom Computer für das aktuelle Gespräch ein.
    Gibt (nutzbare Dateipfade, Hinweise) zurück."""
    gid = st.session_state.gespraechs_id
    hinweise = []
    for pfad in pfade:
        try:
            erg = pdf_indexer.fuege_gespraechs_quelle_hinzu(gid, pfad, meldung=meldung)
        except Exception as e:
            hinweise.append(f"{Path(pfad).name} konnte nicht eingelesen werden ({str(e)[:80]}).")
            continue
        if erg["ohne_text"]:
            hinweise.append(f"Kein lesbarer Text in: {', '.join(erg['ohne_text'])}")
        if erg["fehler"]:
            hinweise.append(f"Nicht einlesbar: {', '.join(erg['fehler'])}")
        if erg["scan_wartet"]:
            hinweise.append(f"Wird noch gelesen (Scan): {', '.join(erg['scan_wartet'])}")
    gelesen = []
    ziele = {pdf_indexer._norm(p) for p in pfade}
    for q in pdf_indexer.gespraechs_quellen(gid):
        if pdf_indexer._norm(q["pfad"]) in ziele:
            gelesen += q["dateien"]
    return gelesen, hinweise


@st.dialog("Gespräch zu bestimmten Dokumenten", width="large")
def dokument_dialog():
    gid = st.session_state.gespraechs_id
    st.caption("Wählen Sie die Dokumente, um die es in diesem Gespräch gehen soll. "
               "Ich antworte dann nur aus diesen Dokumenten.")
    auswahl = pdf_indexer.alle_dokumente(gid)
    namen = {d["pfad"]: f"{d['name']}  ·  {d['herkunft']}" for d in auswahl}
    vorher = [p for p in st.session_state.get("nur_pfade", []) if p in namen]
    gewaehlt = st.multiselect(
        "Dokumente", list(namen), default=vorher, format_func=lambda p: namen.get(p, Path(p).name),
        placeholder="Hier klicken und Namen tippen, z. B. „Verwaltervertrag“ …", select_all=False,
    )

    zaehler = st.session_state.setdefault("upload_zaehler", 0)
    with st.expander("Datei vom Computer hinzufügen (z. B. von einem Stick)", icon=":material/upload_file:"):
        hochgeladen = st.file_uploader(
            "Dateien", type=["pdf", "docx", "txt"], accept_multiple_files=True,
            key=f"gq_upload_{zaehler}", label_visibility="collapsed",
        )
        ordner_knopf = st.button("Ganzen Ordner wählen …", icon=":material/folder:")

    neue_pfade = []
    if ordner_knopf:
        with st.spinner("Bitte im aufgehenden Fenster einen Ordner auswählen …"):
            pfad = waehle_ordner_dialog()
        if pfad is None:
            st.warning("Das Auswahlfenster ließ sich nicht öffnen. Bitte die Dateien oben hochladen.")
        elif pfad:
            neue_pfade.append(pfad)

    links, rechts = st.columns(2)
    alle = links.button("Doch alle Unterlagen verwenden", use_container_width=True)
    uebernehmen = rechts.button("Übernehmen", type="primary", use_container_width=True,
                                disabled=not (gewaehlt or hochgeladen or neue_pfade))
    if alle:
        st.session_state.nur_pfade = []
        st.rerun()
    if not (uebernehmen or neue_pfade):
        return

    if hochgeladen:
        neue_pfade += [str(pdf_indexer.speichere_upload(gid, d.name, d.getvalue())) for d in hochgeladen]
        st.session_state.upload_zaehler = zaehler + 1
    hinweise = []
    if neue_pfade:
        detail = st.empty()
        with st.spinner("Dateien werden eingelesen …"):
            dazu, hinweise = lies_fuer_gespraech(neue_pfade, meldung=detail.caption)
        gewaehlt = list(dict.fromkeys(gewaehlt + dazu))
    st.session_state.nur_pfade = gewaehlt
    st.session_state.auswahl_hinweise = hinweise
    st.rerun()


# ── Dokumentenvergleich ───────────────────────────────────────────────────────

def _dokument_wahl(spalte, buchstabe: str, auswahl: list[dict]) -> str | None:
    """Ein Dokument aus den Unterlagen wählen oder vom Computer hochladen.
    Gibt den Pfad zurück (bzw. "upload:<Buchstabe>")."""
    with spalte:
        index = st.selectbox(
            f"Dokument {buchstabe}", range(len(auswahl)), index=None,
            format_func=lambda i: f"{auswahl[i]['name']}  ·  {auswahl[i]['herkunft']}",
            placeholder="Namen tippen oder auswählen …", key=f"vgl_wahl_{buchstabe}",
        )
        with st.popover("Vom Computer", icon=":material/upload_file:", type="tertiary"):
            hochgeladen = st.file_uploader(
                "Datei hochladen", type=["pdf", "docx", "txt"], key=f"vgl_upload_{buchstabe}",
            )
        if hochgeladen:
            st.caption(f"Verwendet: {hochgeladen.name}")
            return f"upload:{buchstabe}"
        return auswahl[index]["pfad"] if index is not None else None


@st.dialog("Dokumente vergleichen", width="large")
def vergleich_dialog():
    gid = st.session_state.gespraechs_id
    auswahl = pdf_indexer.alle_dokumente(gid)
    st.caption("Die KI liest die Dokumente vollständig und stellt sie gegenüber – "
               "z. B. Protokolle mehrerer Jahre oder mehrere Angebote.")
    anzahl = st.segmented_control("Wie viele Dokumente möchten Sie vergleichen?",
                                  [2, 3, 4, 5], default=2, key="vgl_anzahl") or 2
    buchstaben = vergleich.BUCHSTABEN[:anzahl]
    spalten = st.columns(2)
    wahlen = [_dokument_wahl(spalten[i % 2], b, auswahl) for i, b in enumerate(buchstaben)]
    schwerpunkt = st.text_input(
        "Worauf soll besonders geachtet werden? (freiwillig)",
        placeholder="z. B. Kosten und Fristen, Änderungen bei der Tiefgarage …",
    )
    if not st.button("Vergleichen", type="primary", disabled=not all(wahlen)):
        return
    if len(set(wahlen)) < len(wahlen):
        st.warning("Bitte jedes Dokument nur einmal wählen.")
        return

    # Hochgeladene Dateien beim Gespräch ablegen und als Gesprächsquelle einlesen,
    # damit Rückfragen zum Vergleich auch darin suchen können.
    pfade = []
    for wahl, buchstabe in zip(wahlen, buchstaben):
        if wahl.startswith("upload:"):
            datei = st.session_state[f"vgl_upload_{buchstabe}"]
            pfad = str(pdf_indexer.speichere_upload(gid, datei.name, datei.getvalue()))
            try:
                pdf_indexer.fuege_gespraechs_quelle_hinzu(gid, pfad)
            except Exception:
                pass   # Vergleich klappt trotzdem; nur Rückfragen finden die Datei dann nicht
            pfade.append(pfad)
        else:
            pfade.append(wahl)

    detail = st.empty()
    with st.spinner("Dokumente werden gelesen und verglichen – das kann eine Minute dauern …"):
        try:
            antwort = vergleich.vergleiche(pfade, schwerpunkt, meldung=detail.caption)
        except Exception as e:
            st.error(fehlertext_ki(e, "Der Vergleich ist fehlgeschlagen"))
            return

    frage = "Vergleich: " + " · ".join(Path(p).name for p in pfade)
    if schwerpunkt.strip():
        frage += f" (Schwerpunkt: {schwerpunkt.strip()})"
    doks = [{"datei": Path(p).name, "pfad": p, "seite": 0} for p in pfade]
    st.session_state.chat.append({"rolle": "user", "text": frage})
    st.session_state.chat.append({"rolle": "assistent", "text": antwort, "doks": doks, "webs": []})
    speichere_im_verlauf(frage, antwort, doks, [])
    st.rerun()


_DIALOG_SKRIPT = """
import tkinter as tk
from tkinter import filedialog
fenster = tk.Tk()
fenster.withdraw()
fenster.attributes("-topmost", True)
pfad = filedialog.askdirectory(title="Ordner mit Unterlagen auswählen", mustexist=True, parent=fenster)
print(pfad or "")
"""


def waehle_ordner_dialog() -> str | None:
    """Öffnet das Windows-Fenster zur Ordnerauswahl (in eigenem Prozess, weil
    Streamlit keine Fenster öffnen kann). None = Fenster nicht verfügbar,
    "" = abgebrochen."""
    try:
        erg = subprocess.run(
            [sys.executable, "-c", _DIALOG_SKRIPT],
            capture_output=True, text=True, encoding="utf-8", timeout=900,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception:
        return None
    if erg.returncode != 0:
        return None
    return erg.stdout.strip()


def ordner_hinzufuegen(pfad: str):
    ok, meldung = pdf_indexer.fuege_ordner_hinzu(pfad)
    if ok:
        st.session_state.neuer_ordner = os.path.abspath(pfad.strip().strip('"'))
        st.session_state.pop("ordner_manuell", None)
        st.rerun()
    else:
        st.warning(meldung)


def ordner_ui():
    st.caption("Häkchen = wird bei Fragen durchsucht. Umschalten wirkt sofort.")
    zaehler = pdf_indexer.dateien_pro_ordner()
    ordner = pdf_indexer.lade_ordner()
    for o in ordner:
        schluessel = hashlib.md5(pdf_indexer._norm(o["pfad"]).encode()).hexdigest()[:10]
        name = Path(o["pfad"]).name or o["pfad"]
        aktiv = st.checkbox(
            f"{name}{' (Hauptordner)' if o['haupt'] else ''}",
            value=o["aktiv"], key=f"ordner_{schluessel}", help=o["pfad"],
        )
        info = f"{zaehler.get(pdf_indexer._norm(o['pfad']), 0)} Datei(en)"
        if not o["erreichbar"]:
            info += " · :material/warning: gerade nicht erreichbar (z. B. Stick abgezogen) — bereits Eingelesenes bleibt nutzbar"
        st.caption(info)
        if aktiv != o["aktiv"]:
            pdf_indexer.setze_ordner_aktiv(o["pfad"], aktiv)
            st.rerun(scope="fragment")

        if not o["haupt"]:
            frage_key = f"ordner_weg_{schluessel}"
            if st.session_state.get(frage_key):
                st.warning(f"Ordner „{name}“ aus der Liste entfernen? Die Dateien selbst bleiben erhalten.")
                ja, nein = st.columns(2)
                if ja.button("Ja, entfernen", key=f"{frage_key}_ja"):
                    pdf_indexer.entferne_ordner(o["pfad"])
                    st.session_state.pop(frage_key, None)
                    st.rerun(scope="fragment")
                if nein.button("Abbrechen", key=f"{frage_key}_nein"):
                    st.session_state.pop(frage_key, None)
                    st.rerun(scope="fragment")
            elif st.button("Aus Liste entfernen", key=f"{frage_key}_btn", icon=":material/delete:", type="tertiary"):
                st.session_state[frage_key] = True
                st.rerun(scope="fragment")

    if ordner and not any(o["aktiv"] for o in ordner):
        st.warning("Kein Ordner eingeschaltet — der Assistent durchsucht gerade keine Unterlagen.")

    if st.button("Ordner hinzufügen", icon=":material/create_new_folder:"):
        with st.spinner("Bitte im aufgehenden Fenster einen Ordner auswählen …"):
            pfad = waehle_ordner_dialog()
        if pfad is None:
            st.session_state.ordner_manuell = True
        elif pfad:
            ordner_hinzufuegen(pfad)
    if st.session_state.get("ordner_manuell"):
        st.caption("Ordnerpfad aus der Adresszeile des Explorers kopieren und hier einfügen:")
        eingabe = st.text_input("Ordnerpfad", key="ordner_pfad_eingabe", label_visibility="collapsed")
        if st.button("Hinzufügen", disabled=not eingabe):
            ordner_hinzufuegen(eingabe)

    if st.button("Unterlagen jetzt neu prüfen", icon=":material/refresh:", help="Nach dem Hinzufügen oder Ändern von Dateien während der Assistent läuft."):
        st.session_state.pop("index_status", None)
        st.rerun()


# ── E-Mail-Zusammenfassung ────────────────────────────────────────────────────

def gespraech_als_text() -> str:
    return "\n\n".join(
        f"{'Frage' if n['rolle'] == 'user' else 'Antwort'}: {n['text']}"
        for n in st.session_state.chat
    )


def erzeuge_zusammenfassung() -> str:
    if not st.session_state.chat:
        return ""
    return frage_ki(
        "Fasse das folgende Gespräch eines WEG-Beirats mit seinem KI-Assistenten "
        "sachlich zusammen: die gestellten Fragen, die wichtigsten Antworten und "
        "genannten Quellen. Verständlich und kompakt, als Text für eine E-Mail "
        "an Miteigentümer. Keine Anrede, kein Gruß.\n\n" + gespraech_als_text()
    )


@st.dialog("Gespräch als E-Mail")
def email_dialog():
    stand = (st.session_state.gespraechs_id, len(st.session_state.chat))
    gespeichert = st.session_state.get("zusammenfassung")
    if not gespeichert or gespeichert[0] != stand:
        with st.spinner("Zusammenfassung wird erstellt …"):
            try:
                st.session_state.zusammenfassung = (stand, erzeuge_zusammenfassung())
            except Exception as e:
                st.error(fehlertext_ki(e, "Zusammenfassung fehlgeschlagen"))
                return
    zsf = st.session_state.zusammenfassung[1]
    st.caption("Sie können den Text hier noch ändern, bevor Sie ihn verschicken.")
    zsf = st.text_area("Zusammenfassung", zsf, height=280, label_visibility="collapsed")
    betreff = f"Beirat-Gespräch vom {datetime.now().strftime('%d.%m.%Y')}"
    mailto = ("mailto:?subject=" + urllib.parse.quote(betreff)
              + "&body=" + urllib.parse.quote(zsf[:1500]))
    links, rechts = st.columns(2)
    links.link_button("Im E-Mail-Programm öffnen", mailto, icon=":material/mail:", type="primary", use_container_width=True)
    rechts.download_button("Als Textdatei speichern", zsf, icon=":material/download:", file_name=f"{betreff}.txt",
                           use_container_width=True)
    if len(zsf) > 1500:
        st.caption("Hinweis: Ins E-Mail-Programm passen nur die ersten 1.500 Zeichen — "
                   "für den ganzen Text bitte die Textdatei nehmen.")


# ── Gespräch als PDF ──────────────────────────────────────────────────────────

PDF_ERKLAERUNG = {
    "Wie im Chat": "Fragen, Antworten und Quellen – so wie hier zu sehen",
    "Als Beschlussvorlage": "Sachverhalt, Beschlussvorschlag, Platz für das Abstimmungsergebnis",
    "Als Liste / Stichpunkte": "Kurz und übersichtlich",
    "Als Brief an Miteigentümer": "Mit Anrede und Grußformel",
    "Eigene Anweisung": "Sie beschreiben selbst, wie es aussehen soll",
}


@st.dialog("Gespräch als PDF")
def pdf_dialog():
    wahl = st.radio("Wie soll das PDF aussehen?", list(PDF_FORMAT_OPTIONEN),
                    captions=[PDF_ERKLAERUNG.get(k, "") for k in PDF_FORMAT_OPTIONEN])
    eigene = ""
    if wahl == "Eigene Anweisung":
        eigene = st.text_input("Wie soll die KI das Gespräch gestalten?",
                               placeholder="z. B. als Tabelle, als Protokollauszug, als Aushang …")
    if st.button("PDF erstellen", type="primary", disabled=wahl == "Eigene Anweisung" and not eigene.strip()):
        anweisung = PDF_FORMAT_OPTIONEN[wahl] or eigene.strip()
        with st.spinner("PDF wird erstellt …"):
            try:
                if anweisung:
                    doks, webs, gesehen = [], [], set()
                    for n in st.session_state.chat:
                        for d in n.get("doks", []):
                            if (d["datei"], d.get("seite", 0)) not in gesehen:
                                gesehen.add((d["datei"], d.get("seite", 0)))
                                doks.append(d)
                        webs += [w for w in n.get("webs", []) if w not in webs]
                    html = gestalte_fuer_pdf("Gesamtes Gespräch", gespraech_als_text(), anweisung)
                    titel = f"Beirat-Gespräch vom {datetime.now().strftime('%d.%m.%Y')}"
                    st.session_state.gespraech_pdf = pdf_export.erzeuge_pdf_antwort(titel, html, doks, webs)
                else:
                    st.session_state.gespraech_pdf = pdf_export.erzeuge_pdf_gespraech(st.session_state.chat)
            except Exception as e:
                st.session_state.pop("gespraech_pdf", None)
                st.error(fehlertext_ki(e, "PDF-Erstellung fehlgeschlagen"))
    if st.session_state.get("gespraech_pdf"):
        st.success("PDF ist fertig.")
        st.download_button(
            "PDF speichern", st.session_state.gespraech_pdf, icon=":material/download:", type="primary", use_container_width=True,
            file_name=f"Beirat-Gespraech_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf",
            mime="application/pdf",
        )


@st.dialog("Unterlagen-Ordner", width="large")
def ordner_dialog():
    ordner_ui()


# ── Seitenleiste ──────────────────────────────────────────────────────────────

def zeit_text(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return ""
    tage = (datetime.now().date() - t.date()).days
    if tage == 0:
        return f"Heute, {t:%H:%M}"
    if tage == 1:
        return f"Gestern, {t:%H:%M}"
    return f"{t:%d.%m.%Y}"


def seitenleiste(status: dict):
    st.markdown("#### Beirat-Assistent")
    st.button("Neues Gespräch", icon=":material/edit_square:", type="primary", use_container_width=True,
              on_click=wechsle_gespraech, disabled=not st.session_state.chat)

    st.caption("FRÜHERE GESPRÄCHE")
    gruppen = gruppiere_verlauf(lade_verlauf())
    if not gruppen:
        st.caption("Noch keine. Ihre Gespräche erscheinen hier automatisch.")
    for gruppe in gruppen[:20]:
        aktiv = gruppe["id"] == st.session_state.gespraechs_id
        erste = gruppe["eintraege"][0]["frage"]
        titel = erste[:42] + ("…" if len(erste) > 42 else "")
        anzahl = len(gruppe["eintraege"])
        spalte_titel, spalte_mehr = st.columns([6, 1], vertical_alignment="center")
        spalte_titel.button(
            titel, key=f"gespraech_{gruppe['id']}", use_container_width=True,
            type="secondary" if aktiv else "tertiary",
            help=f"{zeit_text(gruppe['letzte_zeit'])} · {anzahl} Frage{'n' if anzahl > 1 else ''}"
                 + (" · gerade geöffnet" if aktiv else " · Klicken zum Fortsetzen"),
            on_click=wechsle_gespraech, args=(gruppe["id"], gruppe_zu_chat(gruppe)),
        )
        with spalte_mehr.popover("", icon=":material/more_horiz:", help="Weitere Aktionen"):
            st.caption(f"{zeit_text(gruppe['letzte_zeit'])} · {anzahl} Frage(n)")
            if st.button("Gespräch endgültig löschen", key=f"loeschen_{gruppe['id']}", icon=":material/delete:"):
                loesche_gespraech(gruppe["id"])
                if aktiv:
                    wechsle_gespraech()
                st.rerun()
    if len(gruppen) > 20:
        st.caption(f"… und {len(gruppen) - 20} ältere Gespräche.")

    st.divider()
    st.button("Unterlagen-Ordner verwalten", icon=":material/folder_managed:", use_container_width=True,
              on_click=oeffne_dialog, args=("ordner",))
    if isinstance(status.get("fehler"), str):
        st.warning(f"Unterlagen-Prüfung fehlgeschlagen: {status['fehler'][:150]}")
        return
    st.caption(f"{pdf_indexer.anzahl_eingelesen()} Dokumente eingelesen")
    hintergrund_anzeige()
    if status.get("fehler"):
        with st.expander(f"{len(status['fehler'])} Datei(en) nicht einlesbar", icon=":material/warning:"):
            st.caption("Wird beim nächsten Start automatisch erneut versucht.")
            for name in status["fehler"]:
                st.markdown(f"- {name}")
    if status.get("ohne_text"):
        with st.expander(f"{len(status['ohne_text'])} Datei(en) ohne lesbaren Text", icon=":material/info:"):
            for name in status["ohne_text"]:
                st.markdown(f"- {name}")
    st.caption("Die Antworten ersetzen keine Rechtsberatung.")


# ── Knöpfe am Eingabefeld ─────────────────────────────────────────────────────

def eingabe_leiste():
    nur = st.session_state.get("nur_pfade", [])
    if nur:
        etikett = f"Nur {len(nur)} Dokument{'e' if len(nur) > 1 else ''}"
    else:
        etikett = "Alle Unterlagen"
    with st.container(horizontal=True, gap="small"):
        with st.popover(etikett, icon=":material/library_books:", help="Worin soll ich suchen?"):
            st.markdown("**Worin soll ich suchen?**")
            if nur:
                st.caption("Dieses Gespräch dreht sich nur um:")
                for p in nur:
                    st.markdown(f"- {Path(p).name}")
            else:
                st.caption("Gerade: in allen eingeschalteten Unterlagen-Ordnern.")
            st.button("Bestimmte Dokumente wählen …", icon=":material/checklist:", use_container_width=True,
                      on_click=oeffne_dialog, args=("dokumente",))
            if nur and st.button("Wieder alle Unterlagen", use_container_width=True):
                st.session_state.nur_pfade = []
                st.rerun()

        with st.popover(f"Internet: {st.session_state.web_modus}", icon=":material/language:", help="Im Internet nachsehen?"):
            st.radio("**Im Internet nachsehen?**", list(WEB_OPTIONEN), key="web_modus",
                     captions=list(WEB_OPTIONEN.values()), on_change=speichere_einstellungen)

        st.button("Vergleichen", icon=":material/compare:", on_click=oeffne_dialog, args=("vergleich",),
                  help="Zwei Dokumente gegenüberstellen")


# ── Oberfläche ────────────────────────────────────────────────────────────────

if not config.GEMINI_API_KEY:
    st.title("Beirat-Assistent")
    st.error(
        "Es fehlt noch der Google-KI-Schlüssel.\n\n"
        "Bitte einmal **einrichten.bat** doppelklicken (im Ordner des Assistenten)."
    )
    st.stop()

if "chat" not in st.session_state:
    wechsle_gespraech()
if "web_modus" not in st.session_state:
    st.session_state.update(lade_einstellungen())

if st.session_state.get("neuer_ordner"):
    st.session_state.index_status = indexiere_mit_anzeige(
        nur_ordner=st.session_state.pop("neuer_ordner")
    )
    st.rerun()

if "index_status" not in st.session_state:
    st.session_state.index_status = indexiere_mit_anzeige()

# Stufe 2 (Scans lesen, Suchvektoren) läuft im Hintergrund weiter
pdf_indexer.starte_hintergrund()

with st.sidebar:
    seitenleiste(st.session_state.index_status)

# Kopfzeile mit Titel und Gesprächs-Aktionen (immer sichtbar, bei leerem Gespräch ausgegraut)
leer = not st.session_state.chat
erste = "" if leer else st.session_state.chat[0]["text"]
spalte_titel, spalte_aktionen = st.columns([3, 2], vertical_alignment="center")
spalte_titel.markdown(f"#### {erste[:70]}{'…' if len(erste) > 70 else ''}" if erste else "&nbsp;")
hilfe_leer = " – zuerst eine Frage stellen" if leer else ""
with spalte_aktionen.container(horizontal=True, horizontal_alignment="right", gap="small"):
    st.button("Als PDF", icon=":material/picture_as_pdf:", disabled=leer,
              on_click=oeffne_dialog, args=("pdf",), help="Gespräch als PDF speichern" + hilfe_leer)
    st.button("Als E-Mail", icon=":material/mail:", disabled=leer,
              on_click=oeffne_dialog, args=("email",), help="Zusammenfassung für eine E-Mail" + hilfe_leer)

if st.session_state.get("nur_pfade"):
    namen = ", ".join(Path(p).name for p in st.session_state.nur_pfade[:3])
    mehr = len(st.session_state.nur_pfade) - 3
    st.info(f"Dieses Gespräch dreht sich nur um: **{namen}**"
            + (f" und {mehr} weitere" if mehr > 0 else ""), icon=":material/filter_alt:")
for hinweis in st.session_state.pop("auswahl_hinweise", []):
    st.warning(hinweis)

# Begrüßung bei leerem Gespräch
if not st.session_state.chat:
    st.markdown("<div style='text-align:center;padding-top:6vh'>"
                "<h2>Was möchten Sie wissen?</h2>"
                "<p style='opacity:.7'>Ich suche die Antwort in Ihren Unterlagen und nenne die Fundstellen.</p>"
                "</div>", unsafe_allow_html=True)
    with st.container(horizontal=True, horizontal_alignment="center", gap="small"):
        for beispiel in ("Wann endet der Verwaltervertrag?",
                         "Was wurde zuletzt beschlossen?",
                         "Wie hoch ist die Erhaltungsrücklage?"):
            if st.button(beispiel):
                st.session_state.vorschlag = beispiel

# Bisherige Nachrichten
for idx, nachricht in enumerate(st.session_state.chat):
    with st.chat_message("user" if nachricht["rolle"] == "user" else "assistant"):
        st.markdown(nachricht["text"])
        if nachricht["rolle"] == "assistent":
            zeige_quellen(nachricht.get("doks", []), nachricht.get("webs", []),
                          nachricht["text"], key_prefix=f"alt{idx}")

with st.bottom:
    eingabe_leiste()
    eingabe = st.chat_input("Frage stellen …")

dialog = st.session_state.pop("dialog", None)
if dialog == "dokumente":
    dokument_dialog()
elif dialog == "vergleich":
    vergleich_dialog()
elif dialog == "pdf":
    pdf_dialog()
elif dialog == "email":
    email_dialog()
elif dialog == "ordner":
    ordner_dialog()

frage = eingabe or st.session_state.pop("vorschlag", None)
if frage:
    st.session_state.chat.append({"rolle": "user", "text": frage})
    with st.chat_message("user"):
        st.markdown(frage)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Unterlagen werden durchsucht und Antwort erstellt …"):
                antwort, doks, webs = beantworte_frage(
                    frage, st.session_state.web_modus,
                    st.session_state.get("nur_pfade"),
                )
        except Exception as e:
            antwort = fehlertext_ki(e)
            doks, webs = [], []
        st.markdown(antwort)
    st.session_state.chat.append({"rolle": "assistent", "text": antwort, "doks": doks, "webs": webs})
    speichere_im_verlauf(frage, antwort, doks, webs)
    st.rerun()

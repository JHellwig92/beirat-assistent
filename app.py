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

st.set_page_config(page_title="Beirat-Assistent", page_icon="🏠", layout="centered")

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
    "Unverändert": None,
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


def loesche_gespraech(collection, gespraechs_id: str):
    """Entfernt alle Verlaufseinträge einer Gespräch-ID unwiderruflich —
    samt den Quellen, die nur für dieses Gespräch hinzugefügt wurden."""
    verlauf = lade_verlauf()
    verlauf = [e for e in verlauf if e["gespraechs_id"] != gespraechs_id]
    speichere_verlauf(verlauf)
    pdf_indexer.entferne_alle_gespraechs_quellen(collection, gespraechs_id)


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

@st.cache_resource
def hole_collection():
    _, collection = pdf_indexer.erstelle_client()
    return collection


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


def beantworte_frage(collection, frage: str, web_modus: str):
    doks = suche.suche_dokumente(collection, frage, st.session_state.gespraechs_id)
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

    st.markdown("**Quellen:**")
    schon_gezeigt = set()
    for i, d in gezeigt_doks:
        merkmal = (d["datei"], d.get("seite", 0))
        if merkmal in schon_gezeigt:
            continue
        schon_gezeigt.add(merkmal)
        ort = f" – Seite {d['seite']}" if d.get("seite") else ""
        if d.get("pfad"):
            if st.button(f"📄 [D{i}] {d['datei']}{ort}", key=f"{key_prefix}_d{i}"):
                try:
                    os.startfile(d["pfad"])
                except Exception:
                    st.warning(f"Datei konnte nicht geöffnet werden: {d['pfad']}")
        else:
            st.markdown(f"📄 [D{i}] {d['datei']}{ort}")
    for i, w in gezeigt_webs:
        if w.get("url"):
            st.markdown(f"🔗 [W{i}] [{w['titel'] or w['url']}]({w['url']})")


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


def pdf_export_ui(
    frage: str, antwort: str, key_prefix: str,
    doks: list[dict] | None = None, webs: list[dict] | None = None,
):
    state_key = f"{key_prefix}_pdf"
    with st.expander("📄 Als PDF speichern"):
        format_wahl = st.selectbox(
            "Formatierung", list(PDF_FORMAT_OPTIONEN.keys()), key=f"{state_key}_wahl"
        )
        eigene_anweisung = ""
        if format_wahl == "Eigene Anweisung":
            eigene_anweisung = st.text_input(
                "Wie soll die KI den Inhalt formatieren?",
                key=f"{state_key}_eigen",
                placeholder="z. B. als Flyer in Pink, als Tabelle, als Protokollauszug …",
            )
        if st.button("PDF erstellen", key=f"{state_key}_btn"):
            anweisung = PDF_FORMAT_OPTIONEN[format_wahl] or eigene_anweisung
            with st.spinner("PDF wird erstellt …"):
                try:
                    koerper_html = (
                        gestalte_fuer_pdf(frage, antwort, anweisung)
                        if anweisung else pdf_export.markdown_zu_html(antwort)
                    )
                    st.session_state[state_key] = pdf_export.erzeuge_pdf_antwort(
                        frage, koerper_html, doks=doks, webs=webs
                    )
                except Exception as e:
                    st.error(f"PDF-Erstellung fehlgeschlagen: {str(e)[:150]}")
        if st.session_state.get(state_key):
            dateiname = f"Beirat-Antwort_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf"
            st.download_button(
                "💾 PDF speichern unter …", st.session_state[state_key],
                file_name=dateiname, mime="application/pdf", key=f"{state_key}_dl",
            )


# ── Unterlagen einlesen & Ordner ──────────────────────────────────────────────

SCAN_HINWEIS = (
    "Eingescannte Dokumente werden dabei von der KI gelesen. Das kann einige "
    "Minuten dauern (bei vielen Unterlagen auch länger). Bitte das schwarze Fenster "
    "und den Browser geöffnet lassen."
)


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


def indexiere_mit_anzeige(collection, nur_ordner: str | None = None) -> dict:
    """Prüft/liest die Unterlagen ein und zeigt dabei einen Fortschrittsbalken."""
    if nur_ordner:
        titel = f"📂 **Neuer Ordner „{Path(nur_ordner).name}“ wird eingelesen …**"
    else:
        titel = "📂 **Unterlagen werden auf Änderungen geprüft …**"
    hinweis = ""
    if not pdf_indexer.anzahl_eingelesen() or nur_ordner:
        hinweis = SCAN_HINWEIS + " Falls doch unterbrochen wird: Beim nächsten Start geht es dort weiter."
    platz, fortschritt, meldung = fortschritts_box(titel, hinweis)
    try:
        status = pdf_indexer.pruefe_und_aktualisiere(collection, fortschritt, meldung, nur_ordner)
    except Exception as e:
        status = {"fehler": str(e)}
    platz.empty()
    return status


# ── Quellen nur für dieses Gespräch ───────────────────────────────────────────

def gespraechs_quellen_einlesen(collection, pfade: list[str]):
    """Liest Dateien/Ordner für das aktuelle Gespräch ein (mit Fortschrittsanzeige)."""
    gid = st.session_state.gespraechs_id
    platz, fortschritt, meldung = fortschritts_box(
        "📎 **Quellen für dieses Gespräch werden eingelesen …**", SCAN_HINWEIS
    )
    ok, ohne_text, fehler = [], [], []
    for pfad in pfade:
        try:
            erg = pdf_indexer.fuege_gespraechs_quelle_hinzu(collection, gid, pfad, fortschritt, meldung)
        except Exception as e:
            fehler.append(f"{Path(pfad).name} ({str(e)[:80]})")
            continue
        ok += erg["ok"]
        ohne_text += erg["ohne_text"]
        fehler += erg["fehler"]
    platz.empty()
    meldungen = []
    if ok:
        meldungen.append(("success", f"Für dieses Gespräch bereit: {', '.join(ok)}"))
    if ohne_text:
        meldungen.append(("warning", f"Kein lesbarer Text gefunden in: {', '.join(ohne_text)}"))
    if fehler:
        meldungen.append(("error", f"Konnte nicht eingelesen werden: {', '.join(fehler)} — "
                                   "bitte später noch einmal versuchen."))
    if not (ok or ohne_text or fehler):
        meldungen.append(("warning", "In der Auswahl waren keine PDF-, Word- oder Textdateien."))
    st.session_state.quellen_meldungen = meldungen


def gespraechs_quellen_ui(collection):
    st.subheader("📎 Nur für dieses Gespräch")
    st.caption("Dateien oder Ordner, die nur in diesem Gespräch durchsucht werden — "
               "z. B. Unterlagen für eine einzelne Sitzung.")
    gid = st.session_state.gespraechs_id
    for art, text in st.session_state.pop("quellen_meldungen", []):
        getattr(st, art)(text)

    for q in pdf_indexer.gespraechs_quellen(gid):
        symbol = "📁" if q["typ"] == "ordner" else "📄"
        zusatz = f" ({len(q['dateien'])} Dateien)" if q["typ"] == "ordner" else ""
        spalte_name, spalte_weg = st.columns([5, 1])
        spalte_name.markdown(f"{symbol} {q['name']}{zusatz}")
        schluessel = hashlib.md5(q["pfad"].encode()).hexdigest()[:10]
        if spalte_weg.button("✖", key=f"gq_weg_{schluessel}", help="Aus diesem Gespräch entfernen"):
            pdf_indexer.entferne_gespraechs_quelle(collection, gid, q["pfad"])
            st.rerun()

    zaehler = st.session_state.setdefault("upload_zaehler", 0)
    hochgeladen = st.file_uploader(
        "Dateien hinzufügen", type=["pdf", "docx", "txt"], accept_multiple_files=True,
        key=f"gq_upload_{zaehler}", label_visibility="collapsed",
    )
    if hochgeladen and st.button("📎 Diese Dateien verwenden", type="primary"):
        st.session_state.neue_gespraechs_quellen = [
            str(pdf_indexer.speichere_upload(gid, d.name, d.getvalue())) for d in hochgeladen
        ]
        st.session_state.upload_zaehler = zaehler + 1   # Auswahlfeld leeren
        st.rerun()

    if st.button("📁 Ordner für dieses Gespräch wählen"):
        with st.spinner("Bitte im aufgehenden Fenster einen Ordner auswählen …"):
            pfad = waehle_ordner_dialog()
        if pfad is None:
            st.warning("Das Auswahlfenster ließ sich nicht öffnen. Bitte die Dateien "
                       "stattdessen oben per „Browse files“ hochladen.")
        elif pfad:
            st.session_state.neue_gespraechs_quellen = [pfad]
            st.rerun()


# ── Dokumentenvergleich ───────────────────────────────────────────────────────

def _dokument_wahl(spalte, buchstabe: str, auswahl: list[dict]) -> str | None:
    """Ein Dokument aus den Unterlagen wählen oder hochladen. Gibt den Pfad zurück."""
    with spalte:
        st.markdown(f"**Dokument {buchstabe}**")
        hochgeladen = st.file_uploader(
            "Hochladen", type=["pdf", "docx", "txt"], key=f"vgl_upload_{buchstabe}",
            label_visibility="collapsed",
        )
        if hochgeladen:
            st.caption("Hochgeladene Datei wird verwendet.")
            return f"upload:{buchstabe}"
        index = st.selectbox(
            "… oder aus den Unterlagen", range(len(auswahl)), index=None,
            format_func=lambda i: f"{auswahl[i]['name']}  ·  {auswahl[i]['herkunft']}",
            placeholder="Dokument suchen …", key=f"vgl_wahl_{buchstabe}",
        )
        return auswahl[index]["pfad"] if index is not None else None


@st.dialog("⚖️ Dokumente vergleichen", width="large")
def vergleich_dialog(collection):
    gid = st.session_state.gespraechs_id
    auswahl = pdf_indexer.alle_dokumente(gid)
    st.caption("Zwei Dokumente wählen — aus den Unterlagen oder per Hochladen. "
               "Die KI liest beide vollständig und stellt sie gegenüber.")
    spalte_a, spalte_b = st.columns(2)
    wahl_a = _dokument_wahl(spalte_a, "A", auswahl)
    wahl_b = _dokument_wahl(spalte_b, "B", auswahl)
    schwerpunkt = st.text_input(
        "Worauf soll besonders geachtet werden? (freiwillig)",
        placeholder="z. B. Kosten und Fristen, Änderungen bei der Tiefgarage …",
    )
    if not st.button("Vergleichen", type="primary", disabled=not (wahl_a and wahl_b)):
        return
    if wahl_a == wahl_b:
        st.warning("Bitte zwei verschiedene Dokumente wählen.")
        return

    # Hochgeladene Dateien beim Gespräch ablegen und als Gesprächsquelle einlesen,
    # damit Rückfragen zum Vergleich auch darin suchen können.
    pfade = []
    for wahl, buchstabe in ((wahl_a, "A"), (wahl_b, "B")):
        if wahl.startswith("upload:"):
            datei = st.session_state[f"vgl_upload_{buchstabe}"]
            pfad = str(pdf_indexer.speichere_upload(gid, datei.name, datei.getvalue()))
            try:
                pdf_indexer.fuege_gespraechs_quelle_hinzu(collection, gid, pfad)
            except Exception:
                pass   # Vergleich klappt trotzdem; nur Rückfragen finden die Datei dann nicht
            pfade.append(pfad)
        else:
            pfade.append(wahl)

    detail = st.empty()
    with st.spinner("Dokumente werden gelesen und verglichen — das kann eine Minute dauern …"):
        try:
            antwort = vergleich.vergleiche(pfade[0], pfade[1], schwerpunkt, meldung=detail.caption)
        except Exception as e:
            st.error(fehlertext_ki(e, "Der Vergleich ist fehlgeschlagen"))
            return

    frage = f"⚖️ Vergleich: {Path(pfade[0]).name} ↔ {Path(pfade[1]).name}"
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


def ordner_ui(collection):
    st.subheader("📁 Unterlagen-Ordner")
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
            info += " · ⚠️ gerade nicht erreichbar (z. B. Stick abgezogen) — bereits Eingelesenes bleibt nutzbar"
        st.caption(info)
        if aktiv != o["aktiv"]:
            pdf_indexer.setze_ordner_aktiv(o["pfad"], aktiv)
            st.rerun()

        if not o["haupt"]:
            frage_key = f"ordner_weg_{schluessel}"
            if st.session_state.get(frage_key):
                st.warning(f"Ordner „{name}“ aus der Liste entfernen? Die Dateien selbst bleiben erhalten.")
                ja, nein = st.columns(2)
                if ja.button("Ja, entfernen", key=f"{frage_key}_ja"):
                    pdf_indexer.entferne_ordner(collection, o["pfad"])
                    st.session_state.pop(frage_key, None)
                    st.rerun()
                if nein.button("Abbrechen", key=f"{frage_key}_nein"):
                    st.session_state.pop(frage_key, None)
                    st.rerun()
            elif st.button("🗑️ Aus Liste entfernen", key=f"{frage_key}_btn"):
                st.session_state[frage_key] = True
                st.rerun()

    if ordner and not any(o["aktiv"] for o in ordner):
        st.warning("Kein Ordner eingeschaltet — der Assistent durchsucht gerade keine Unterlagen.")

    if st.button("➕ Ordner hinzufügen"):
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

    if st.button("🔄 Unterlagen jetzt neu prüfen", help="Nach dem Hinzufügen oder Ändern von Dateien während der Assistent läuft."):
        st.session_state.pop("index_status", None)
        st.rerun()


# ── E-Mail-Zusammenfassung ────────────────────────────────────────────────────

def erzeuge_zusammenfassung() -> str:
    nachrichten = st.session_state.chat
    if not nachrichten:
        return ""
    gespraech = "\n\n".join(
        f"{'Frage' if n['rolle'] == 'user' else 'Antwort'}: {n['text']}"
        for n in nachrichten
    )
    return frage_ki(
        "Fasse das folgende Gespräch eines WEG-Beirats mit seinem KI-Assistenten "
        "sachlich zusammen: die gestellten Fragen, die wichtigsten Antworten und "
        "genannten Quellen. Verständlich und kompakt, als Text für eine E-Mail "
        "an Miteigentümer. Keine Anrede, kein Gruß.\n\n" + gespraech
    )


# ── Oberfläche ────────────────────────────────────────────────────────────────

st.title("🏠 Beirat-Assistent")

if not config.GEMINI_API_KEY:
    st.error(
        "Es fehlt noch der Google-KI-Schlüssel.\n\n"
        "Bitte einmal **einrichten.bat** doppelklicken (im Ordner des Assistenten)."
    )
    st.stop()

collection = hole_collection()

if "chat" not in st.session_state:
    st.session_state.chat = []

if "gespraechs_id" not in st.session_state:
    st.session_state.gespraechs_id = str(uuid.uuid4())

if st.session_state.get("neuer_ordner"):
    st.session_state.index_status = indexiere_mit_anzeige(
        collection, nur_ordner=st.session_state.pop("neuer_ordner")
    )
    st.rerun()

if "index_status" not in st.session_state:
    st.session_state.index_status = indexiere_mit_anzeige(collection)

if st.session_state.get("neue_gespraechs_quellen"):
    gespraechs_quellen_einlesen(collection, st.session_state.pop("neue_gespraechs_quellen"))
    st.rerun()

status = st.session_state.index_status
with st.sidebar:
    st.header("ℹ️ Übersicht")
    if isinstance(status.get("fehler"), str):
        st.warning(f"Unterlagen-Prüfung fehlgeschlagen: {status['fehler'][:150]}")
    else:
        st.markdown(f"**{status['gesamt']}** Textabschnitte aus den Unterlagen geladen")
        if status["neu"] or status["aktualisiert"] or status["geloescht"]:
            st.caption(
                f"Gerade aktualisiert: {status['neu']} neu, "
                f"{status['aktualisiert']} geändert, {status['geloescht']} entfernt"
            )
        if status.get("kontingent"):
            st.warning(
                f"{status['offen']} Datei(en) konnten heute noch nicht eingelesen werden — "
                "das Tageslimit der kostenlosen KI ist erreicht. Sie werden beim nächsten "
                "Start automatisch nachgeholt."
            )
        if status.get("fehler"):
            with st.expander(f"⚠️ {len(status['fehler'])} Datei(en) nicht einlesbar"):
                st.caption("Wird beim nächsten Start automatisch erneut versucht.")
                for name in status["fehler"]:
                    st.markdown(f"- {name}")
        if status.get("ohne_text"):
            with st.expander(f"ℹ️ {len(status['ohne_text'])} Datei(en) ohne lesbaren Text"):
                st.caption("Diese Dateien enthalten keinen Text, den der Assistent nutzen kann.")
                for name in status["ohne_text"]:
                    st.markdown(f"- {name}")

    st.divider()
    ordner_ui(collection)
    st.divider()
    gespraechs_quellen_ui(collection)
    st.divider()
    st.subheader("⚖️ Dokumente vergleichen")
    vergleich_oeffnen = st.button("Zwei Dokumente vergleichen …")
    st.divider()

    web_modus = st.radio(
        "🌐 Internetsuche",
        ["Automatisch", "Immer", "Nie"],
        help="Automatisch: Das Internet wird nur befragt, wenn die eigenen "
             "Unterlagen nicht ausreichen.",
    )

    if st.button("🆕 Neues Gespräch beginnen"):
        st.session_state.chat = []
        st.session_state.gespraechs_id = str(uuid.uuid4())
        st.session_state.pop("zusammenfassung", None)
        st.session_state.pop("gespraech_pdf", None)
        st.rerun()

    st.divider()
    st.subheader("📧 Zusammenfassung")
    if st.button("Gespräch zusammenfassen", disabled=not st.session_state.chat):
        with st.spinner("Zusammenfassung wird erstellt …"):
            try:
                st.session_state.zusammenfassung = erzeuge_zusammenfassung()
            except Exception as e:
                text = str(e)
                if "503" in text or "overloaded" in text.lower() or "unavailable" in text.lower():
                    st.warning("Die KI ist gerade stark ausgelastet. Bitte eine Minute warten und es erneut versuchen.")
                elif "RESOURCE_EXHAUSTED" in text or "429" in text:
                    st.warning("Die kostenlose KI-Stufe ist kurz ausgeschöpft. Bitte eine Minute warten und es erneut versuchen.")
                else:
                    st.error(f"Zusammenfassung fehlgeschlagen: {text[:150]}")
    if st.session_state.get("zusammenfassung"):
        zsf = st.session_state.zusammenfassung
        st.text_area("Zusammenfassung", zsf, height=200)
        betreff = f"Beirat-Gespräch vom {datetime.now().strftime('%d.%m.%Y')}"
        mailto = (
            "mailto:?subject=" + urllib.parse.quote(betreff)
            + "&body=" + urllib.parse.quote(zsf[:1500])
        )
        st.link_button("📧 Im E-Mail-Programm öffnen", mailto)
        st.download_button(
            "💾 Als Textdatei speichern", zsf,
            file_name=f"{betreff}.txt",
        )

    st.divider()
    st.subheader("📄 Gespräch als PDF")
    if st.button("Gesamtes Gespräch als PDF erstellen", disabled=not st.session_state.chat):
        with st.spinner("PDF wird erstellt …"):
            try:
                st.session_state.gespraech_pdf = pdf_export.erzeuge_pdf_gespraech(
                    st.session_state.chat
                )
            except Exception as e:
                st.error(f"PDF-Erstellung fehlgeschlagen: {str(e)[:150]}")
    if st.session_state.get("gespraech_pdf"):
        st.download_button(
            "💾 PDF speichern unter …", st.session_state.gespraech_pdf,
            file_name=f"Beirat-Gespraech_{datetime.now().strftime('%Y-%m-%d_%H%M')}.pdf",
            mime="application/pdf",
        )

    st.divider()
    st.subheader("🕐 Frühere Gespräche")
    gruppen = gruppiere_verlauf(lade_verlauf())
    if not gruppen:
        st.caption("Noch keine früheren Gespräche.")
    for gruppe in gruppen[:15]:
        zeit = gruppe["eintraege"][0].get("zeit", "")[:16].replace("T", " ")
        erste_frage = gruppe["eintraege"][0]["frage"][:45]
        aktiv = gruppe["id"] == st.session_state.gespraechs_id
        titel = f"{'🟢 ' if aktiv else ''}{zeit} · {len(gruppe['eintraege'])} Frage(n) · {erste_frage}"
        loesch_key = f"loeschen_{gruppe['id']}"
        with st.expander(titel):
            for e in gruppe["eintraege"]:
                st.markdown(f"**Frage:** {e['frage']}")
                st.caption(e["antwort"][:300] + ("…" if len(e["antwort"]) > 300 else ""))

            if st.session_state.get(loesch_key):
                st.warning("Dieses Gespräch wirklich unwiderruflich löschen?")
                spalte_ja, spalte_nein = st.columns(2)
                if spalte_ja.button("Ja, löschen", key=f"{loesch_key}_ja"):
                    loesche_gespraech(collection, gruppe["id"])
                    st.session_state.pop(loesch_key, None)
                    if aktiv:
                        st.session_state.chat = []
                        st.session_state.gespraechs_id = str(uuid.uuid4())
                        st.session_state.pop("zusammenfassung", None)
                        st.session_state.pop("gespraech_pdf", None)
                    st.rerun()
                if spalte_nein.button("Abbrechen", key=f"{loesch_key}_nein"):
                    st.session_state.pop(loesch_key, None)
                    st.rerun()
            elif aktiv:
                st.caption("Das ist das aktuell geöffnete Gespräch.")
                if st.button("🗑️ Löschen", key=f"{loesch_key}_btn"):
                    st.session_state[loesch_key] = True
                    st.rerun()
            else:
                spalte_fortsetzen, spalte_loeschen = st.columns(2)
                if spalte_fortsetzen.button("▶️ Fortsetzen", key=f"fortsetzen_{gruppe['id']}"):
                    st.session_state.chat = gruppe_zu_chat(gruppe)
                    st.session_state.gespraechs_id = gruppe["id"]
                    st.session_state.pop("zusammenfassung", None)
                    st.session_state.pop("gespraech_pdf", None)
                    st.rerun()
                if spalte_loeschen.button("🗑️ Löschen", key=f"{loesch_key}_btn"):
                    st.session_state[loesch_key] = True
                    st.rerun()
    if len(gruppen) > 15:
        st.caption(f"… und {len(gruppen) - 15} weitere ältere Gespräche.")

    st.caption("Hinweis: Die Antworten ersetzen keine Rechtsberatung.")

if vergleich_oeffnen:
    vergleich_dialog(collection)

# Bisherige Nachrichten dieser Sitzung anzeigen
for idx, nachricht in enumerate(st.session_state.chat):
    with st.chat_message("user" if nachricht["rolle"] == "user" else "assistant"):
        st.markdown(nachricht["text"])
        if nachricht["rolle"] == "assistent":
            zeige_quellen(
                nachricht.get("doks", []), nachricht.get("webs", []),
                nachricht["text"], key_prefix=f"alt{idx}",
            )
            vorherige_frage = st.session_state.chat[idx - 1]["text"] if idx > 0 else ""
            pdf_export_ui(
                vorherige_frage, nachricht["text"], key_prefix=f"alt{idx}",
                doks=nachricht.get("doks", []), webs=nachricht.get("webs", []),
            )

frage = st.chat_input("Frage stellen — z. B.: Wer zahlt die Erneuerung der Rolltore?")
if frage:
    st.session_state.chat.append({"rolle": "user", "text": frage})
    with st.chat_message("user"):
        st.markdown(frage)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Unterlagen werden durchsucht und Antwort erstellt …"):
                antwort, doks, webs = beantworte_frage(collection, frage, web_modus)
        except Exception as e:
            antwort = fehlertext_ki(e)
            doks, webs = [], []
        st.markdown(antwort)
        zeige_quellen(doks, webs, antwort, key_prefix=f"neu{len(st.session_state.chat)}")
        pdf_export_ui(
            frage, antwort, key_prefix=f"neu{len(st.session_state.chat)}",
            doks=doks, webs=webs,
        )

    st.session_state.chat.append(
        {"rolle": "assistent", "text": antwort, "doks": doks, "webs": webs}
    )
    speichere_im_verlauf(frage, antwort, doks, webs)

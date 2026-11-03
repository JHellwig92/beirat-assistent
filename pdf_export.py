"""PDF-Erzeugung aus HTML — für einzelne Antworten und den gesamten Gesprächsverlauf.

Der Inhalt kommt entweder aus der lokalen Markdown->HTML-Umwandlung (Standardfall,
unverändert) oder direkt als von der KI geschriebenes HTML mit Inline-CSS (wenn der
Nutzer eine Formatierung/Gestaltung wie "als Flyer in Pink" gewünscht hat). So kann
die KI Farben, Hervorhebungen und Gliederung wirklich selbst bestimmen, statt dass der
Code Stichwörter errät.
"""
import html as _html
import re
from datetime import datetime
from io import BytesIO

from xhtml2pdf import pisa


# ── Sicherheit ────────────────────────────────────────────────────────────────
# Die KI schreibt das HTML; auch wenn sie angewiesen ist, nur einfache Tags zu
# nutzen, werden potenziell gefährliche/unerwünschte Elemente sicherheitshalber
# entfernt, bevor daraus ein PDF gerendert wird (kein Nachladen externer Bilder,
# kein Skript-Code, keine javascript:-Links).
_GEFAEHRLICHE_TAGS = re.compile(
    r"<(script|iframe|object|embed|img)\b[^>]*>(?:.*?</\1>)?", re.I | re.S
)
_EVENT_ATTRIBUT = re.compile(r'\s+on\w+\s*=\s*"[^"]*"', re.I)
_JS_LINK = re.compile(r'(href\s*=\s*")javascript:[^"]*(")', re.I)


def _sanitisiere_html(fragment: str) -> str:
    fragment = _GEFAEHRLICHE_TAGS.sub("", fragment)
    fragment = _EVENT_ATTRIBUT.sub("", fragment)
    fragment = _JS_LINK.sub(r"\1#\2", fragment)
    return fragment


def _escape(text: str) -> str:
    return _html.escape(text or "", quote=False)


# ── Markdown -> HTML (für unveränderte Antworten, ohne KI-Aufruf) ─────────────

def _inline_html(text: str) -> str:
    text = _escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", text)
    return text


_TABELLEN_TRENNER = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _ist_tabellenzeile(zeile: str) -> bool:
    z = zeile.strip()
    return z.startswith("|") and z.endswith("|") and len(z) > 1


def _parse_tabellenzeile(zeile: str) -> list[str]:
    return [zelle.strip() for zelle in zeile.strip()[1:-1].split("|")]


def markdown_zu_html(text: str) -> str:
    """Wandelt die einfache Markdown-Gliederung der KI-Antworten (Überschriften,
    Listen, Tabellen, **fett**/*kursiv*) in HTML um — ohne dafür die KI erneut
    aufzurufen."""
    zeilen = text.split("\n")
    ausgabe: list[str] = []
    offene_liste: str | None = None

    def liste_schliessen():
        nonlocal offene_liste
        if offene_liste:
            ausgabe.append(f"</{offene_liste}>")
            offene_liste = None

    i, n = 0, len(zeilen)
    while i < n:
        z = zeilen[i].rstrip()
        gestrippt = z.lstrip()

        if _ist_tabellenzeile(z) and i + 1 < n and _TABELLEN_TRENNER.match(zeilen[i + 1].strip()):
            liste_schliessen()
            kopf = _parse_tabellenzeile(z)
            i += 2
            datenzeilen = []
            while i < n and _ist_tabellenzeile(zeilen[i]):
                datenzeilen.append(_parse_tabellenzeile(zeilen[i]))
                i += 1
            ausgabe.append("<table>")
            ausgabe.append("<tr>" + "".join(f"<th>{_inline_html(c)}</th>" for c in kopf) + "</tr>")
            for row in datenzeilen:
                zellen = row + [""] * (len(kopf) - len(row))
                ausgabe.append("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in zellen[:len(kopf)]) + "</tr>")
            ausgabe.append("</table>")
            continue

        if not gestrippt:
            liste_schliessen()
        elif re.match(r"^(-{3,}|_{3,})$", gestrippt):
            liste_schliessen()
            ausgabe.append("<hr>")
        elif z.startswith("### "):
            liste_schliessen()
            ausgabe.append(f"<h3>{_inline_html(z[4:])}</h3>")
        elif z.startswith("## ") or z.startswith("# "):
            liste_schliessen()
            ausgabe.append(f"<h2>{_inline_html(z.lstrip('#').strip())}</h2>")
        elif gestrippt.startswith(("- ", "* ")):
            if offene_liste != "ul":
                liste_schliessen()
                ausgabe.append("<ul>")
                offene_liste = "ul"
            ausgabe.append(f"<li>{_inline_html(gestrippt[2:])}</li>")
        elif re.match(r"^\d+\.\s", gestrippt):
            if offene_liste != "ol":
                liste_schliessen()
                ausgabe.append("<ol>")
                offene_liste = "ol"
            ausgabe.append(f"<li>{_inline_html(re.sub(r'^\\d+\\.\\s', '', gestrippt))}</li>")
        elif re.match(r"^\*\*(.+)\*\*$", gestrippt):
            # Ganze Zeile in **...** (von der KI oft als Abschnitts-Überschrift
            # ohne "#" genutzt, z. B. bei Beschlussvorlagen) -> als Überschrift.
            liste_schliessen()
            ausgabe.append(f"<h3>{_inline_html(gestrippt[2:-2])}</h3>")
        else:
            liste_schliessen()
            ausgabe.append(f"<p>{_inline_html(z)}</p>")
        i += 1
    liste_schliessen()
    return "\n".join(ausgabe)


# ── Quellen als klickbare Links ────────────────────────────────────────────────

def _datei_uri(pfad: str) -> str:
    return "file:///" + str(pfad).replace("\\", "/").lstrip("/")


def _quellen_html(doks: list[dict], webs: list[dict]) -> str:
    if not doks and not webs:
        return ""
    teile = ['<h3>Quellen</h3>', '<ul class="quellen">']
    gezeigt = set()
    di = 0
    for d in doks:
        merkmal = (d.get("datei"), d.get("seite", 0))
        if merkmal in gezeigt:
            continue
        gezeigt.add(merkmal)
        di += 1
        ort = f", Seite {d['seite']}" if d.get("seite") else ""
        label = _escape(f"[D{di}] {d.get('datei', '')}{ort}")
        if d.get("pfad"):
            teile.append(f'<li><a href="{_escape(_datei_uri(d["pfad"]))}">{label}</a></li>')
        else:
            teile.append(f"<li>{label}</li>")
    for wi, w in enumerate(webs, 1):
        if not w.get("url"):
            continue
        label = _escape(f"[W{wi}] {w.get('titel') or w['url']}")
        teile.append(f'<li><a href="{_escape(w["url"])}">{label}</a></li>')
    teile.append("</ul>")
    return "\n".join(teile)


# ── PDF-Rendering ──────────────────────────────────────────────────────────────

_BASIS_CSS = """
@page {
    size: A4;
    margin: 2.2cm 2cm 2.2cm 2cm;
    @frame footer_frame {
        -pdf-frame-content: footer_content;
        bottom: 1cm; margin-left: 2cm; margin-right: 2cm; height: 1cm;
    }
}
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; color: #222222; line-height: 1.4; }
h1 { font-size: 16pt; margin: 0 0 2pt 0; }
h2 { font-size: 13pt; margin: 14pt 0 4pt 0; }
h3 { font-size: 11.5pt; margin: 10pt 0 4pt 0; }
p { margin: 4pt 0; }
ul, ol { margin: 4pt 0; padding-left: 18pt; }
li { margin: 2pt 0; }
table { width: 100%; margin: 8pt 0; }
th, td { border: 1px solid #999999; padding: 4px; text-align: left; font-size: 10pt; }
th { background-color: #eeeeee; }
a { color: #1a5fb4; }
hr { border: none; border-top: 1px solid #cccccc; margin: 10pt 0; }
.kopf { color: #666666; font-size: 8.5pt; margin-bottom: 6pt; }
.quellen { list-style: none; padding-left: 0; font-size: 10pt; }
.footer { font-size: 8pt; color: #999999; text-align: center; }
"""


def erzeuge_pdf_aus_html(koerper_html: str, titel: str) -> bytes:
    zeitstempel = datetime.now().strftime("%d.%m.%Y um %H:%M")
    dokument = f"""<html><head><meta charset="utf-8"><style>{_BASIS_CSS}</style></head>
<body>
<div id="footer_content" class="footer"><pdf:pagenumber/> / <pdf:pagecount/></div>
<h1>{_escape(titel)}</h1>
<div class="kopf">Erstellt am {zeitstempel} Uhr</div>
<hr>
{_sanitisiere_html(koerper_html)}
</body></html>"""
    out = BytesIO()
    ergebnis = pisa.CreatePDF(dokument, dest=out, encoding="utf-8")
    if ergebnis.err:
        raise RuntimeError("PDF-Erzeugung fehlgeschlagen (ungültiges HTML).")
    return out.getvalue()


def erzeuge_pdf_antwort(
    frage: str, koerper_html: str, doks: list[dict] | None = None, webs: list[dict] | None = None
) -> bytes:
    """Baut ein PDF aus einer einzelnen Frage und bereits fertigem HTML-Inhalt
    (lokal aus Markdown erzeugt oder von der KI gestaltet), inkl. Quellen."""
    teile = []
    if frage:
        teile.append(f"<p><b>Frage:</b> {_escape(frage)}</p>")
    teile.append(koerper_html)
    teile.append(_quellen_html(doks or [], webs or []))
    return erzeuge_pdf_aus_html("\n".join(teile), "Beirat-Assistent – Ergebnis")


def erzeuge_pdf_gespraech(chat: list[dict]) -> bytes:
    """Baut ein PDF aus dem gesamten Gesprächsverlauf (Frage/Antwort-Inhalte + Quellen)."""
    teile = []
    for nachricht in chat:
        if nachricht["rolle"] == "user":
            teile.append(f"<p><b>Frage:</b> {_escape(nachricht['text'])}</p>")
        else:
            teile.append(markdown_zu_html(nachricht["text"]))
            teile.append(_quellen_html(nachricht.get("doks", []), nachricht.get("webs", [])))
            teile.append("<hr>")
    return erzeuge_pdf_aus_html("\n".join(teile), "Beirat-Assistent – Gesprächsverlauf")

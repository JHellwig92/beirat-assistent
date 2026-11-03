# Beirat-Assistent

Ein lokaler KI-Assistent für Verwaltungsbeiräte von Wohnungseigentümergemeinschaften (WEG).
Er durchsucht die eigenen Unterlagen (PDF, Word, Text, auch eingescannte Dokumente),
beantwortet Fragen mit Quellenangabe und kann zusätzlich im Internet suchen.

Läuft unter Windows als Browser-App (Streamlit). Für die Antworten dient Google Gemini,
dafür reicht ein kostenloser API-Schlüssel. Das Einlesen läuft komplett lokal über
[Ollama](https://ollama.com): Suchvektoren mit `qwen3-embedding`, Texterkennung von
Scans mit dem Vision-Modell `qwen3-vl` (kein Tageslimit; eine Grafikkarte mit ≥ 6 GB
beschleunigt das deutlich). Die Modelle werden nach jedem Arbeitsgang sofort wieder
entladen. Index, Verlauf und Einstellungen bleiben auf dem Rechner. An Gemini gehen
die jeweils relevanten Textauszüge, beim Dokumentenvergleich die beiden Dokumente.

## Funktionen
- Fragen zu den eigenen Unterlagen. Jede Quelle lässt sich per Klick öffnen.
- Texterkennung für eingescannte PDFs (lokal mit `qwen3-vl`, Zwischenspeicher je Seite;
  Gemini nur als Ersatz, falls Ollama fehlt).
- Mehrere Unterlagen-Ordner mit Ein/Aus-Schalter.
- Dateien oder Ordner nur für ein einzelnes Gespräch.
- Dokumentenvergleich (zwei Dokumente, mit optionalem Schwerpunkt).
- Gesprächsverlauf, PDF-Export, E-Mail-Zusammenfassung.
- Zweistufiges Einlesen: Dateien sind nach kurzer Zeit per Stichwort auffindbar;
  Texterkennung und Suchindex laufen im Hintergrund weiter.
- Automatische Updates beim Start.

## Einrichtung (Windows)
1. Den neuesten Stand unter **Releases** als ZIP herunterladen und entpacken,
   z. B. nach `C:\BeiratAssistent`.
2. Unter <https://aistudio.google.com/apikey> einen kostenlosen Gemini-API-Schlüssel erstellen.
   [Ollama](https://ollama.com/download) installieren (empfohlen). Das Suchmodell lädt der
   Assistent beim ersten Start selbst herunter (ca. 640 MB Suchmodell, 3,3 GB für die
   Texterkennung). Ohne Ollama funktioniert die Suche nur über Stichworte, und Scans
   werden über Gemini gelesen.
3. `einrichten.bat` doppelklicken und den Schritten folgen (Schlüssel, Unterlagen-Ordner).
4. Danach täglich nur noch `start.bat` doppelklicken.

Die Bedienung ist in [`ANLEITUNG.txt`](ANLEITUNG.txt) beschrieben.

## Updates
`start.bat` → `starten.py` → `aktualisieren.py` prüft bei jedem Start das neueste
**GitHub-Release** dieses Repos. Ist es neuer als `version.txt`, werden die
Programmdateien ersetzt. Bei geänderter `requirements.txt` werden die Pakete
nachinstalliert. `.env`, `beirat_daten/` und `.venv/` werden nie angefasst. Bei
Fehlern wird die vorherige Version wiederhergestellt; ohne Internet startet einfach
die bisherige Version. Abschalten: `AUTO_UPDATE=aus` in der `.env`.

**Neue Version ausliefern:**
```bash
# version.txt anpassen, committen, pushen — dann:
gh release create v1.0.1 --title "1.0.1" --notes "- Was ist neu (erscheint beim Update im Fenster)"
```
Nur Releases werden ausgeliefert, normale Pushes auf `main` nicht.

## Einstellungen (`.env`)
| Schlüssel | Bedeutung |
|---|---|
| `GEMINI_API_KEY` | API-Schlüssel (Pflicht) |
| `PDF_ORDNER` | Hauptordner mit den Unterlagen |
| `LAND` | `DE` oder `AT` (Websuche) |
| `GEMINI_MODELL` | Chat-Modell, Standard `gemini-2.5-flash` |
| `OLLAMA_URL` | Ollama-Adresse, Standard `http://127.0.0.1:11434` |
| `OLLAMA_EMBED_MODELL` | Suchmodell, Standard `qwen3-embedding:0.6b` |
| `TEXTERKENNUNG` | `lokal` (Standard) oder `gemini` |
| `OLLAMA_OCR_MODELL` | Modell für die Texterkennung, Standard `qwen3-vl:4b` |
| `KI_TEMPOLIMIT` | `aus` bei bezahltem Gemini-Zugang (nur für `TEXTERKENNUNG=gemini`) |
| `AUTO_UPDATE` | `aus`, um automatische Updates abzuschalten |

## Hinweis
Die Antworten sind eine Orientierungshilfe und ersetzen keine Rechtsberatung.

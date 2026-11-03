"""Internetsuche über DuckDuckGo (Paket: ddgs)."""
import re

import config

# DuckDuckGo liefert über die ddgs-Bibliothek gelegentlich mehrere Ergebniszeilen
# ohne Trennzeichen aneinandergeklebt zurück (Artefakt bei Layout-Änderungen der
# Ergebnisseite). Erkennbar an mehreren Kleinbuchstabe→Großbuchstabe-Übergängen
# ohne Leerzeichen dazwischen — dann wird nur der erste, echte Titel verwendet.
_KLEIN_GROSS_UEBERGANG = re.compile(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])")


def _bereinige_titel(titel: str) -> str:
    titel = (titel or "").strip()
    if not titel:
        return titel
    teile = _KLEIN_GROSS_UEBERGANG.split(titel)
    if len(teile) > 2:
        return teile[0].strip()
    if len(titel) > 140:
        return titel[:140].rsplit(" ", 1)[0] + "…"
    return titel


def suche_web(anfrage: str) -> list[dict]:
    """Sucht im Internet und gibt Treffer als Liste von Dicts zurück."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # alter Paketname als Rückfallebene
        except ImportError:
            return []

    region = "de-de" if config.LAND.upper() == "DE" else "at-de"
    treffer = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(anfrage, region=region, max_results=config.MAX_WEB_ERGEBNISSE):
                treffer.append({
                    "titel": _bereinige_titel(r.get("title", "")),
                    "url": r.get("href", ""),
                    "text": r.get("body", ""),
                })
    except Exception as e:
        print(f"[!] Websuche fehlgeschlagen: {e}")
    return treffer

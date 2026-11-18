"""
bijz_en.py — Bijzonderheden invoeren in het Engels (voor Engelstalige gebruikers).

Opgeslagen wordt altijd Nederlandse tekst: "jaar, NNNpp, vorm, staat, opmerkingen".
Opmerkingen die als EN zijn aangegeven worden bij het opslaan via DeepL naar het Nederlands vertaald.
De Engelse versie bewaren we in shop_translations, zodat de webshop niet nog eens hoeft te vertalen.

Aanpassen? Pas hieronder de keuzes aan: (Engelse tekst, Nederlandse tekst die wordt opgeslagen).
"""

import hashlib

import psycopg2
import requests
import streamlit as st

from common import _get_secret, get_db_url
from i18n import t

CONDITIONS = [
    ("New in plastic", "nieuw in plastic"),
    ("Like new", "als nieuw"),
    ("Used", "gebruikt"),
    ("Traces of use", "met gebruikssporen"),
]
BINDINGS = [("Hardcover", "gebonden"), ("Paperback", "paperback")]


def deepl_en_to_nl(text):
    """Vertaal EN -> NL met DeepL. Geeft (tekst, foutmelding of None)."""
    key = _get_secret("DEEPL_API_KEY") or _get_secret("WEBSHOP_DEEPL_KEY")
    if not key:
        return text, "No DeepL key found in the secrets (DEEPL_API_KEY)."
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    try:
        r = requests.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            data={"text": text, "source_lang": "EN", "target_lang": "NL"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["translations"][0]["text"], None
    except Exception as e:
        return text, f"DeepL translation failed: {e}"


def _store_english(nl_text, en_text):
    """Bewaar de Engelse oorspronkelijke tekst voor de webshop (best effort)."""
    try:
        conn = psycopg2.connect(get_db_url())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS shop_translations (
                        lang TEXT NOT NULL, src_hash TEXT NOT NULL, src TEXT NOT NULL, tgt TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (lang, src_hash))"""
                )
                cur.execute(
                    "INSERT INTO shop_translations (lang, src_hash, src, tgt) VALUES ('en', %s, %s, %s) ON CONFLICT DO NOTHING",
                    (hashlib.sha1(nl_text.encode("utf-8")).hexdigest(), nl_text, en_text),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def bijz_input(key, new=True):
    """
    Invoerveld(en) voor Engelstalige gebruikers. 'key' is de sessiesleutel van het vrije tekstveld
    (zodat voorinvullen blijft werken). Geeft een 'state' dict terug voor finalize().
    """
    state = {"key": key}
    year = pages = 0
    binding = condition = ""
    if new:
        c1, c2, c3, c4 = st.columns(4)
        year = c1.number_input(t("Jaar"), min_value=0, max_value=2100, value=0, step=1, key=f"{key}_year", help=t("0 = leeg laten"))
        pages = c2.number_input(t("Pagina's"), min_value=0, max_value=20000, value=0, step=1, key=f"{key}_pages", help=t("0 = leeg laten"))
        binding = c3.selectbox(t("Vorm"), [""] + [b[0] for b in BINDINGS], key=f"{key}_binding", format_func=lambda v: v or t("(leeg)"))
        condition = c4.selectbox(t("Staat"), [""] + [c[0] for c in CONDITIONS], key=f"{key}_cond", format_func=lambda v: v or t("(leeg)"))
    else:
        condition = st.selectbox(
            t("Staat toevoegen"), [""] + [c[0] for c in CONDITIONS], key=f"{key}_cond",
            format_func=lambda v: v or t("(leeg)"),
        )
    lang_key = f"{key}_lang"
    if lang_key not in st.session_state:
        st.session_state[lang_key] = "EN" if (new and not st.session_state.get(key)) else "NL"
    lang = st.radio(t("Taal van de opmerkingen"), ["EN", "NL"], horizontal=True, key=lang_key)
    st.text_area(t("Opmerkingen (Bijzonderheden)"), height=100, key=key)
    state.update(year=int(year or 0), pages=int(pages or 0), binding=binding, condition=condition, lang=lang)
    return state


def finalize(state):
    """Maakt van de invoer de Nederlandse tekst die wordt opgeslagen (vertaalt zo nodig via DeepL)."""
    notes = (st.session_state.get(state["key"]) or "").strip()
    en_notes = notes
    if notes and state["lang"] == "EN":
        notes, err = deepl_en_to_nl(notes)
        if err:
            st.warning(err + " " + t("De opmerkingen zijn onvertaald opgeslagen."))
    binding_nl = dict(BINDINGS).get(state["binding"], "")
    cond_nl = dict(CONDITIONS).get(state["condition"], "")
    nl_parts, en_parts = [], []
    if state["year"]:
        nl_parts.append(str(state["year"])); en_parts.append(str(state["year"]))
    if state["pages"]:
        nl_parts.append(f"{state['pages']}pp"); en_parts.append(f"{state['pages']}pp")
    if binding_nl:
        nl_parts.append(binding_nl); en_parts.append(state["binding"].lower())
    if cond_nl:
        nl_parts.append(cond_nl); en_parts.append(state["condition"].lower())
    if notes:
        nl_parts.append(notes); en_parts.append(en_notes if state["lang"] == "EN" else notes)
    nl_text = ", ".join(nl_parts)
    if state["lang"] == "EN" and nl_text and notes != en_notes:
        _store_english(nl_text, ", ".join(en_parts))
    return nl_text

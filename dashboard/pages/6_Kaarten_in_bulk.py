"""
pages/6_Kaarten_in_bulk.py — Veel kaarten tegelijk inlezen en daarna snel invullen.

Eerst de scans uploaden (voorkant 260112_01v.jpg en achterkant 260112_01a.jpg horen bij kaart 260112_01), daarna kaart voor kaart
de gegevens invullen. Een kaart zonder prijs telt als 'nog in te vullen'.
De bestandsnaam staat bewust op 6_: zo blijft deze pagina buiten het korte menu.
"""

from pathlib import Path

import streamlit as st

import kaarten_db as K
import kaarten_ui
from common import current_user_short_name, render_logo, require_login

st.set_page_config(page_title="Kaarten in bulk", page_icon="📦", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("📦 Kaarten in bulk")
K.toon_tabellen_melding()

tab_upload, tab_invullen = st.tabs(["1. Scans uploaden", "2. Invullen"])

# ---------------- 1. Scans uploaden ----------------
with tab_upload:
    st.caption(
        "Kies hier de scans van een aantal kaarten tegelijk (voorkant én achterkant; de bestandsnaam eindigt op v of a). "
        "Ze worden verkleind (langste zijde 1600 pixels) en opgeslagen. Verklein grote originele scans liever eerst met "
        "kaarten_verkleinen.py, dan gaat het uploaden veel sneller. Houd het bij een paar tientallen kaarten per keer als de scans groot zijn."
    )
    melding = st.session_state.pop("kb_gelukt", None)
    if melding:
        st.success(melding)
    n = st.session_state.setdefault("kb_teller", 0)
    bestanden = st.file_uploader("Scans", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key=f"kb_upload_{n}")

    if bestanden:
        paren, vreemd = {}, []
        for f in bestanden:
            nummer, kant = K.kaartnummer_uit_bestandsnaam(f.name)
            if not nummer:
                vreemd.append(f.name)
                continue
            paren.setdefault(nummer, {})[kant] = f
        compleet = {nr: d for nr, d in paren.items() if "v" in d and "a" in d}
        mist = {nr: d for nr, d in paren.items() if not ("v" in d and "a" in d)}
        bestaand = K.bestaande_nummers(compleet)
        nieuw = [nr for nr in sorted(compleet) if nr not in bestaand]

        st.markdown(f"**{len(nieuw)}** kaarten klaar om op te slaan · {len(bestaand)} bestaan al · {len(mist)} met maar één scan · {len(vreemd)} onbekende bestandsnamen")
        if mist:
            st.warning("Een scan ontbreekt bij: " + ", ".join(f"{nr} (alleen {'voorkant' if 'v' in d else 'achterkant'})" for nr, d in sorted(mist.items()))
                       + ". Deze kaarten worden niet opgeslagen.")
        if vreemd:
            st.warning("Bestandsnaam eindigt niet op v of a: " + ", ".join(vreemd))
        if bestaand:
            st.info("Bestaan al, worden overgeslagen: " + ", ".join(sorted(bestaand)))

        if nieuw and st.button(f"{len(nieuw)} kaarten opslaan", type="primary", key="kb_opslaan"):
            voortgang = st.progress(0.0)
            gelukt, fouten = 0, []
            for i, nr in enumerate(nieuw):
                try:
                    K.maak_kaart_concept(nr, K.verklein_scan(compleet[nr]["v"].getvalue()),
                                         K.verklein_scan(compleet[nr]["a"].getvalue()), current_user_short_name())
                    gelukt += 1
                except Exception as exc:
                    fouten.append(f"{nr}: {exc}")
                voortgang.progress((i + 1) / len(nieuw))
            voortgang.empty()
            for f in fouten:
                st.error(f)
            st.session_state["kb_gelukt"] = f"{gelukt} kaarten opgeslagen. Ga naar 'Invullen' om de gegevens in te vullen."
            st.session_state["kb_teller"] = n + 1
            if not fouten:
                st.rerun()

# ---------------- 2. Invullen ----------------
with tab_invullen:
    totaal = K.aantal_onvolledig()
    overslaan = st.session_state.setdefault("kb_overslaan", [])
    st.caption(
        f"Nog in te vullen: **{totaal}** kaarten (kaarten zonder prijs), op volgorde van kaartnummer. "
        "Provincie, plaats, thema's, uitgever, kleur en prijs blijven staan voor de volgende kaart; titel, jaartal en opmerking worden leeg."
    )
    nummer = K.volgende_onvolledige(overslaan)
    if not nummer:
        if overslaan:
            st.info("Alle kaarten zijn ingevuld, behalve de overgeslagen kaarten.")
            if st.button("Overgeslagen kaarten opnieuw tonen", key="kb_reset_overslaan"):
                st.session_state["kb_overslaan"] = []
                st.rerun()
        else:
            st.success("Alle kaarten zijn ingevuld.")
        st.stop()

    kaart = K.get_kaart(nummer)
    links, rechts = st.columns([1, 1], gap="large")
    with links:
        st.subheader(f"Kaart {nummer}")
        st.image(kaart["voorkant"], caption="Voorkant", width="stretch")
        st.image(kaart["achterkant"], caption="Achterkant", width="stretch")
    with rechts:
        velden, themas, fout = kaarten_ui.velden_formulier("bw")
        col_a, col_b = st.columns([2, 1])
        if col_a.button("Opslaan en volgende", type="primary", key=f"kb_opslaan_{nummer}"):
            if fout:
                st.error(fout)
            else:
                try:
                    K.wijzig_kaart(nummer, velden, themas)
                except Exception as exc:
                    st.error(f"Opslaan is niet gelukt: {exc}")
                else:
                    for veld in ("titel", "jaar", "opmerking"):
                        st.session_state.pop(f"bw_{veld}", None)
                    st.rerun()
        if col_b.button("Overslaan", key=f"kb_skip_{nummer}"):
            st.session_state["kb_overslaan"] = overslaan + [nummer]
            st.rerun()

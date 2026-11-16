"""
pages/1_Nieuwe_kaart.py — Een ansichtkaart invoeren: twee scans (voorkant en achterkant) en de gegevens.

De bestandsnaam van de scan is het kaartnummer: 260112_01v.jpg (voorkant) en 260112_01a.jpg (achterkant) horen bij kaart 260112_01.
De bestandsnaam van "1_Nieuwe_kaart" staat bewust achter "1_Nieuw_boek": zo staat de pagina in het menu direct onder Nieuw boek.
"""

from pathlib import Path

import streamlit as st

import kaarten_db as K
import kaarten_ui
from common import current_user_short_name, render_logo, require_login

st.set_page_config(page_title="Nieuwe kaart", page_icon="📮", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("📮 Nieuwe kaart")
K.toon_tabellen_melding()

n = st.session_state.setdefault("nk_teller", 0)
meldingen = st.session_state.pop("nk_gelukt", None)
if meldingen:
    st.success(meldingen)

links, rechts = st.columns([1, 1], gap="large")

with links:
    st.markdown("**Scans** (altijd twee: voorkant én achterkant)")
    voor = st.file_uploader("Voorkant (bestandsnaam eindigt op v)", type=["jpg", "jpeg", "png"], key=f"nk_voor_{n}")
    if voor is not None:
        st.image(voor.getvalue(), width="stretch")
    achter = st.file_uploader("Achterkant (bestandsnaam eindigt op a)", type=["jpg", "jpeg", "png"], key=f"nk_achter_{n}")
    if achter is not None:
        st.image(achter.getvalue(), width="stretch")

# Kaartnummer uit de bestandsnamen halen en controleren
nr_v, kant_v = K.kaartnummer_uit_bestandsnaam(voor.name) if voor else (None, None)
nr_a, kant_a = K.kaartnummer_uit_bestandsnaam(achter.name) if achter else (None, None)
afgeleid = nr_v or nr_a or ""
if st.session_state.get("nk_afgeleid") != (n, afgeleid):
    st.session_state["nk_afgeleid"] = (n, afgeleid)
    if afgeleid:
        st.session_state[f"nk_nummer_{n}"] = afgeleid

with rechts:
    nummer = st.text_input("Kaartnummer *", key=f"nk_nummer_{n}", placeholder="bijvoorbeeld 260112_01",
                           help="Wordt ingevuld uit de bestandsnaam van de scan.")
    if voor and kant_v != "v":
        st.warning("De bestandsnaam van de voorkant eindigt niet op v.")
    if achter and kant_a != "a":
        st.warning("De bestandsnaam van de achterkant eindigt niet op a.")
    if nr_v and nr_a and nr_v != nr_a:
        st.warning(f"De twee scans horen bij verschillende kaartnummers ({nr_v} en {nr_a}).")
    if nummer.strip() and K.kaart_bestaat(nummer.strip()):
        st.warning(f"Kaartnummer {nummer.strip()} bestaat al.")

    velden, themas, fout = kaarten_ui.velden_formulier("nk")

    if st.button("Kaart opslaan", type="primary", key=f"nk_opslaan_{n}"):
        if not nummer.strip():
            st.error("Vul een kaartnummer in.")
        elif not voor or not achter:
            st.error("Een kaart heeft altijd twee scans: voorkant én achterkant.")
        elif fout:
            st.error(fout)
        else:
            try:
                K.maak_kaart(nummer.strip(), velden, themas, K.verklein_scan(voor.getvalue()),
                             K.verklein_scan(achter.getvalue()), current_user_short_name())
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Opslaan is niet gelukt: {exc}")
            else:
                st.session_state["nk_gelukt"] = f"Kaart {nummer.strip()} is opgeslagen."
                st.session_state["nk_teller"] = n + 1   # nieuwe lege scans en kaartnummer
                for veld in ("titel", "jaar", "opmerking"):   # deze leeg; provincie, plaats, thema's, uitgever enz. blijven staan voor de volgende kaart
                    st.session_state.pop(f"nk_{veld}", None)
                st.rerun()

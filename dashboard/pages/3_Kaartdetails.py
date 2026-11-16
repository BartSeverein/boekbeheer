"""
pages/3_Kaartdetails.py — Kaarten zoeken, bekijken en aanpassen.

De bestandsnaam staat bewust achter "3_Boekdetails": zo staat de pagina in het menu direct onder Boekdetails.
"""

from pathlib import Path

import streamlit as st

import kaarten_db as K
import kaarten_ui
from common import render_logo, require_login

st.set_page_config(page_title="Kaartdetails", page_icon="🖼️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🖼️ Kaartdetails")
K.toon_tabellen_melding()

totaal = K.aantal_kaarten()
st.caption(f"{totaal['n']} kaarten in het systeem, samen {totaal['voorraad']} op voorraad.")

col_t, col_p, col_th, col_v = st.columns([2, 1.3, 1.3, 1])
tekst = col_t.text_input("Zoek op kaartnummer, titel of plaats", key="kd_zoek")
provincie = col_p.selectbox("Provincie", [""] + K.get_provincies(), key="kd_prov", format_func=lambda p: p or "(alle)")
thema = col_th.selectbox("Thema", [""] + K.get_themas(), key="kd_thema", format_func=lambda t: t or "(alle)")
alleen_voorraad = col_v.checkbox("Alleen op voorraad", value=False, key="kd_voorraad")

rijen = K.zoek_kaarten(tekst, provincie or None, None, thema or None, alleen_voorraad)
if not rijen:
    st.info("Geen kaarten gevonden.")
    st.stop()

tabel = [{"Kaartnummer": r["kaartnummer"], "Titel": r["titel"], "Provincie": r["provincie"], "Plaats": r["plaats"],
          "Prijs": float(r["prijs"]) if r["prijs"] is not None else None, "Voorraad": r["amount"]}
         for r in rijen]
st.caption(f"{len(rijen)} kaarten getoond (de nieuwste eerst, hoogstens 200). Klik op een rij om de kaart te openen.")
keuze = st.dataframe(tabel, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row", key="kd_tabel")

gekozen = None
if keuze and keuze.selection.rows:
    gekozen = rijen[keuze.selection.rows[0]]["kaartnummer"]
if not gekozen and len(rijen) == 1:
    gekozen = rijen[0]["kaartnummer"]
if not gekozen:
    st.stop()

kaart = K.get_kaart(gekozen)
if not kaart:
    st.error("Deze kaart bestaat niet meer.")
    st.stop()

st.divider()
st.subheader(f"Kaart {gekozen}")
foto_v, foto_a = st.columns(2)
for kolom, naam, data in ((foto_v, "Voorkant", kaart.get("voorkant")), (foto_a, "Achterkant", kaart.get("achterkant"))):
    with kolom:
        if data:
            st.image(data, caption=naam, width="stretch")
        else:
            st.info(f"{naam}: geen scan.")

with st.expander("Scans vervangen"):
    nieuw_v = st.file_uploader("Nieuwe voorkant", type=["jpg", "jpeg", "png"], key=f"kd_nv_{gekozen}")
    nieuw_a = st.file_uploader("Nieuwe achterkant", type=["jpg", "jpeg", "png"], key=f"kd_na_{gekozen}")

velden, themas, fout = kaarten_ui.velden_formulier(f"kd_{gekozen}", kaart)

if st.button("Wijzigingen opslaan", type="primary", key=f"kd_opslaan_{gekozen}"):
    if fout:
        st.error(fout)
    else:
        try:
            K.wijzig_kaart(gekozen, velden, themas,
                           K.verklein_scan(nieuw_v.getvalue()) if nieuw_v else None,
                           K.verklein_scan(nieuw_a.getvalue()) if nieuw_a else None)
        except Exception as exc:
            st.error(f"Opslaan is niet gelukt: {exc}")
        else:
            st.success("Opgeslagen.")
            st.rerun()

with st.expander("Kaart verwijderen"):
    zeker = st.checkbox("Ja, verwijder deze kaart en haar scans voorgoed", key=f"kd_zeker_{gekozen}")
    if st.button("Verwijderen", disabled=not zeker, key=f"kd_del_{gekozen}"):
        K.verwijder_kaart(gekozen)
        st.success(f"Kaart {gekozen} is verwijderd.")
        st.rerun()

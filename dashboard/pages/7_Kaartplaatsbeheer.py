"""
pages/7_Kaartplaatsbeheer.py — De plaatsnamen van de ansichtkaarten beheren (net als Auteursbeheer, maar voor plaatsen).

De bestandsnaam staat bewust achter "7_Auteursbeheer": zo staat de pagina in het menu direct onder Auteursbeheer.
Een plaats hoort bij één provincie. Dezelfde naam in twee provincies (Elst) zijn dus twee plaatsen.
"""

from pathlib import Path

import streamlit as st

import kaarten_db as K
from common import render_logo, require_login

st.set_page_config(page_title="Kaartplaatsbeheer", page_icon="🗺️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🗺️ Kaartplaatsbeheer")
K.toon_tabellen_melding()
st.caption(
    "De plaatsen waaruit je bij een kaart kiest, per provincie. Voeg een plaats toe, pas een schrijfwijze aan of voeg twee spellingen "
    "van dezelfde plaats samen. Bij het samenvoegen gaan alle kaarten mee en wordt de oude naam een afwijkende schrijfwijze."
)

melding = st.session_state.pop("kp_melding", None)
if melding:
    st.success(melding)

provincies = K.get_provincies()
provincie = st.selectbox("Provincie", provincies, key="kp_prov")
if not provincie:
    st.stop()

rijen = K.plaatsen_met_aantal(provincie)
zoek = st.text_input("Zoek een plaats", key="kp_zoek").strip().lower()
getoond = [r for r in rijen if not zoek or zoek in r["naam"].lower()]
met_kaarten = sum(1 for r in rijen if r["kaarten"])
st.markdown(f"**{len(rijen)}** plaatsen in {provincie}, waarvan **{met_kaarten}** met kaarten.")
st.dataframe([{"Plaats": r["naam"], "Kaarten": r["kaarten"], "Eiland": r["eiland"]} for r in getoond],
             width="stretch", hide_index=True, height=300)

namen = {r["id"]: r["naam"] for r in rijen}
ids = list(namen)


def klaar(tekst):
    st.session_state["kp_melding"] = tekst
    st.rerun()


with st.expander("Plaats toevoegen"):
    nieuwe = st.text_input("Naam van de nieuwe plaats", key="kp_nieuw")
    if st.button("Toevoegen", key="kp_toevoegen"):
        try:
            K.voeg_plaats_toe(provincie, nieuwe)
        except ValueError as exc:
            st.error(str(exc))
        else:
            klaar(f"{nieuwe.strip()} is toegevoegd aan {provincie}.")

with st.expander("Schrijfwijze aanpassen"):
    if ids:
        wie = st.selectbox("Plaats", ids, format_func=lambda i: namen[i], key="kp_hernoem_wie")
        nieuwe_naam = st.text_input("Nieuwe naam", value=namen[wie], key=f"kp_hernoem_naam_{wie}")
        if st.button("Hernoemen", key="kp_hernoemen"):
            try:
                K.hernoem_plaats(wie, nieuwe_naam)
            except ValueError as exc:
                st.error(str(exc))
            else:
                klaar(f"{namen[wie]} heet nu {nieuwe_naam.strip()}.")

with st.expander("Twee plaatsen samenvoegen"):
    if len(ids) > 1:
        van = st.selectbox("Deze plaats verdwijnt (verkeerde schrijfwijze)", ids, format_func=lambda i: namen[i], key="kp_van")
        naar = st.selectbox("Alles gaat naar deze plaats (juiste schrijfwijze)", ids, format_func=lambda i: namen[i], key="kp_naar")
        if st.button("Samenvoegen", key="kp_samenvoegen"):
            try:
                K.voeg_plaatsen_samen(van, naar)
            except ValueError as exc:
                st.error(str(exc))
            else:
                klaar(f"{namen[van]} is samengevoegd met {namen[naar]}.")

with st.expander("Plaats verwijderen"):
    leeg = [i for i in ids if not next(r["kaarten"] for r in rijen if r["id"] == i)]
    if leeg:
        weg = st.selectbox("Plaats zonder kaarten", leeg, format_func=lambda i: namen[i], key="kp_weg")
        if st.button("Verwijderen", key="kp_verwijder"):
            try:
                K.verwijder_plaats(weg)
            except ValueError as exc:
                st.error(str(exc))
            else:
                klaar(f"{namen[weg]} is verwijderd.")
    else:
        st.caption("Alle plaatsen in deze provincie hebben kaarten. Voeg eerst samen als je er een wilt laten verdwijnen.")

with st.expander("Provincie of gebied toevoegen (bijvoorbeeld Buitenland)"):
    nieuwe_prov = st.text_input("Naam", key="kp_nieuwe_prov")
    if st.button("Toevoegen", key="kp_prov_toevoegen"):
        try:
            K.voeg_provincie_toe(nieuwe_prov)
        except ValueError as exc:
            st.error(str(exc))
        else:
            klaar(f"{nieuwe_prov.strip()} is toegevoegd. Kies het hierboven en voeg er plaatsen aan toe.")

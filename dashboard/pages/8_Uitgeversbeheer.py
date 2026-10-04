"""
pages/8_Uitgeversbeheer.py — Bekende-uitgeverslijst beheren.

Rijen toevoegen, aanpassen of verwijderen, en dan op 'Opslaan' klikken om de
volledige lijst in Supabase bij te werken.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from common import get_known_publishers, replace_known_publishers, render_logo, require_login, get_setting, set_setting

st.set_page_config(page_title="Uitgeversbeheer", page_icon="🏭", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🏭 Uitgeversbeheer")

st.caption(
    "Pas hier de lijst met bekende uitgevers aan die als dropdown wordt gebruikt bij "
    "boeken bewerken en toevoegen. Voeg rijen toe met de '+' onderaan de tabel, "
    "verwijder een rij met het prullenbak-icoon, of pas een waarde direct aan. "
    "Klik daarna op 'Opslaan' om de wijzigingen door te voeren."
)

st.markdown("")

st.caption(
    "Gebruik altijd dit formaat: uitgeversnaam ; adres huisnummer - postcode - plaats - NL ; contactadres  \n"
    "De puntkomma's zijn cruciaal voor de juiste werking!"
)

known_publishers = get_known_publishers()
df = pd.DataFrame({"Uitgever": known_publishers})

edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    width="stretch",
    key="publishers_editor",
    column_config={
        "Uitgever": st.column_config.TextColumn("Uitgever", width="large"),
    },
)

col1, col2 = st.columns([1, 5])
with col1:
    if st.button("Opslaan", key="save_publishers"):
        new_values = edited_df["Uitgever"].dropna().astype(str).tolist()
        replace_known_publishers(new_values)
        get_known_publishers.clear()
        st.success(f"Opgeslagen — {len(set(v.strip() for v in new_values if v.strip()))} uitgevers in de lijst.")
        st.rerun()

with col2:
    st.caption(f"Huidig aantal in de lijst: {len(known_publishers)}")

st.divider()

st.subheader("Automatisch herkennen van uitgevers")
st.caption(
    "Als bij het invoeren van een nieuw boek via ISBN een uitgeversnaam wordt gevonden "
    "(bij Boekwinkeltjes of ISBNdb), vergelijkt de app die met de naam vóór de eerste "
    "puntkomma van elke uitgever hierboven. Komt die voldoende overeen, dan wordt de "
    "bestaande uitgever gebruikt in plaats van een nieuwe aan te maken."
)
current_threshold = int(get_setting("publisher_match_threshold", "90"))
new_threshold = st.slider(
    "Gevoeligheid van automatisch opzoeken van uitgever",
    min_value=0,
    max_value=100,
    value=current_threshold,
    help="0 = maakt niet uit (bijna alles wordt gezien als een match), 100 = moet perfect hetzelfde zijn",
)
if new_threshold != current_threshold:
    set_setting("publisher_match_threshold", new_threshold)
    st.success(f"Gevoeligheid ingesteld op {new_threshold}.")

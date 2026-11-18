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

st.header("📚 Uitgevers van boeken")

st.caption(
    "Gebruik voor boeken met een ISBN altijd dit formaat: uitgeversnaam ; adres huisnummer - postcode - plaats - NL ; contactadres  \n"
    "De puntkomma's zijn cruciaal voor de juiste werking en uitgevers die het niet hebben worden in de vroege ochtend automatisch verwijderd uit de keuzelijst. In de boekdetails blijven ze natuurlijk wel."
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
    if st.button("Opslaan", type="primary", key="save_publishers"):
        new_values = edited_df["Uitgever"].dropna().astype(str).tolist()
        replace_known_publishers(new_values)
        get_known_publishers.clear()
        st.success(f"Opgeslagen — {len(set(v.strip() for v in new_values if v.strip()))} uitgevers in de lijst.")
        st.rerun()

with col2:
    st.caption(f"Huidig aantal in de lijst: {len(known_publishers)}")

st.divider()

st.subheader("Automatisch herkennen van uitgevers van boeken")
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


# ---------- Uitgevers van ansichtkaarten ----------

import kaarten_db as K

st.divider()
st.header("📮 Uitgevers van ansichtkaarten")

if not K.tabellen_aanwezig():
    st.info("De tabellen voor de kaarten bestaan nog niet (kaarten_1_tabellen.sql en kaarten_3_uitgevers.sql in Supabase).")
else:
    st.caption(
        f"De uitgevers die je bij een kaart kunt kiezen: {K.aantal_uitgevers()} stuks, in de vorm naam en vestigingsplaats. "
        "Zoek eerst; je ziet hoogstens 200 uitgevers tegelijk. Pas een naam of plaats aan, voeg een regel toe met de '+', "
        "of verwijder een regel (alleen mogelijk zolang er geen kaarten aan hangen). Klik daarna op 'Opslaan'."
    )
    melding = st.session_state.pop("ku_melding", None)
    if melding:
        st.success(melding)
    zoek = st.text_input("Zoek een uitgever (naam of plaats)", key="ku_zoek")
    rijen = K.zoek_uitgevers(zoek)
    import pandas as pd
    basis = pd.DataFrame([{"id": r["id"], "Naam": r["naam"], "Plaats": r["plaats"] or "", "Kaarten": r["kaarten"]} for r in rijen],
                         columns=["id", "Naam", "Plaats", "Kaarten"])
    bewerkt = st.data_editor(
        basis, num_rows="dynamic", width="stretch", hide_index=True, key=f"ku_editor_{zoek}",
        column_config={"id": None, "Naam": st.column_config.TextColumn("Naam", width="large"),
                       "Plaats": st.column_config.TextColumn("Plaats", width="medium"),
                       "Kaarten": st.column_config.NumberColumn("Kaarten", disabled=True)},
    )
    if st.button("Opslaan", type="primary", key="ku_opslaan"):
        oud = {int(r["id"]): (r["Naam"], r["Plaats"] or "") for _, r in basis.iterrows()}
        fouten, gewijzigd, nieuw, weg = [], 0, 0, 0
        nu_ids = set()
        for _, r in bewerkt.iterrows():
            naam, plaats = (r["Naam"] or ""), (r["Plaats"] or "")
            if pd.isna(r["id"]):
                if not str(naam).strip():
                    continue
                try:
                    K.voeg_uitgever_toe(naam, plaats); nieuw += 1
                except ValueError as exc:
                    fouten.append(str(exc))
            else:
                uid = int(r["id"]); nu_ids.add(uid)
                if oud.get(uid) != (naam, plaats):
                    try:
                        K.wijzig_uitgever(uid, naam, plaats); gewijzigd += 1
                    except ValueError as exc:
                        fouten.append(str(exc))
        for uid in set(oud) - nu_ids:
            try:
                K.verwijder_uitgever(uid); weg += 1
            except ValueError as exc:
                fouten.append(f"{oud[uid][0]}: {exc}")
        for f in fouten:
            st.error(f)
        if not fouten:
            st.session_state["ku_melding"] = f"Opgeslagen: {nieuw} nieuw, {gewijzigd} aangepast, {weg} verwijderd."
            st.rerun()

"""
pages/11_Hulp_en_instellingen.py — Architectuurschema, handleiding-download,
supportgegevens en de instellingen voor het druppelsysteem.
"""

import base64
import datetime as dt
from pathlib import Path

import streamlit as st

from common import (
    render_logo,
    require_login,
    get_setting,
    set_setting,
)

st.set_page_config(page_title="Hulp en instellingen", page_icon="ℹ️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("ℹ️ Hulp en instellingen")

# ---------- Info & help ----------

st.subheader("Architectuur")
architecture_path = Path(__file__).parent.parent / "assets" / "Architectuur.png"
if architecture_path.exists():
    st.image(str(architecture_path), use_container_width=True)
else:
    st.info("Architectuur.png is nog niet gevonden in de assets-map.")

st.divider()

manual_path = Path(__file__).parent.parent / "assets" / "boekbeheer.pdf"
if manual_path.exists():
    manual_bytes = manual_path.read_bytes()
    size_kb = round(len(manual_bytes) / 1024)
    manual_b64 = base64.b64encode(manual_bytes).decode("ascii")
    st.markdown(
        f'Download handleiding: <a href="data:application/pdf;base64,{manual_b64}" '
        f'download="boekbeheer.pdf">klik hier</a> (PDF, {size_kb} Kb)',
        unsafe_allow_html=True,
    )
else:
    st.info("boekbeheer.pdf is nog niet gevonden in de assets-map.")

st.markdown(
    "**Support:** Bart, 06-20539792 of [klik hier voor Whatsapp](https://wa.me/0031620539792)"
)

st.divider()
st.markdown("Het Boekbeheersysteem is © 2026, Severein Services")

# ---------- Druppelsysteem Boekwinkeltjes ----------

st.divider()
st.header("Druppelsysteem Boekwinkeltjes")
st.caption(
    "Boeken worden druppelsgewijs aan Boekwinkeltjes toegevoegd, voor optimale "
    "zichtbaarheid en verkoopkansen. Dat gaat in drie drukke bezoekperioden: de "
    "lunchpauze, einde werktijd en de avond. Kies hieronder om de hoeveel minuten "
    "een boek moet worden toegevoegd en wat de perioden zijn."
)

DRIP_DEFAULTS = {
    "bw_drip_interval_minutes": "8",
    "bw_drip_lunch_start": "11:00",
    "bw_drip_lunch_end": "14:00",
    "bw_drip_endwork_start": "16:15",
    "bw_drip_endwork_end": "16:45",
    "bw_drip_evening_start": "19:00",
    "bw_drip_evening_end": "22:00",
}


def _time_from_setting(key):
    value = get_setting(key, DRIP_DEFAULTS[key])
    hours, minutes = value.split(":")
    return dt.time(int(hours), int(minutes))


drip_col1, drip_col2 = st.columns(2)
with drip_col1:
    st.markdown("**🥪 Lunchpauze**")
    lunch_start = st.time_input("Van", value=_time_from_setting("bw_drip_lunch_start"), key="drip_lunch_start")
    lunch_end = st.time_input("Tot", value=_time_from_setting("bw_drip_lunch_end"), key="drip_lunch_end")

    st.markdown("**🌜 Avond**")
    evening_start = st.time_input("Van", value=_time_from_setting("bw_drip_evening_start"), key="drip_evening_start")
    evening_end = st.time_input("Tot", value=_time_from_setting("bw_drip_evening_end"), key="drip_evening_end")

with drip_col2:
    st.markdown("**📉 Einde werktijd**")
    endwork_start = st.time_input("Van", value=_time_from_setting("bw_drip_endwork_start"), key="drip_endwork_start")
    endwork_end = st.time_input("Tot", value=_time_from_setting("bw_drip_endwork_end"), key="drip_endwork_end")

    st.markdown("**⏳ Interval**")
    interval_minutes = st.slider(
        "Om de hoeveel minuten een boek wordt toegevoegd",
        min_value=1,
        max_value=20,
        value=int(get_setting("bw_drip_interval_minutes", DRIP_DEFAULTS["bw_drip_interval_minutes"])),
        key="drip_interval",
    )

if st.button("Opslaan", key="save_drip_settings"):
    set_setting("bw_drip_lunch_start", lunch_start.strftime("%H:%M"))
    set_setting("bw_drip_lunch_end", lunch_end.strftime("%H:%M"))
    set_setting("bw_drip_endwork_start", endwork_start.strftime("%H:%M"))
    set_setting("bw_drip_endwork_end", endwork_end.strftime("%H:%M"))
    set_setting("bw_drip_evening_start", evening_start.strftime("%H:%M"))
    set_setting("bw_drip_evening_end", evening_end.strftime("%H:%M"))
    set_setting("bw_drip_interval_minutes", str(interval_minutes))
    st.success("Opgeslagen.")

st.caption(
    "Geldt alleen voor Boekwinkeltjes. De synchronisatie met Bol kent dit "
    "'nieuw toegevoegd'-mechanisme niet en blijft altijd direct doorgaan. Staan er "
    "aan het einde van de avondperiode nog boeken te wachten, dan worden die in "
    "één keer alsnog aangemaakt."
)

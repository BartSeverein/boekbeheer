"""
pages/7_Auteursbeheer.py — Korte tekst per auteur voor de auteurspagina's van de webshop.

Een auteur krijgt in de webshop een eigen pagina zodra er minstens 3 boeken van hem of haar te koop staan. Hier kun je per
auteur een paar zinnen over de schrijver invullen; die komen op de pagina vóór de zin "Er zijn 3 boeken van … in mijn webshop."
De tekst gaat mee met de eerstvolgende volledige webshop-run en wordt daar ook in het Engels, Duits en Frans gezet.
De teksten blijven bewaard, ook als een auteur door verkoop onder de 3 boeken komt en zijn pagina tijdelijk verdwijnt.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from common import render_logo, require_login, get_webshop_authors, get_author_texts, save_author_text

st.set_page_config(page_title="Auteursbeheer", page_icon="✍️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("✍️ Auteursbeheer")
st.caption(
    "Schrijf een paar zinnen over een auteur. Die komen op de auteurspagina in de webshop, vóór de zin "
    "\"Er zijn 3 boeken van … in mijn webshop.\" Schrijf in het Nederlands; de webshop vertaalt de tekst zelf. "
    "Een auteur zonder tekst ziet er hetzelfde uit als nu. Wijzigingen staan na de eerstvolgende volledige webshop-run online."
)

texts = get_author_texts()
if texts is None:
    st.error("De tabel voor auteursteksten bestaat nog niet. Voer auteursteksten.sql uit in Supabase.")
    st.stop()

try:
    authors = get_webshop_authors()
except Exception as exc:
    st.error(f"De lijst met auteurs kon niet worden opgebouwd: {exc}")
    st.stop()

with_text = {a["slug"] for a in authors if (texts.get(a["slug"], {}).get("text") or "").strip()}
st.markdown(
    f"**{len(authors)}** auteurs hebben nu een pagina (3 of meer boeken), waarvan **{len(with_text)}** met een tekst "
    f"en **{len(authors) - len(with_text)}** zonder."
)

only_missing = st.checkbox("Alleen auteurs zonder tekst tonen", value=True)
search = st.text_input("Zoek een auteur", placeholder="bijvoorbeeld Manara").strip().lower()
shown = [
    a for a in authors
    if (not only_missing or a["slug"] not in with_text) and (not search or search in a["name"].lower())
]

if not shown:
    st.info("Geen auteurs gevonden met deze keuze.")
else:
    def _label(a):
        mark = "✔ " if a["slug"] in with_text else ""
        return f"{mark}{a['name']} ({a['count']} boeken)"

    choice = st.selectbox("Auteur", shown, format_func=_label, key="author_choice")
    slug, name = choice["slug"], choice["name"]
    current = texts.get(slug, {}).get("text", "")
    new_text = st.text_area(
        f"Tekst over {name}",
        value=current,
        height=160,
        key=f"author_text_{slug}",
        help="Twee of drie zinnen is genoeg. Regeleindes worden spaties: het blijft één alinea.",
    )
    st.caption(f"Staat op: /auteur/{slug}/")
    if new_text.strip():
        st.markdown(f"**Zo komt het op de pagina:** {' '.join(new_text.split())} Er zijn {choice['count']} boeken van {name} in mijn webshop.")
    if st.button("Opslaan", type="primary", key=f"author_save_{slug}"):
        save_author_text(slug, name, new_text)
        st.success("Opgeslagen. Staat online na de eerstvolgende volledige webshop-run.")
        st.rerun()

# Bewaarde teksten van auteurs die nu geen pagina hebben
current_slugs = {a["slug"] for a in authors}
parked = {s: v for s, v in texts.items() if s not in current_slugs and (v.get("text") or "").strip()}
if parked:
    st.divider()
    st.subheader("Bewaarde teksten zonder pagina")
    st.caption(
        "Van deze auteurs staan nu minder dan 3 boeken in de webshop, dus er is geen pagina. De tekst blijft bewaard en komt "
        "terug zodra er weer 3 boeken te koop staan."
    )
    st.dataframe(
        pd.DataFrame([{"Auteur": v["name"] or s, "Tekst": v["text"]} for s, v in sorted(parked.items(), key=lambda kv: (kv[1]["name"] or kv[0]).lower())]),
        hide_index=True, use_container_width=True,
    )

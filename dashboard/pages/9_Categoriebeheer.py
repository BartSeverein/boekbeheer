"""
pages/9_Categoriebeheer.py — Koppeling tussen Boekwinkeltjes-categorieën en
ISBNdb-subjects beheren. Deze koppeling wordt gebruikt op 'Nieuw boek' om,
op basis van de subjects die ISBNdb bij een ISBN teruggeeft, automatisch een
passende Categorie 1 (en eventueel Categorie 2) voor te stellen.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from common import (
    render_logo,
    require_login,
    search_isbndb_subjects,
    get_category_subject_mappings,
    save_category_subject_mapping,
    get_setting,
    set_setting,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS, english_translation

st.set_page_config(page_title="Categoriebeheer", page_icon="🏷️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🏷️ Categoriebeheer")

st.caption(
    "Dit overzicht koppelt elke Boekwinkeltjes-categorie aan een lijst ISBNdb-subjects "
    "die daarbij passen. Op 'Nieuw boek' gebruikt de app deze koppeling om, op basis van "
    "de subjects die ISBNdb bij een ingevuld ISBN teruggeeft, automatisch een passende "
    "Categorie 1 (en bij Hobby/Topografie/Studieboeken ook Categorie 2) voor te stellen. "
    "Alleen de kolom 'ISBNdb subjects' is hier aan te passen — de Boekwinkeltjes-kant "
    "ligt vast (dat zijn precies de dropdown-waarden die de app al gebruikt)."
)


def _build_category_rows():
    rows = []
    for value, label in CATEGORY1_OPTIONS:
        rows.append({"category1": value, "category2": "", "label": label})
    for cat1_value, cat1_label in CATEGORY1_OPTIONS:
        if cat1_value not in CATEGORY2_OPTIONS:
            continue
        for value, label in CATEGORY2_OPTIONS[cat1_value]:
            if value == "":
                continue  # 'Maak uw keuze'-placeholder, niet zinvol om subjects aan te koppelen
            rows.append({"category1": cat1_value, "category2": value, "label": f"{cat1_label} > {label}"})
    return rows


rows = _build_category_rows()
existing = {(m["boekwinkeltjes_category1"], m["boekwinkeltjes_category2"]): m["subjects"] or "" for m in get_category_subject_mappings()}

for row in rows:
    stored = existing.get((row["category1"], row["category2"]), "")
    row["subjects_display"] = ", ".join(s.strip() for s in stored.split("|") if s.strip())

st.divider()

with st.expander("⚙️ Automatisch aanvullen via ISBNdb"):
    st.warning(
        f"Dit zoekt zowel op de Nederlandse als de Engelse naam van elke rij hierboven "
        f"(ISBNdb-subjects zijn doorgaans Engelstalig) — dat zijn 2 opzoekingen per rij, "
        f"dus {len(rows) * 2} in totaal, wat een flink deel van je dagquotum kan opeisen. "
        "Bestaande, handmatig aangepaste subjects voor een rij worden overschreven."
    )
    if st.button("Start automatisch aanvullen"):
        progress = st.progress(0, text="Bezig...")
        for i, row in enumerate(rows):
            dutch_label = row["label"].split(" > ")[-1]
            english_label = english_translation(dutch_label)
            found = search_isbndb_subjects(dutch_label)
            if english_label.lower() != dutch_label.lower():
                found_en = search_isbndb_subjects(english_label)
                found = list(dict.fromkeys(found + found_en))  # samenvoegen, dubbelen eruit
            save_category_subject_mapping(row["category1"], row["category2"], "|".join(found))
            progress.progress((i + 1) / len(rows), text=f"{i + 1}/{len(rows)}: {row['label']}")
        progress.empty()
        st.success("Klaar! De tabel hieronder toont de nieuwe resultaten.")
        st.rerun()

st.divider()

df = pd.DataFrame(rows)[["label", "subjects_display"]].rename(
    columns={"label": "Boekwinkeltjes categorie", "subjects_display": "ISBNdb subjects"}
)

edited_df = st.data_editor(
    df,
    use_container_width=True,
    num_rows="fixed",
    key="category_mapping_editor",
    column_config={
        "Boekwinkeltjes categorie": st.column_config.TextColumn("Boekwinkeltjes categorie", disabled=True),
        "ISBNdb subjects": st.column_config.TextColumn(
            "ISBNdb subjects", help="Kommagescheiden lijst van ISBNdb-subjectnamen"
        ),
    },
)

if st.button("Opslaan"):
    for row, (_, edited_row) in zip(rows, edited_df.iterrows()):
        subjects_list = [s.strip() for s in str(edited_row["ISBNdb subjects"]).split(",") if s.strip()]
        save_category_subject_mapping(row["category1"], row["category2"], "|".join(subjects_list))
    st.success("Opgeslagen.")
    st.rerun()

st.divider()

st.subheader("Automatisch herkennen van categorieën")
st.caption(
    "Als bij het invoeren van een nieuw boek via ISBN een categorie wordt gevonden bij "
    "Bol, vergelijkt de app die met de categorieën van Boekwinkeltjes. Komt die "
    "voldoende overeen, dan wordt die categorie gebruikt."
)
current_cat_threshold = int(get_setting("category_match_threshold", "90"))
new_cat_threshold = st.slider(
    "Gevoeligheid van automatisch opzoeken van categorie",
    min_value=0,
    max_value=100,
    value=current_cat_threshold,
    help="0 = maakt niet uit (bijna alles wordt gezien als een match), 100 = moet perfect hetzelfde zijn",
)
if new_cat_threshold != current_cat_threshold:
    set_setting("category_match_threshold", new_cat_threshold)
    st.success(f"Gevoeligheid ingesteld op {new_cat_threshold}.")

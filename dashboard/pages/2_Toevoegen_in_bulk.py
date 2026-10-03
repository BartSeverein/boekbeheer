"""
pages/2_Toevoegen_in_bulk.py — In één keer meerdere boeken toevoegen op basis van
een tekstbestand met één ISBN per regel. Elk boek wordt automatisch aangevuld
(uitgever, categorie, titel, auteur, beschrijving, enz. — dezelfde bronnen als
'Nieuw boek'), maar komt standaard in de wachtrij te staan (queued = TRUE) met
synchronisatie uit, zodat er niets naar Boekwinkeltjes gaat vóór iemand het
handmatig heeft gecontroleerd.
"""

from pathlib import Path

import streamlit as st

from common import (
    render_logo,
    require_login,
    load_books,
    current_user_short_name,
    get_user_last_location,
    autofill_book_fields_from_isbn,
    create_new_book_draft,
    get_queued_books,
    info_box,
    delete_book,
    save_uploaded_images,
    normalize_isbn,
    find_existing_book_by_isbn,
    save_book_edits,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS

st.set_page_config(page_title="Toevoegen in bulk", page_icon="📥", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("📥 Toevoegen in bulk")

st.caption(
    "Upload een tekstbestand met op elke regel één ISBN. Voor elk ISBN wordt een "
    "nieuw boek aangemaakt met o.a. automatisch ingevulde titel, auteur, uitgever, "
    "categorie, afbeelding(en). Pas als je een boek hieronder hebt gecontroleerd en "
    "in het boek de wachtrij op 'Nee' zet, gaat het mee met "
    "[de eerstvolgende synchronisatie](https://boekbeheer.streamlit.app/Hulp_en_instellingen#druppelsysteem-boekwinkeltjes) "
    "naar Boekwinkeltjes."
)

st.warning(
    "Dit doet meerdere opzoekingen per ISBN (Boekwinkeltjes, Bol, ISBNdb, Google "
    "Books/Open Library), bij veel ISBN's tegelijk kan dit een flink deel van je "
    "ISBNdb-dagquotum opeisen en enige tijd duren."
)

bulk_duplicates = st.session_state.pop("bulk_import_duplicates", None)
if bulk_duplicates:
    st.warning(f"{len(bulk_duplicates)} ISBN('s) uit de laatste import stonden al in je database:")
    for isbn, existing in bulk_duplicates:
        dup_col_info, dup_col_increase, dup_col_button = st.columns([4, 1.3, 1])
        with dup_col_info:
            location_text = f", op {existing['location']}" if existing.get("location") else ""
            st.write(f"ISBN {isbn} — **{existing['title'] or '(geen titel)'}** (voorraad: {existing['amount']}{location_text})")
        with dup_col_increase:
            if st.button("Verhoog voorraad met 1", key=f"bulk_dup_increase_{existing['id']}"):
                save_book_edits(
                    existing["id"],
                    {"amount": (existing["amount"] or 0) + 1},
                    user_short_name=current_user_short_name(),
                    previous_values={"amount": existing["amount"]},
                )
                load_books.clear()
                st.success(f"Voorraad verhoogd naar {(existing['amount'] or 0) + 1}.")
        with dup_col_button:
            st.link_button("Naar boek", f"Boekdetails?book_id={existing['id']}")

default_location_prefill = get_user_last_location(current_user_short_name()) or ""
loc_col, cat1_col, cat2_col = st.columns(3)
with loc_col:
    import_location = st.text_input(
        "Locatie voor deze import",
        value=default_location_prefill,
        key="bulk_import_location",
    )
with cat1_col:
    bulk_cat1_labels = ["(automatisch bepalen per boek)"] + [label for _, label in CATEGORY1_OPTIONS]
    bulk_cat1_values = [""] + [value for value, _ in CATEGORY1_OPTIONS]
    chosen_bulk_cat1_label = st.selectbox(
        "Standaardcategorie 1 voor deze import (optioneel)", bulk_cat1_labels, key="bulk_import_category1"
    )
    import_category1 = bulk_cat1_values[bulk_cat1_labels.index(chosen_bulk_cat1_label)]
with cat2_col:
    if import_category1 in CATEGORY2_OPTIONS:
        cat2_options = CATEGORY2_OPTIONS[import_category1]
        bulk_cat2_labels = [label for _, label in cat2_options]
        bulk_cat2_values = [value for value, _ in cat2_options]
        chosen_bulk_cat2_label = st.selectbox(
            "Standaardcategorie 2 voor deze import",
            bulk_cat2_labels,
            key=f"bulk_import_category2_{import_category1}",
        )
        import_category2 = bulk_cat2_values[bulk_cat2_labels.index(chosen_bulk_cat2_label)]
    else:
        import_category2 = ""

if import_category1:
    st.caption(
        "Deze categorie wordt alleen gebruikt als de automatische opzoeking voor een "
        "boek zelf geen categorie oplevert — vindt Boekwinkeltjes/ISBNdb wél een "
        "categorie, dan blijft die leidend."
    )

st.markdown("**Tekstbestand met één ISBN per regel, mag ISBN-10 of ISBN-13 zijn**")
uploaded_file = st.file_uploader("Tekstbestand", type=["txt"], label_visibility="collapsed")

if uploaded_file is not None:
    raw_lines = uploaded_file.getvalue().decode("utf-8", errors="ignore").splitlines()
    isbns = [normalize_isbn(line.strip()) for line in raw_lines if line.strip()]
    st.caption(f"{len(isbns)} ISBN('s) gevonden in het bestand.")

    if isbns and st.button("Start bulk-import", key="start_bulk_import"):
        progress = st.progress(0, text="Bezig...")
        created = 0
        failed = []
        duplicates = []
        for i, isbn in enumerate(isbns):
            try:
                existing = find_existing_book_by_isbn(isbn)
                if existing:
                    duplicates.append((isbn, existing))
                    progress.progress((i + 1) / len(isbns), text=f"{i + 1}/{len(isbns)}: {isbn} (al aanwezig)")
                    continue

                fields = autofill_book_fields_from_isbn(isbn)
                cover_bytes = fields.pop("_cover_bytes", None)
                cover_content_type = fields.pop("_cover_content_type", None)
                fields.setdefault("amount", 1)
                fields.setdefault("price", 0.0)
                fields.setdefault("shipping_cost", 3.75)
                fields["location"] = import_location
                if import_category1 and not fields.get("category1"):
                    fields["category1"] = import_category1
                    fields["category2"] = import_category2
                fields["queued"] = True
                fields["push_enabled"] = False
                temp_id = create_new_book_draft(fields, user_short_name=current_user_short_name())
                if cover_bytes:
                    save_uploaded_images(
                        temp_id, [{"data": cover_bytes, "content_type": cover_content_type, "is_main": True}]
                    )
                created += 1
            except Exception as e:
                failed.append((isbn, str(e)))
            progress.progress((i + 1) / len(isbns), text=f"{i + 1}/{len(isbns)}: {isbn}")
        progress.empty()
        load_books.clear()
        st.success(f"{created} boek(en) toegevoegd aan de wachtrij.")
        if duplicates:
            st.session_state["bulk_import_duplicates"] = duplicates
        if failed:
            st.error(
                "Niet gelukt voor: "
                + ", ".join(f"{isbn} ({err})" for isbn, err in failed)
            )
        st.rerun()

st.divider()
st.subheader("📋 Boeken die wachten op controle")

queued_books = get_queued_books()
if not queued_books:
    info_box("Geen boeken in de wachtrij.")
else:
    woord = "boek" if len(queued_books) == 1 else "boeken"
    werkwoord = "wacht" if len(queued_books) == 1 else "wachten"
    st.caption(f"{len(queued_books)} {woord} {werkwoord} op handmatige controle.")
    for book in queued_books:
        col_info, col_edit, col_delete = st.columns([5, 1, 1])
        with col_info:
            st.write(f"**{book['title'] or '(geen titel)'}** — {book['author'] or '–'} — ISBN {book['ean'] or '–'}")
        with col_edit:
            if st.button("Bewerken", key=f"edit_queued_{book['id']}"):
                st.session_state["preselect_book_id"] = book["id"]
                st.switch_page("pages/3_Boekdetails.py")
        with col_delete:
            if st.button("Verwijderen", key=f"delete_queued_{book['id']}"):
                delete_book(book["id"])
                load_books.clear()
                st.success(f"'{book['title'] or book['ean'] or book['id']}' verwijderd.")
                st.rerun()

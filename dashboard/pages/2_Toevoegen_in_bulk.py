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
from i18n import category_option, t

from common import (
    render_logo,
    require_login,
    load_books,
    current_user_short_name,
    get_user_last_location,
    autofill_book_fields_from_isbn,
    create_new_book_draft,
    get_queued_books,
    get_books_awaiting_admin,
    queued_counts,
    filter_queued_books,
    queued_caption,
    queued_row_text,
    QUEUE_VIEW_MINE,
    QUEUE_VIEW_ALL,
    info_box,
    delete_book,
    save_uploaded_images,
    normalize_isbn,
    find_existing_book_by_isbn,
    save_book_edits,
    get_shipping_costs,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS

st.set_page_config(page_title="Toevoegen in bulk", page_icon="📥", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
_current_user = require_login()
st.title(t('📥 Toevoegen in bulk'))

st.caption(
    t("Upload een tekstbestand met ISBN's. Pas als je een boek hieronder hebt gecontroleerd en in het boek de wachtrij op 'Nee' blijft, gaat het mee met [de eerstvolgende synchronisatie](https://boekbeheer.streamlit.app/Hulp_en_instellingen#druppelsysteem-boekwinkeltjes) naar de webshop, Bol en Boekwinkeltjes. Zie [de handleiding](https://boekbeheer.streamlit.app/Hulp_en_instellingen) voor meer info.")
)

st.warning(
    t("Dit doet meerdere opzoekingen per ISBN (Boekwinkeltjes, Bol, ISBNdb, Google Books/Open Library), bij veel ISBN's tegelijk kan dit een flink deel van je ISBNdb-dagquotum opeisen en enige tijd duren.")
)

bulk_duplicates = st.session_state.get("bulk_import_duplicates", None)
if bulk_duplicates:
    st.warning(t("{0} ISBN('s) uit de laatste import stonden al in je database:", len(bulk_duplicates)))
    for isbn, existing in bulk_duplicates:
        dup_col_info, dup_col_increase, dup_col_button = st.columns([4, 1.3, 1])
        with dup_col_info:
            location_text = t(", op {0}", existing['location']) if existing.get("location") else ""
            st.write(t("{0} — **{1}** (voorraad: {2}{3})", isbn, existing['title'] or t('(geen titel)'), existing['amount'], location_text))
        with dup_col_increase:
            if st.button(t('Voorraad +1'), key=f"bulk_dup_increase_{existing['id']}"):
                save_book_edits(
                    existing["id"],
                    {"amount": (existing["amount"] or 0) + 1},
                    user_short_name=current_user_short_name(),
                    previous_values={"amount": existing["amount"]},
                )
                load_books.clear()
                st.session_state["bulk_import_duplicates"] = [
                    pair for pair in bulk_duplicates if pair[1]["id"] != existing["id"]
                ]
                st.success(t('Voorraad verhoogd naar {0}.', (existing['amount'] or 0) + 1))
                st.rerun()
        with dup_col_button:
            st.link_button(t('Naar boek'), f"Boekdetails?book_id={existing['id']}")

default_location_prefill = get_user_last_location(current_user_short_name()) or ""
loc_col, cat1_col, cat2_col = st.columns(3)
with loc_col:
    import_location = st.text_input(
        t('Locatie voor deze import'),
        value=default_location_prefill,
        key="bulk_import_location",
    )
with cat1_col:
    bulk_cat1_labels = ["(automatisch bepalen per boek)"] + [label for _, label in CATEGORY1_OPTIONS]
    bulk_cat1_values = [""] + [value for value, _ in CATEGORY1_OPTIONS]
    chosen_bulk_cat1_label = st.selectbox(
        t('Standaardcategorie 1 voor deze import (optioneel)'), bulk_cat1_labels, key="bulk_import_category1", format_func=category_option
    )
    import_category1 = bulk_cat1_values[bulk_cat1_labels.index(chosen_bulk_cat1_label)]
with cat2_col:
    if import_category1 in CATEGORY2_OPTIONS:
        cat2_options = CATEGORY2_OPTIONS[import_category1]
        bulk_cat2_labels = [label for _, label in cat2_options]
        bulk_cat2_values = [value for value, _ in cat2_options]
        chosen_bulk_cat2_label = st.selectbox(
            t('Standaardcategorie 2 voor deze import'),
            bulk_cat2_labels,
            key=f"bulk_import_category2_{import_category1}",
            format_func=category_option,
        )
        import_category2 = bulk_cat2_values[bulk_cat2_labels.index(chosen_bulk_cat2_label)]
    else:
        import_category2 = ""

if import_category1:
    st.caption(
        t('Deze categorie wordt alleen gebruikt als de automatische opzoeking voor een boek zelf geen categorie oplevert — vindt Boekwinkeltjes/ISBNdb wél een categorie, dan blijft die leidend.')
    )

st.markdown(t('**Tekstbestand met één ISBN per regel, mag ISBN-10 of ISBN-13 zijn**'))
uploaded_file = st.file_uploader(t('Tekstbestand'), type=["txt"], label_visibility="collapsed")

if uploaded_file is not None:
    raw_lines = uploaded_file.getvalue().decode("utf-8", errors="ignore").splitlines()
    isbns = [normalize_isbn(line.strip()) for line in raw_lines if line.strip()]
    st.caption(t("{0} ISBN('s) gevonden in het bestand.", len(isbns)))

    if isbns and st.button(t('Start bulk-import'), key="start_bulk_import"):
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
                # Normaal bepaalt autofill dit al (briefpost/pakketpost); alleen als dat om wat
                # voor reden niet gelukt is: het hoogste van de twee ingestelde bedragen.
                fields.setdefault("shipping_cost", max(get_shipping_costs()))
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
        st.success(t('{0} boek(en) toegevoegd aan de wachtrij.', created))
        st.session_state["bulk_import_duplicates"] = duplicates
        if failed:
            st.error(
                t("Niet gelukt voor: {0}", ", ".join(f"{isbn} ({err})" for isbn, err in failed))
            )
        st.rerun()

st.divider()
st.subheader(t('📋 Boeken die wachten op controle'))

if "bulk_queue_loc_done" in st.session_state:
    st.success(st.session_state.pop("bulk_queue_loc_done"))
queued_books = get_queued_books()
if not queued_books:
    info_box(t("Geen boeken in de wachtrij."))
else:
    my_name = current_user_short_name()
    n_mine, n_total = queued_counts(queued_books, my_name)
    queue_view = st.radio(
        t('Toon'), [QUEUE_VIEW_MINE, QUEUE_VIEW_ALL], horizontal=True, format_func=t, key="bulk_queue_view", label_visibility="collapsed"
    )
    st.caption(queued_caption(n_mine, n_total))
    shown_books = filter_queued_books(queued_books, my_name, queue_view)
    if not shown_books:
        info_box(t("Je hebt geen boeken in de wachtrij staan. Kies '{0}' om de rest te zien.", t(QUEUE_VIEW_ALL)))
    if shown_books:
        # Eén locatie voor alle boeken die hieronder getoond worden (kies 'Alle boeken' voor de hele wachtrij).
        with st.expander(t('📍 Locatie wijzigen voor alle {0} getoonde boeken', len(shown_books))):
            new_location = st.text_input(t('Nieuwe locatie'), key="bulk_queue_new_location", placeholder=t('bijv. 0471AR')).strip()
            if st.button(t('Locatie van {0} boeken wijzigen', len(shown_books)), key="bulk_queue_set_location", disabled=not new_location):
                _me = current_user_short_name()
                for _b in shown_books:
                    save_book_edits(_b["id"], {"location": new_location}, user_short_name=_me,
                                    previous_values={"location": _b.get("location")})
                load_books.clear()
                st.session_state["bulk_queue_loc_done"] = t('De locatie van {0} boeken is gewijzigd naar {1}.', len(shown_books), new_location)
                st.rerun()
    for book in shown_books:
        col_info, col_edit, col_delete = st.columns([5, 1, 1])
        with col_info:
            st.write(queued_row_text(book))
        with col_edit:
            if st.button(t('Bewerken'), key=f"edit_queued_{book['id']}"):
                st.session_state["preselect_book_id"] = book["id"]
                st.switch_page("pages/3_Boekdetails.py")
        with col_delete:
            if st.button(t('Verwijderen'), key=f"delete_queued_{book['id']}"):
                delete_book(book["id"])
                load_books.clear()
                st.success(t("'{0}' verwijderd.", book['title'] or book['ean'] or book['id']))
                st.rerun()


# ---------- Alleen voor beheerders: boeken van anderen die wachten op goedkeuring ----------
if (_current_user is None) or (_current_user.get("role") == "beheerder"):
    st.divider()
    st.subheader(t('🛂 Wachten op goedkeuring door de beheerder'))
    st.caption(t("Boeken die een gebruiker heeft ingevoerd en gecontroleerd, maar die nog niet zijn vrijgegeven voor synchronisatie. Klik op 'Bewerken' om te bekijken, of op 'Vrijgeven' als je het zeker weet."))
    _awaiting = get_books_awaiting_admin()
    if not _awaiting:
        info_box(t("Geen boeken die op goedkeuring wachten."))
    else:
        st.write(t("{0} boek(en) wachten op goedkeuring.", len(_awaiting)))
        for _b in _awaiting:
            _ci, _cs, _cr, _ce = st.columns([6, 1.5, 1.3, 1])
            _ci.write(queued_row_text(_b))
            _cs.write(t('Nog niet vrijgegeven'))
            if _cr.button(t('✅ Vrijgeven'), key=f"release_awaiting_{_b['id']}"):
                _new, _old = {"push_enabled": True}, {"push_enabled": False}
                save_book_edits(_b["id"], _new, user_short_name=current_user_short_name(), previous_values=_old)
                load_books.clear()
                st.session_state["bulk_queue_loc_done"] = t("'{0}' vrijgegeven voor synchronisatie.", _b["title"] or _b["ean"] or _b["id"])
                st.rerun()
            if _ce.button(t('Bewerken'), key=f"edit_awaiting_{_b['id']}"):
                st.session_state["preselect_book_id"] = _b["id"]
                st.switch_page("pages/3_Boekdetails.py")

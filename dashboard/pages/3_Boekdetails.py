"""
pages/3_Boekdetails.py — Eén pagina om een boek in detail te bekijken en te bewerken, met bovenaan
een keuze tussen Voorradig (voorraad > 0), Niet voorradig (voorraad 0) en Alles.
In de weergave 'Niet voorradig' zijn locatie en verzendkosten niet zichtbaar en heet de prijs
'Laatste prijs Boekwinkeltjes'.
"""

import re
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import streamlit as st
from pathlib import Path

from common import (
    load_books,
    load_orders,
    get_all_images,
    format_price,
    format_datetime_nl,
    set_main_image_url,
    format_isbn,
    save_book_edits,
    get_known_publishers,
    add_known_publisher,
    render_logo,
    require_login,
    current_user_short_name,
    get_book_activity,
    na,
    save_uploaded_images,
    lookup_boekwinkeltjes_market_info,
    info_box,
    delete_book_image,
    lookup_bol_competing_offers,
    format_price_dot,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS


def _s(value):
    """Zet NaN/None om naar een lege string; anders de waarde zelf. Voorkomt de
    valkuil dat 'waarde or \"\"' een NaN laat staan, omdat NaN in Python 'waar' is."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value

st.set_page_config(page_title="Boekdetails", page_icon="📖", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("📖 Boekdetails")

books_all, orders = load_books(), load_orders()

# De vier weergaven: welke boeken je ziet, gebaseerd op voorraad en of het boek
# al bij Boekwinkeltjes is aangemaakt.
VIEW_OPTIONS = {
    "📗 Voorradig": "voorradig",
    "📒 Voorradig, nog niet op BW": "voorradig_nog_niet_bw",
    "📕 Niet voorradig": "niet_voorradig",
    "📘 Alles": "alles",
}
SEARCH_LABELS = {
    "voorradig": "🔎 Zoek op titel, auteur of ISBN in alle voorradige boeken.",
    "voorradig_nog_niet_bw": "🔎 Zoek op titel, auteur of ISBN in voorradige boeken die nog wachten op het druppelsysteem.",
    "niet_voorradig": "🔎 Zoek op titel, auteur of ISBN in alle boeken die ooit zijn ingevoerd, maar niet meer voorradig zijn.",
    "alles": "🔎 Zoek op titel, auteur of ISBN in álle boeken: voorradig en niet voorradig.",
}
EMPTY_MESSAGES = {
    "voorradig": "Nog geen voorradige boeken om te tonen.",
    "voorradig_nog_niet_bw": "Geen boeken die wachten op het druppelsysteem — alles staat al op Boekwinkeltjes.",
    "niet_voorradig": "Geen niet-voorradige boeken om te tonen.",
    "alles": "Nog geen boeken om te tonen.",
}

preselect_id = st.session_state.pop("preselect_book_id", None)
if preselect_id is not None:
    st.session_state["current_book_id"] = preselect_id
    # Kom je vanaf een andere pagina naar één specifiek boek, zet de weergave dan zo
    # dat dat boek er ook in zit.
    if not books_all.empty:
        preselected = books_all[books_all["id"] == preselect_id]
        if not preselected.empty:
            preselect_row = preselected.iloc[0]
            preselect_amount = preselect_row["amount"]
            has_stock = pd.notna(preselect_amount) and preselect_amount > 0
            if not has_stock:
                st.session_state["book_view_mode"] = "📕 Niet voorradig"
            elif preselect_row["id"] < 0:
                st.session_state["book_view_mode"] = "📒 Voorradig, nog niet op BW"
            else:
                st.session_state["book_view_mode"] = "📗 Voorradig"

# Na het opslaan schuift de weergave mee met het boek (bijv. naar 'Niet voorradig' als de
# voorraad op 0 is gezet). Dat moet vóór het aanmaken van de keuzeknop worden toegepast.
if "pending_view_mode" in st.session_state:
    st.session_state["book_view_mode"] = st.session_state.pop("pending_view_mode")

view_label = st.radio(
    "Toon", list(VIEW_OPTIONS.keys()), horizontal=True, key="book_view_mode", label_visibility="collapsed"
)
mode = VIEW_OPTIONS[view_label]
show_logistics = mode != "niet_voorradig"  # locatie en verzendkosten
price_label = "Laatste prijs Boekwinkeltjes" if mode == "niet_voorradig" else "Prijs Boekwinkeltjes"

if books_all.empty or mode == "alles":
    books = books_all
elif mode == "voorradig":
    books = books_all[(books_all["amount"].fillna(0) > 0) & (books_all["id"] > 0)]
elif mode == "voorradig_nog_niet_bw":
    books = books_all[(books_all["amount"].fillna(0) > 0) & (books_all["id"] < 0)]
else:
    books = books_all[books_all["amount"].fillna(0) == 0]

if books.empty:
    st.info(EMPTY_MESSAGES[mode])
else:
    default_search = ""
    if preselect_id is not None:
        match = books[books["id"] == preselect_id]
        if not match.empty:
            default_search = match.iloc[0]["title"]

    search = st.text_input(SEARCH_LABELS[mode], default_search, key="book_search_input")

    if search:
        s = search.strip().lower()
        mask = (
            books["title"].str.lower().str.contains(s, na=False)
            | books["author"].str.lower().str.contains(s, na=False)
            | books["book_number"].astype(str).str.contains(s, na=False)
            | books["ean"].astype(str).str.contains(s, na=False)
        )
        filtered = books[mask]

        if filtered.empty:
            st.warning("Geen boeken gevonden voor deze zoekterm.")
        else:
            st.caption(f"{len(filtered)} resultaat/resultaten")
            book_options = {
                f"{row['title']} — {row['author']} — ISBN {format_isbn(row['ean'])}": row["id"]
                for _, row in filtered.iterrows()
            }
            option_labels = list(book_options.keys())
            option_ids = list(book_options.values())
            current_id = st.session_state.get("current_book_id")
            default_index = option_ids.index(current_id) if current_id in option_ids else 0
            chosen_label = st.selectbox(
                "Kies een boek uit de zoekresultaten",
                option_labels,
                index=default_index,
                key="book_search_select",
            )
            st.session_state["current_book_id"] = book_options[chosen_label]
    else:
        if mode == "voorradig":
            st.caption(f"Typ om te zoeken in {len(books)} voorradige boeken.")
        elif mode == "voorradig_nog_niet_bw":
            st.caption(f"Typ om te zoeken in {len(books)} voorradige boeken die nog wachten op het druppelsysteem.")
        elif mode == "niet_voorradig":
            st.caption(f"Typ om te zoeken in {len(books)} niet-voorradige boeken.")
        else:
            st.caption(f"Typ om te zoeken in alle {len(books)} bekend zijnde boeken, voorradig en niet voorradig.")

    chosen_id = st.session_state.get("current_book_id")

    if chosen_id is not None and chosen_id in books["id"].values:
        b = books[books["id"] == chosen_id].iloc[0]

        st.markdown(f"## {b['title']}")

        book_queued = bool(b["queued"]) if pd.notna(b["queued"]) else False
        book_push_enabled = bool(b["push_enabled"]) if pd.notna(b["push_enabled"]) else True
        if not book_push_enabled and not book_queued and chosen_id > 0:
            st.warning(
                "⚠️ Synchronisatie met Boekwinkeltjes staat uit voor dit boek — vermoedelijk omdat het "
                "niet meer bij Boekwinkeltjes bestaat (bijv. daar handmatig verwijderd). Het boek wordt "
                "daardoor niet meer bijgewerkt."
            )
            if st.button("Synchronisatie weer aanzetten", key=f"reenable_push_{chosen_id}"):
                save_book_edits(
                    chosen_id,
                    {"push_enabled": True},
                    user_short_name=current_user_short_name(),
                    previous_values={"push_enabled": False},
                )
                load_books.clear()
                st.success("Synchronisatie weer aangezet. Wordt bij de eerstvolgende sync opnieuw geprobeerd.")
                st.rerun()

        images = get_all_images(chosen_id)
        main_image_url = b.get("main_image_url")

        default_idx = 0
        if main_image_url:
            for i, img in enumerate(images):
                if main_image_url in (img["url_large"], img["url_medium"], img["url_small"]):
                    default_idx = i
                    break

        idx_key = f"selected_image_idx_{chosen_id}"
        if idx_key not in st.session_state:
            st.session_state[idx_key] = default_idx
        current_idx = min(st.session_state[idx_key], len(images) - 1) if images else 0

        image_col, info_col1, info_col2 = st.columns([1, 1, 1])
        with image_col:
            if images:
                current_img = images[current_idx]
                big_url = current_img["url_large"] or current_img["url_medium"] or current_img["url_small"]
                st.markdown(
                    f'<img src="{big_url}" style="height:320px; width:auto; max-width:100%; object-fit:contain;" />',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("Geen afbeelding beschikbaar (of nog niet gesynchroniseerd).")
        with info_col1:
            st.markdown(f"**Auteur:** {na(b['author'])}")
            st.markdown(f"**Uitgever:** {na(b['publisher_name'])}")
            st.markdown(f"**Adres:** {na(b['publisher_address'])}")
            st.markdown(f"**Contact:** {na(b['publisher_contact'])}")
            st.markdown(f"**{price_label}:** €{format_price(b['price'])}")
            if show_logistics:
                st.markdown(f"**Verzendkosten Boekwinkeltjes:** €{format_price(b['shipping_cost'])}")
            st.markdown(f"**Voorraad:** {na(b['amount'])}")
        with info_col2:
            categories = [
                str(c).strip()
                for c in [b["category1"], b["category2"], b["category3"]]
                if pd.notna(c) and str(c).strip()
            ]
            st.markdown(f"**Categorie:** {' / '.join(categories) if categories else '–'}")
            st.markdown(f"**Taal:** {na(b['language'])}")
            st.markdown(f"**ISBN:** {format_isbn(b['ean'])}")
            if show_logistics:
                st.markdown(f"**Locatie:** {na(b['location'])}")

        st.markdown("<br>", unsafe_allow_html=True)

        if len(images) > 1:
            st.markdown(
                """
                <style>
                button[aria-label="🔍"], button[aria-label="⭐"], button[aria-label="🗑️"] {
                    padding: 0.25rem !important;
                    min-width: unset !important;
                    width: auto !important;
                    line-height: 1 !important;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
            st.markdown(
                "**Alle foto's** — klik op 🔍 voor een groter afbeelding, op ⭐ om hem aan te wijzen "
                "als hoofdafbeelding, en op 🗑️ om hem te verwijderen:"
            )
            thumbs_per_row = 5
            for row_start in range(0, len(images), thumbs_per_row):
                row_images = images[row_start : row_start + thumbs_per_row]
                thumb_cols = st.columns(thumbs_per_row)
                for offset, (col, img) in enumerate(zip(thumb_cols, row_images)):
                    i = row_start + offset
                    thumb_url = img["url_medium"] or img["url_large"] or img["url_small"]
                    with col:
                        st.markdown(
                            f'<img src="{thumb_url}" style="height:160px; width:auto; max-width:100%; object-fit:contain;" />',
                            unsafe_allow_html=True,
                        )
                        is_current_main = bool(main_image_url) and main_image_url in (
                            img["url_large"],
                            img["url_medium"],
                            img["url_small"],
                        )
                        b1, b2, b3 = st.columns([1, 1, 1])
                        with b1:
                            if st.button("🔍", key=f"thumb_btn_{chosen_id}_{i}"):
                                st.session_state[idx_key] = i
                                st.rerun()
                        with b2:
                            if is_current_main:
                                st.markdown(
                                    "<div style='text-align:center;'>⭐</div>", unsafe_allow_html=True
                                )
                            elif st.button("⭐", key=f"main_btn_{chosen_id}_{i}"):
                                new_main_url = img["url_large"] or img["url_medium"] or img["url_small"]
                                set_main_image_url(chosen_id, new_main_url)
                                st.session_state[idx_key] = i
                                get_all_images.clear()
                                load_books.clear()
                                st.rerun()
                        with b3:
                            if st.button("🗑️", key=f"delete_img_btn_{chosen_id}_{i}"):
                                delete_book_image(chosen_id, img["source"], img["ref_id"])
                                st.session_state[idx_key] = 0
                                get_all_images.clear()
                                st.rerun()
        if b.get("weblink"):
            st.markdown(f"**Link:** [{b['weblink']}]({b['weblink']})")

        st.markdown("**Korte beschrijving**")
        st.write(_s(b["short_description"]) or "—")
        st.markdown("**Lange beschrijving**")
        st.write(_s(b["long_description"]) or "—")

        with st.expander("✏️ Boek bewerken"):
            st.caption(
                "Pas aan wat je wilt en druk daarna op 'Opslaan' onderaan. Wijzigingen "
                "gaan naar Boekwinkeltjes bij de eerstvolgende sync (of klik op Home "
                "op '📚 Sync boeken nu')."
            )

            edit_prefix = f"edit_{chosen_id}"

            # ISBN, de Bol-prijs, en Categorie 1/2 staan buiten het formulier, omdat
            # ze direct moeten meebewegen met wat je hierboven kiest/typt (dat kan
            # een st.form niet, die update pas na op 'Opslaan' te klikken).
            isbn_col, bol_col = st.columns(2)
            with isbn_col:
                edit_ean = st.text_input("ISBN", value=_s(b["ean"]), key=f"{edit_prefix}_ean")
            isbn_present = len(re.sub(r"\D", "", edit_ean.strip())) >= 10
            with bol_col:
                if isbn_present:
                    current_bol_value = float(b["shipping_cost_bol"]) if pd.notna(b["shipping_cost_bol"]) else None
                    bol_unsuitable = st.checkbox(
                        "Boek is ongeschikt voor Bol (bijv. verboden type)",
                        value=False,
                        key=f"{edit_prefix}_bol_unsuitable",
                    )
                    if bol_unsuitable:
                        st.markdown("**Prijs Bol, incl. verzendkosten en commissie (€)**")
                        st.write("Boek ongeschikt voor Bol")
                        edit_shipping_cost_bol = None
                    else:
                        if current_bol_value is not None:
                            default_bol_value = current_bol_value
                        else:
                            base_price = float(b["price"]) if pd.notna(b["price"]) else 0.0
                            base_shipping = float(b["shipping_cost"]) if pd.notna(b["shipping_cost"]) else 0.0
                            default_bol_value = round(base_price + base_shipping + 2.25, 2)
                        edit_shipping_cost_bol = st.number_input(
                            "Prijs Bol, incl. verzendkosten en commissie (€)",
                            value=default_bol_value,
                            step=0.5,
                            key=f"{edit_prefix}_shipping_cost_bol",
                        )
                        if current_bol_value is None:
                            st.caption("(aanpasbare suggestie)")
                else:
                    st.markdown("**Prijs Bol, incl. verzendkosten en commissie (€)**")
                    st.write("Boek ongeschikt voor Bol")
                    edit_shipping_cost_bol = None

            if isbn_present:
                # Beide opzoekingen hebben niks met elkaar te maken, dus die halen we
                # gelijktijdig op in plaats van na elkaar.
                with ThreadPoolExecutor(max_workers=2) as executor:
                    future_market = executor.submit(lookup_boekwinkeltjes_market_info, edit_ean)
                    future_bol_offers = executor.submit(lookup_bol_competing_offers, edit_ean)
                    market_info, market_error = future_market.result()
                    bol_count, bol_laagste, bol_hoogste = future_bol_offers.result()
                bw_box_lines = []
                if market_info:
                    active = market_info.get("activeAmount") or 0
                    if active > 0:
                        msg = (
                            f"Dit boek wordt momenteel {active} keer aangeboden op Boekwinkeltjes "
                            f"voor €{format_price_dot(market_info.get('laagste_prijs'))} t/m €{format_price_dot(market_info.get('hoogste_prijs'))}"
                        )
                        if market_info.get("lastOrder"):
                            msg += (
                                f", en is voor het laatst verkocht op {market_info['lastOrder']} "
                                f"voor het bedrag van {market_info['lastPrice']}"
                            )
                        bw_box_lines.append(msg + ".")
                    elif market_info.get("lastOrder"):
                        bw_box_lines.append(
                            f"Dit boek wordt momenteel niet aangeboden, maar is voor het laatst verkocht "
                            f"op {market_info['lastOrder']} voor het bedrag van {market_info['lastPrice']}."
                        )
                    else:
                        bw_box_lines.append("Dit boek wordt niet aangeboden op Boekwinkeltjes.")

                if bol_count is not None:
                    if bol_count > 0:
                        verkoper_woord = "verkoper" if bol_count == 1 else "verkopers"
                        bw_box_lines.append(
                            f"Bij Bol wordt dit boek door {bol_count} {verkoper_woord} aangeboden voor "
                            f"€{format_price_dot(bol_laagste)} t/m €{format_price_dot(bol_hoogste)}."
                        )
                    else:
                        bw_box_lines.append("Bij Bol wordt dit boek momenteel niet aangeboden.")

                if bw_box_lines:
                    info_box("<br>".join(bw_box_lines))

            cat1_col, cat2_col = st.columns(2)
            with cat1_col:
                cat1_labels = [label for _, label in CATEGORY1_OPTIONS]
                cat1_values = [value for value, _ in CATEGORY1_OPTIONS]
                current_cat1_value = _s(b["category1"])
                if current_cat1_value not in cat1_values:
                    cat1_labels = [f"(huidig) {current_cat1_value}" if current_cat1_value else "(leeg)"] + cat1_labels
                    cat1_values = [current_cat1_value] + cat1_values
                cat1_index = cat1_values.index(current_cat1_value)
                chosen_cat1_label = st.selectbox(
                    "Categorie 1", cat1_labels, index=cat1_index, key=f"{edit_prefix}_category1"
                )
                edit_category1 = cat1_values[cat1_labels.index(chosen_cat1_label)]

            with cat2_col:
                if edit_category1 in CATEGORY2_OPTIONS:
                    cat2_options = CATEGORY2_OPTIONS[edit_category1]
                    cat2_labels = [label for _, label in cat2_options]
                    cat2_values = [value for value, _ in cat2_options]
                    current_cat2_value = _s(b["category2"]) if edit_category1 == current_cat1_value else ""
                    if current_cat2_value not in cat2_values:
                        cat2_labels = [f"(huidig) {current_cat2_value}"] + cat2_labels
                        cat2_values = [current_cat2_value] + cat2_values
                    cat2_index = cat2_values.index(current_cat2_value)
                    chosen_cat2_label = st.selectbox(
                        "Categorie 2",
                        cat2_labels,
                        index=cat2_index,
                        key=f"{edit_prefix}_category2_dd_{edit_category1}",
                    )
                    edit_category2 = cat2_values[cat2_labels.index(chosen_cat2_label)]
                else:
                    edit_category2 = st.text_input(
                        "Categorie 2", value=_s(b["category2"]), key=f"{edit_prefix}_category2_txt"
                    )

            with st.form(key=f"edit_book_form_{chosen_id}"):
                col_a, col_b = st.columns(2)
                with col_a:
                    edit_title = st.text_input("Titel", value=_s(b["title"]), key=f"{edit_prefix}_title")
                    edit_author = st.text_input("Auteur", value=_s(b["author"]), key=f"{edit_prefix}_author")

                    known_publishers = get_known_publishers()
                    current_publisher = _s(b["publisher"])
                    pub_options = list(known_publishers)
                    if current_publisher and current_publisher not in pub_options:
                        pub_options = [current_publisher] + pub_options
                    default_pub_index = pub_options.index(current_publisher) if current_publisher in pub_options else 0
                    chosen_publisher_option = st.selectbox(
                        "Uitgever (kies uit de lijst)",
                        pub_options,
                        index=default_pub_index,
                        key=f"{edit_prefix}_publisher_dd",
                    )
                    new_publisher_text = st.text_input(
                        "Of vul hier een nieuwe uitgever in (laat leeg om de keuze hierboven te gebruiken; "
                        "vorm: naam ; adres - plaats - NL ; contact)",
                        value="",
                        key=f"{edit_prefix}_publisher_new",
                    )

                    edit_price = st.number_input(
                        f"{price_label} (€)",
                        value=float(b["price"]) if pd.notna(b["price"]) else 0.0,
                        step=0.5,
                        key=f"{edit_prefix}_price",
                    )

                    current_shipping_cost = float(b["shipping_cost"]) if pd.notna(b["shipping_cost"]) else 0.0
                    if show_logistics:
                        SHIPPING_BW_OPTIONS = ["Vrije invoer", "3,75", "7,25"]
                        current_shipping_str = f"{current_shipping_cost:.2f}".replace(".", ",")
                        default_shipping_index = (
                            SHIPPING_BW_OPTIONS.index(current_shipping_str)
                            if current_shipping_str in SHIPPING_BW_OPTIONS
                            else 0
                        )
                        shipping_bw_choice = st.selectbox(
                            "Verzendkosten Boekwinkeltjes (€)",
                            SHIPPING_BW_OPTIONS,
                            index=default_shipping_index,
                            key=f"{edit_prefix}_shipping_bw_choice",
                        )
                        edit_shipping_cost_free = st.number_input(
                            "Verzendkosten Boekwinkeltjes - vrij bedrag (alleen gebruikt als je hierboven 'Vrije invoer' kiest)",
                            value=current_shipping_cost,
                            step=0.25,
                            key=f"{edit_prefix}_shipping_bw_free",
                        )

                    edit_amount = st.number_input(
                        "Voorraad",
                        value=int(b["amount"]) if pd.notna(b["amount"]) else 0,
                        step=1,
                        key=f"{edit_prefix}_amount",
                    )
                with col_b:
                    if show_logistics:
                        edit_location = st.text_input(
                            "Locatie (voeg R toe voor Rick, T voor Thuis)",
                            value=_s(b["location"]),
                            key=f"{edit_prefix}_location",
                        )
                    else:
                        # Niet zichtbaar in deze weergave; de bestaande waarde blijft gewoon staan.
                        edit_location = _s(b["location"])

                    edit_category3 = st.text_input(
                        "Categorie 3", value=_s(b["category3"]), key=f"{edit_prefix}_category3"
                    )
                    edit_language = st.text_input("Taal", value=_s(b["language"]), key=f"{edit_prefix}_language")
                    edit_short_desc = st.text_area(
                        "Bijzonderheden (jaar, pagina's, vorm, staat)",
                        value=_s(b["short_description"]),
                        height=100,
                        key=f"{edit_prefix}_short",
                    )
                    edit_long_desc = st.text_area(
                        "Meer info (in Nederlands)",
                        value=_s(b["long_description"]),
                        height=150,
                        key=f"{edit_prefix}_long",
                    )
                    # Het veld 'Synchronisatie naar Boekwinkeltjes' is bewust niet meer
                    # zichtbaar: die staat blijft gewoon zoals hij was, tenzij de
                    # wachtrij hieronder van Ja naar Nee gaat (zie verderop).
                    current_push_enabled = bool(b["push_enabled"]) if pd.notna(b["push_enabled"]) else True
                    current_queued = bool(b["queued"]) if pd.notna(b["queued"]) else False
                    edit_queued_choice = st.selectbox(
                        "Wachtrij?",
                        options=["Nee", "Ja"],
                        index=1 if current_queued else 0,
                        key=f"{edit_prefix}_queued",
                        help="'Ja' = dit boek wacht op handmatige controle en wordt niet gesynchroniseerd. "
                        "Zet je dit op 'Nee', dan gaat synchronisatie automatisch aan.",
                    )

                st.markdown("**📷 Afbeeldingen toevoegen**")
                new_uploaded_files = st.file_uploader(
                    "Afbeeldingen uploaden",
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=True,
                    key=f"{edit_prefix}_images_upload",
                )

                submitted = st.form_submit_button("Opslaan")

            if submitted:
                final_publisher = new_publisher_text.strip() or chosen_publisher_option
                if new_publisher_text.strip():
                    add_known_publisher(final_publisher)

                if show_logistics:
                    final_shipping_cost = (
                        edit_shipping_cost_free
                        if shipping_bw_choice == "Vrije invoer"
                        else float(shipping_bw_choice.replace(",", "."))
                    )
                else:
                    final_shipping_cost = current_shipping_cost  # niet zichtbaar in deze weergave: ongewijzigd

                edit_queued = edit_queued_choice == "Ja"
                # Synchronisatie blijft standaard ongewijzigd; wordt de wachtrij van Ja
                # naar Nee gezet, dan gaat 'ie automatisch aan (dat scheelt een handeling
                # na het controleren van een bulk-geïmporteerd boek).
                edit_push_enabled = current_push_enabled
                if current_queued and not edit_queued:
                    edit_push_enabled = True

                save_book_edits(
                    chosen_id,
                    {
                        "title": edit_title,
                        "author": edit_author,
                        "publisher": final_publisher,
                        "price": edit_price,
                        "shipping_cost": final_shipping_cost,
                        "shipping_cost_bol": edit_shipping_cost_bol,
                        "amount": int(edit_amount),
                        "ean": edit_ean,
                        "location": edit_location,
                        "category1": edit_category1,
                        "category2": edit_category2,
                        "category3": edit_category3,
                        "language": edit_language,
                        "short_description": edit_short_desc,
                        "long_description": edit_long_desc,
                        "push_enabled": edit_push_enabled,
                        "queued": edit_queued,
                    },
                    user_short_name=current_user_short_name(),
                    previous_values={
                        "title": b["title"],
                        "author": b["author"],
                        "publisher": b["publisher"],
                        "price": b["price"],
                        "shipping_cost": b["shipping_cost"],
                        "shipping_cost_bol": b["shipping_cost_bol"],
                        "amount": b["amount"],
                        "ean": b["ean"],
                        "location": b["location"],
                        "category1": b["category1"],
                        "category2": b["category2"],
                        "category3": b["category3"],
                        "language": b["language"],
                        "short_description": b["short_description"],
                        "long_description": b["long_description"],
                        "push_enabled": b["push_enabled"],
                        "queued": b["queued"],
                    },
                )

                if new_uploaded_files:
                    images_to_save = [
                        {"data": f.getvalue(), "content_type": f.type or "image/jpeg", "is_main": False}
                        for f in new_uploaded_files
                    ]
                    save_uploaded_images(chosen_id, images_to_save)

                load_books.clear()
                get_all_images.clear()
                if mode != "alles":
                    if int(edit_amount) <= 0:
                        st.session_state["pending_view_mode"] = "📕 Niet voorradig"
                    elif chosen_id < 0:
                        st.session_state["pending_view_mode"] = "📒 Voorradig, nog niet op BW"
                    else:
                        st.session_state["pending_view_mode"] = "📗 Voorradig"
                st.success("Opgeslagen! Wordt bij de eerstvolgende sync naar Boekwinkeltjes gestuurd.")
                st.rerun()

        st.divider()

        current_push_status = "Push naar Boekwinkeltjes" if (pd.isna(b["push_enabled"]) or b["push_enabled"]) else "Geen push"
        st.caption(f"Synchronisatiestatus: **{current_push_status}**")

        activity_rows = get_book_activity(chosen_id)
        if activity_rows:
            action_labels = {"created": "aangemaakt", "edited": "aangepast"}
            for row in activity_rows:
                if row["action"] == "stock_change":
                    status_label = (row.get("changed_fields") or "").split("|")[0]
                    st.caption(
                        f"{row['user_short_name']} — op '{status_label}' gezet — {format_datetime_nl(row['occurred_at'])}"
                    )
                    continue
                action_label = action_labels.get(row["action"], row["action"])
                changed = row.get("changed_fields")
                if changed:
                    field_list = changed.split("|")
                    if len(field_list) > 3:
                        shown = field_list[:3]
                        extra = len(field_list) - 3
                        woord = "veld" if extra == 1 else "velden"
                        fields_str = f"{', '.join(shown)}, en nog {extra} {woord}"
                    else:
                        fields_str = ", ".join(field_list)
                    action_label = f"{action_label} ({fields_str})"
                st.caption(
                    f"{row['user_short_name']} — {action_label} — {format_datetime_nl(row['occurred_at'])}"
                )

        st.caption(f"Laatst gesynchroniseerd met Boekwinkeltjes: {format_datetime_nl(b['last_synced_at'])}")

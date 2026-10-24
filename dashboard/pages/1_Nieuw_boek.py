"""
pages/1_Nieuw_boek.py — Nieuw boek toevoegen vanuit het dashboard.

Het boek wordt lokaal in Supabase gezet met een tijdelijk id, en pas bij de
eerstvolgende sync (of via de knop 'Sync boeken nu' op Home) daadwerkelijk
aangemaakt bij Boekwinkeltjes. Zodra dat gelukt is, krijgt het boek zijn
echte id en verschijnt het gewoon tussen de andere boeken.

Elk veld (behalve Locatie) heeft een 'versienummer' in zijn widget-key, dat na
elke succesvolle opslag omhoog gaat. Zo is elk veld na 'Nog een boek
toevoegen' een gegarandeerd verse, lege widget, in plaats van te vertrouwen op
het losstaand verwijderen van de oude waarde uit de sessiestatus (wat bij
sommige widgets in Streamlit niet altijd betrouwbaar leeg blijft ogen).

ISBN staat bovenaan: naast marktinfo (via een onofficieel endpoint) vult dat,
indien beschikbaar, ook Titel/Auteur/Uitgever/Taal/Bijzonderheden voor als
uitgangswaarde (zonder iets te overschrijven wat je al zelf had ingevuld).

Categorie 1 (en indien van toepassing Categorie 2), Uitgever, Verzendkosten
Boekwinkeltjes en de Bol-prijs staan buiten een formulier om, zodat de
afhankelijke velden direct meebewegen.
"""

from pathlib import Path

import base64
import re
from concurrent.futures import ThreadPoolExecutor

import streamlit as st

from common import (
    create_new_book_draft,
    load_books,
    get_known_publishers,
    add_known_publisher,
    render_logo,
    require_login,
    current_user_short_name,
    get_user_last_location,
    order_main_first,
    save_uploaded_images,
    lookup_boekwinkeltjes_market_info,
    lookup_book_metadata_external,
    match_subjects_to_category,
    find_matching_publisher,
    record_isbn_prefix_observation,
    lookup_publisher_by_isbn_prefix,
    info_box,
    find_existing_book_by_isbn,
    save_book_edits,
    determine_busstuk,
    get_shipping_costs,
    shipping_amount_label,
    shipping_options_labels,
    lookup_abebooks_lowest_price,
    abebooks_box_line,
    lookup_bol_competing_offers,
    format_price_dot,
    suggest_bulk_price,
    lookup_bol_catalog_product,
    lookup_bol_category_names,
    find_matching_category,
    clean_boekwinkeltjes_title_and_bijz,
    get_top_category_pairs,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS, category1_label_for_value, category2_label_for_value

st.set_page_config(page_title="Nieuw boek", page_icon="📑", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
current_user = require_login()
st.title("📑 Nieuw boek toevoegen")

# 'v' bepaalt de sleutel-suffix van elk veld (behalve Locatie). Na een succesvolle
# opslag gaat dit nummer omhoog, waardoor elk veld daarna een gegarandeerd nieuwe,
# lege widget is.
v = st.session_state.get("new_book_form_version", 0)


def k(name):
    """Bouwt een versie-afhankelijke widget-key, bijv. k('title') -> 'new_book_title_v3'."""
    return f"new_book_{name}_v{v}"


if "new_book_success_msg" in st.session_state and not st.session_state.get("new_book_just_saved"):
    del st.session_state["new_book_success_msg"]

# ---------- ISBN eerst: marktinfo + voorinvullen van een paar velden ----------

ean = st.text_input("ISBN", key=k("ean"))

isbn_clean = ean.strip()
isbn_is_valid = len(re.sub(r"\D", "", isbn_clean)) >= 10

if isbn_is_valid:
    # Eén keer per ISBN opzoeken (database) en onthouden: anders gebeurt het bij elke klik opnieuw.
    if st.session_state.get("new_book_existing_for") != isbn_clean:
        st.session_state["new_book_existing"] = find_existing_book_by_isbn(isbn_clean)
        st.session_state["new_book_existing_for"] = isbn_clean
    existing_book = st.session_state["new_book_existing"]
    if existing_book:
        location_text = f", op {existing_book['location']}" if existing_book.get("location") else ""
        st.warning(
            f"Dit ISBN staat al in je database: **{existing_book['title'] or '(geen titel)'}** "
            f"(voorraad: {existing_book['amount']}{location_text})."
        )
        existing_col_increase, existing_col_button = st.columns([1.3, 1])
        with existing_col_increase:
            if st.button("Voorraad +1", key="existing_book_increase"):
                save_book_edits(
                    existing_book["id"],
                    {"amount": (existing_book["amount"] or 0) + 1},
                    user_short_name=current_user_short_name(),
                    previous_values={"amount": existing_book["amount"]},
                )
                load_books.clear()
                st.session_state.pop("new_book_existing_for", None)
                st.success(f"Voorraad verhoogd naar {(existing_book['amount'] or 0) + 1}.")
        with existing_col_button:
            st.link_button("Naar boek", f"Boekdetails?book_id={existing_book['id']}")


# De verzendkosten voor briefpost en pakketpost komen uit de instellingen (pagina
# 'Hulp en instellingen'). Eenmaal per run ophalen, zodat de voorinvulling en de
# keuzelijst verderop gegarandeerd met dezelfde bedragen werken.
SHIPPING_BRIEFPOST, SHIPPING_PAKKETPOST = get_shipping_costs()
SHIPPING_BW_OPTIONS = shipping_options_labels(SHIPPING_BRIEFPOST, SHIPPING_PAKKETPOST)

market_info, market_error = None, None
bol_count, bol_laagste, bol_hoogste = None, None, None
external_metadata_preview = None
if isbn_is_valid:
    # Deze drie opzoekingen hebben niks met elkaar te maken, dus die halen we
    # gelijktijdig op in plaats van na elkaar — dat scheelt flink wachttijd,
    # aangezien het stuk voor stuk live netwerkverzoeken zijn.
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_market = executor.submit(lookup_boekwinkeltjes_market_info, ean)
        future_bol_offers = executor.submit(lookup_bol_competing_offers, isbn_clean)
        future_metadata = executor.submit(lookup_book_metadata_external, ean)
        market_info, market_error = future_market.result()
        bol_count, bol_laagste, bol_hoogste = future_bol_offers.result()
        external_metadata_preview = future_metadata.result()

known_publishers = get_known_publishers()
NEW_PUBLISHER_SENTINEL = "➕ Nieuwe uitgever invoeren..."

box_lines = []
bw_shows_nothing = False
bw_no_offers = False  # Boekwinkeltjes heeft nu niets te koop (ook als het boek ooit wel is verkocht)
if market_info:
    active = market_info.get("activeAmount") or 0
    bw_no_offers = active == 0
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
        box_lines.append(msg + ".")
    elif market_info.get("lastOrder"):
        box_lines.append(
            f"Bij Boekwinkeltjes wordt dit boek momenteel niet aangeboden, maar is voor het laatst verkocht "
            f"op {market_info['lastOrder']} voor het bedrag van {market_info['lastPrice']}."
        )
    else:
        bw_shows_nothing = True
        bw_no_offers = True
        box_lines.append("Bij Boekwinkeltjes wordt dit boek momenteel niet aangeboden.")
elif market_error == "Geen gegevens gevonden voor dit ISBN.":
    # Boekwinkeltjes kent dit ISBN helemaal niet: dat is hetzelfde als 'niet aangeboden'.
    bw_shows_nothing = True
    bw_no_offers = True
    box_lines.append("Bij Boekwinkeltjes wordt dit boek momenteel niet aangeboden.")
elif ean.strip() and ean.strip() != "0":
    st.caption(f"Kon geen marktgegevens ophalen: {market_error}")

if external_metadata_preview and external_metadata_preview.get("prices"):
    box_lines.append("Externe prijzen: " + ", ".join(external_metadata_preview["prices"]))

if bol_count is not None:
    if bol_count > 0:
        verkoper_woord = "verkoper" if bol_count == 1 else "verkopers"
        box_lines.append(
            f"Bij Bol wordt dit boek door {bol_count} {verkoper_woord} aangeboden voor "
            f"€{format_price_dot(bol_laagste)} t/m €{format_price_dot(bol_hoogste)}."
        )
    else:
        box_lines.append(
            "Ook bij Bol niet."
            if bw_no_offers
            else "Bij Bol wordt dit boek momenteel niet aangeboden."
        )

# Is het boek bij zowel Boekwinkeltjes als Bol nergens te koop? Dan als hulpmiddel de laagste
# prijs bij Abebooks erbij (anders ga je daar buiten de app handmatig kijken). Alleen dan
# wordt Abebooks aangeroepen — zie de toelichting bij lookup_abebooks_lowest_price in common.py.
abebooks_result = None
if isbn_is_valid and bw_no_offers and bol_count == 0:
    abebooks_result = lookup_abebooks_lowest_price(isbn_clean)
    box_lines.append(abebooks_box_line(abebooks_result))

if isbn_is_valid:
    bol_url = f"https://www.bol.com/nl/nl/s/?searchtext={isbn_clean}"
    if box_lines:
        box_col, bol_col = st.columns([4, 1])
        with box_col:
            info_box("<br>".join(box_lines))
        with bol_col:
            st.link_button("Open op Bol", bol_url, width="stretch")
    else:
        st.link_button("Open op Bol", bol_url)

if abebooks_result:
    st.link_button("Zoek op Abebooks", f"https://www.abebooks.com/servlet/SearchResults?isbn={isbn_clean}")
    with st.expander("Technische details van de Abebooks-opzoeking"):
        st.caption(
            "Wat Abebooks teruggaf. Handig om te controleren of het bedrag klopt, of om door te geven "
            "als er iets niet goed gelezen wordt."
        )
        if abebooks_result["status"] == "price":
            st.write("Bedrag gelezen uit het veld:")
            st.code(abebooks_result["source"] or "(onbekend)", language=None)
            if abebooks_result.get("rate_note"):
                st.write(f"Omrekening naar euro: {abebooks_result['rate_note']}.")
        elif abebooks_result["message"]:
            st.write(f"Melding: {abebooks_result['message']}.")
        st.code(abebooks_result["raw"] or "(geen antwoord ontvangen)", language="json")

# Titel/Auteur/Taal/Bijzonderheden/Uitgever (marktinfo + ISBNdb) en omslagfoto/
# beschrijving als uitgangswaarde voorinvullen — maar alleen de eerste keer voor
# deze ISBN, en alleen in lege velden, zodat we niets overschrijven wat je zelf
# al had aangepast.
if isbn_is_valid and st.session_state.get("new_book_prefilled_isbn") != ean:
    if market_info:
        cleaned_titel, cleaned_bijz = clean_boekwinkeltjes_title_and_bijz(
            market_info.get("titel"), market_info.get("bijz")
        )
        prefill_map = {
            k("title"): cleaned_titel,
            k("author"): market_info.get("schrijver"),
            k("language"): market_info.get("taal"),
            k("short"): cleaned_bijz,
        }
        for key, value in prefill_map.items():
            if value:
                st.session_state[key] = value

        suggested_publisher = market_info.get("uitgever")
        if suggested_publisher:
            if suggested_publisher in known_publishers:
                st.session_state[k("publisher_dd")] = suggested_publisher
                record_isbn_prefix_observation(ean, suggested_publisher)
            else:
                fuzzy_match = find_matching_publisher(suggested_publisher)
                if fuzzy_match:
                    st.session_state[k("publisher_dd")] = fuzzy_match
                    record_isbn_prefix_observation(ean, fuzzy_match)
                else:
                    st.session_state[k("publisher_dd")] = NEW_PUBLISHER_SENTINEL
                    st.session_state[k("publisher_new")] = suggested_publisher

    metadata = external_metadata_preview
    if metadata:
        if metadata.get("title") and not st.session_state.get(k("title")):
            st.session_state[k("title")] = metadata["title"]
        if metadata.get("author") and not st.session_state.get(k("author")):
            st.session_state[k("author")] = metadata["author"]
        if metadata.get("language") and not st.session_state.get(k("language")):
            st.session_state[k("language")] = metadata["language"]
        if metadata.get("bijz") and not st.session_state.get(k("short")):
            st.session_state[k("short")] = metadata["bijz"]
        if metadata.get("description") and not st.session_state.get(k("long")):
            st.session_state[k("long")] = metadata["description"]
        if metadata.get("publisher") and not st.session_state.get(k("publisher_new")):
            if metadata["publisher"] in known_publishers:
                st.session_state[k("publisher_dd")] = metadata["publisher"]
                record_isbn_prefix_observation(ean, metadata["publisher"])
            else:
                fuzzy_match = find_matching_publisher(metadata["publisher"])
                if fuzzy_match:
                    st.session_state[k("publisher_dd")] = fuzzy_match
                    record_isbn_prefix_observation(ean, fuzzy_match)
                else:
                    st.session_state[k("publisher_dd")] = NEW_PUBLISHER_SENTINEL
                    st.session_state[k("publisher_new")] = metadata["publisher"]
        if metadata.get("cover_bytes"):
            st.session_state[k("external_images")] = [
                {
                    "data": metadata["cover_bytes"],
                    "content_type": metadata.get("cover_content_type", "image/jpeg"),
                    "label": f"Omslag via {metadata.get('cover_source') or metadata.get('source', 'internet')}",
                }
            ]
            st.session_state[k("cover_note")] = None
        else:
            rejected_sources = metadata.get("cover_rejected")
            st.session_state[k("cover_note")] = (
                f"Geen omslag toegevoegd: {', '.join(rejected_sources)} gaf alleen een plaatshouder (\"geen omslag beschikbaar\")."
                if rejected_sources else None
            )

    # Bol als laatste aanvulling voor wat nog steeds ontbreekt (best-effort, zie
    # kanttekening bij lookup_bol_catalog_product)
    bol_product = lookup_bol_catalog_product(ean)
    if bol_product:
        if bol_product.get("title") and not st.session_state.get(k("title")):
            st.session_state[k("title")] = bol_product["title"]
        if bol_product.get("author") and not st.session_state.get(k("author")):
            st.session_state[k("author")] = bol_product["author"]
        if bol_product.get("description") and not st.session_state.get(k("long")):
            st.session_state[k("long")] = bol_product["description"]
        if not st.session_state.get(k("short")):
            bijz_parts = []
            if bol_product.get("year"):
                bijz_parts.append(bol_product["year"])
            if bol_product.get("pages"):
                bijz_parts.append(f"{bol_product['pages']}pp")
            if bol_product.get("binding"):
                bijz_parts.append(bol_product["binding"])
            if bijz_parts:
                st.session_state[k("short")] = ", ".join(bijz_parts[:2]) + (
                    f", {bijz_parts[2]}" if len(bijz_parts) > 2 else ""
                )
        if bol_product.get("manufacturer_name") and not st.session_state.get(k("publisher_new")) and not st.session_state.get(k("publisher_dd")):
            bol_publisher_candidate = " ; ".join(
                [
                    bol_product.get("manufacturer_name") or "",
                    bol_product.get("manufacturer_address") or "",
                    bol_product.get("manufacturer_contact") or "",
                ]
            )
            if bol_publisher_candidate in known_publishers:
                st.session_state[k("publisher_dd")] = bol_publisher_candidate
                record_isbn_prefix_observation(ean, bol_publisher_candidate)
            else:
                fuzzy_match = find_matching_publisher(bol_publisher_candidate)
                if fuzzy_match:
                    st.session_state[k("publisher_dd")] = fuzzy_match
                    record_isbn_prefix_observation(ean, fuzzy_match)
                else:
                    st.session_state[k("publisher_dd")] = NEW_PUBLISHER_SENTINEL
                    st.session_state[k("publisher_new")] = bol_publisher_candidate

        # Geen enkele bron kon een uitgever vinden: als laatste redmiddel kijken of
        # het ISBN-uitgeverscijferblok al vaak genoeg aan een uitgever is gekoppeld.
        if not st.session_state.get(k("publisher_dd")) and not st.session_state.get(k("publisher_new")):
            learned_publisher = lookup_publisher_by_isbn_prefix(ean)
            if learned_publisher:
                st.session_state[k("publisher_dd")] = learned_publisher

        if metadata and metadata.get("subjects") and st.session_state.get(k("category1"), "(leeg)") == "(leeg)":
            matched_cat1, matched_cat2 = match_subjects_to_category(metadata["subjects"])
            if matched_cat1:
                st.session_state[k("category1")] = category1_label_for_value(matched_cat1)
                if matched_cat2:
                    st.session_state[k(f"category2_dd_{matched_cat1}")] = category2_label_for_value(
                        matched_cat1, matched_cat2
                    )

    # Bol als laatste aanvulling voor de categorie, als er nog steeds niets is gekozen
    if st.session_state.get(k("category1"), "(leeg)") == "(leeg)":
        bol_category_names = lookup_bol_category_names(ean)
        if bol_category_names:
            bol_cat1, bol_cat2 = find_matching_category(bol_category_names)
            if bol_cat1:
                st.session_state[k("category1")] = category1_label_for_value(bol_cat1)
                if bol_cat2:
                    st.session_state[k(f"category2_dd_{bol_cat1}")] = category2_label_for_value(
                        bol_cat1, bol_cat2
                    )

    # Afmetingen: Google Books/ISBNdb (via metadata) hebben voorkeur, anders Bol
    # als laatste terugval — bepaalt het "Busstuk?"-veld en de voorgestelde
    # verzendkosten hieronder.
    length_cm = width_cm = thickness_cm = None
    if metadata and metadata.get("length_cm"):
        length_cm, width_cm, thickness_cm = metadata["length_cm"], metadata.get("width_cm"), metadata["thickness_cm"]
    elif bol_product and bol_product.get("length_cm"):
        length_cm, width_cm, thickness_cm = bol_product["length_cm"], bol_product.get("width_cm"), bol_product["thickness_cm"]
    if length_cm is not None:
        st.session_state[k("length_cm")] = length_cm
        st.session_state[k("width_cm")] = width_cm
        st.session_state[k("thickness_cm")] = thickness_cm
    _, suggested_shipping_cost, _ = determine_busstuk(
        length_cm, thickness_cm, SHIPPING_BRIEFPOST, SHIPPING_PAKKETPOST
    )
    st.session_state[k("shipping_bw_choice")] = shipping_amount_label(suggested_shipping_cost)

    # Boekwinkeltjes heeft niets, maar Bol wel: prijs voorstellen op basis van Bol's
    # laagste prijs (min €2,25 marge, min de verzendkosten die hierboven bij dit boek
    # zijn gekozen — briefpost of pakketpost — en afgerond naar beneden op ,45 of
    # ,95, met €3,95 als bodem). Pas hier, omdat de verzendkosten eerst bekend moeten zijn.
    if bw_shows_nothing and bol_count and bol_count > 0 and bol_laagste and k("price") not in st.session_state:
        suggested_bw_price = suggest_bulk_price(float(bol_laagste) - 2.25 - suggested_shipping_cost)
        if suggested_bw_price is not None:
            st.session_state[k("price")] = suggested_bw_price

    st.session_state["new_book_prefilled_isbn"] = ean
    st.rerun()

st.divider()

col_a, col_b = st.columns(2)

CAT1_LABELS = ["(leeg)"] + [label for _, label in CATEGORY1_OPTIONS]
CAT1_VALUES = [""] + [value for value, _ in CATEGORY1_OPTIONS]

with col_a:
    title = st.text_input("Titel *", key=k("title"))
    author = st.text_input("Auteur", key=k("author"))

    pub_options = [NEW_PUBLISHER_SENTINEL] + known_publishers
    chosen_publisher_option = st.selectbox("Uitgever", pub_options, key=k("publisher_dd"))
    if chosen_publisher_option == NEW_PUBLISHER_SENTINEL:
        publisher = st.text_input(
            "Nieuwe uitgever (vorm: naam ; adres - plaats - NL ; contact)",
            key=k("publisher_new"),
        )
    else:
        publisher = chosen_publisher_option

    price = st.number_input("Prijs webshop en Boekwinkeltjes (€) *", min_value=0.0, step=0.5, key=k("price"))

    busstuk_length_cm = st.session_state.get(k("length_cm"))
    busstuk_thickness_cm = st.session_state.get(k("thickness_cm"))
    _, shipping_cost_unknown, busstuk_message = determine_busstuk(
        busstuk_length_cm, busstuk_thickness_cm, SHIPPING_BRIEFPOST, SHIPPING_PAKKETPOST
    )
    st.info(busstuk_message)

    # Zonder keuze (bijv. nog geen ISBN ingevuld) gaan we uit van 'onbekend': het hoogste
    # bedrag, net als het advies hierboven. Staat er nog een oude keuze die niet meer in
    # de lijst voorkomt (bijv. omdat je de bedragen op 'Hulp en instellingen' hebt gewijzigd
    # terwijl dit formulier openstond), dan terugvallen daarop in plaats van vast te lopen.
    shipping_bw_fallback = shipping_amount_label(shipping_cost_unknown)
    if st.session_state.get(k("shipping_bw_choice"), shipping_bw_fallback) not in SHIPPING_BW_OPTIONS:
        st.session_state[k("shipping_bw_choice")] = shipping_bw_fallback
    shipping_bw_default = st.session_state.get(k("shipping_bw_choice"), shipping_bw_fallback)
    shipping_bw_index = SHIPPING_BW_OPTIONS.index(shipping_bw_default)
    shipping_bw_choice = st.selectbox(
        "Verzendkosten webshop en Boekwinkeltjes (€)", SHIPPING_BW_OPTIONS, index=shipping_bw_index, key=k("shipping_bw_choice")
    )
    if shipping_bw_choice == "Vrije invoer":
        shipping_cost = st.number_input(
            "Verzendkosten webshop en Boekwinkeltjes - vrij bedrag (€)",
            min_value=0.0,
            step=0.25,
            key=k("shipping_bw_free"),
        )
    else:
        shipping_cost = float(shipping_bw_choice.replace(",", "."))

    amount = st.number_input("Voorraad *", min_value=0, step=1, value=1, key=k("amount"))

    isbn_present = isbn_is_valid
    if isbn_present:
        bol_unsuitable = st.checkbox(
            "Boek is ongeschikt voor Bol (bijv. verboden type)", key=k("bol_unsuitable")
        )
        if bol_unsuitable:
            st.markdown("**Prijs Bol, incl. verzendkosten en commissie (€)**")
            st.write("Boek ongeschikt voor Bol")
            shipping_cost_bol = None
        else:
            floor_bol = round(price + shipping_cost + 2.25, 2)
            bol_count, bol_laagste_offer, _ = lookup_bol_competing_offers(ean)
            if bol_count and bol_laagste_offer:
                suggested_bol = suggest_bulk_price(bol_laagste_offer, floor=floor_bol)
            else:
                suggested_bol = floor_bol
            shipping_cost_bol = st.number_input(
                "Prijs Bol, incl. verzendkosten en commissie (€)",
                min_value=0.0,
                step=0.5,
                value=suggested_bol,
                key=k("shipping_bol"),
            )
            st.caption("(aanpasbare suggestie)")
    else:
        st.markdown("**Prijs Bol, incl. verzendkosten en commissie (€)**")
        st.write("Boek ongeschikt voor Bol")
        shipping_cost_bol = None

with col_b:
    if "new_book_default_location" not in st.session_state:
        st.session_state["new_book_default_location"] = (
            (get_user_last_location(current_user_short_name()) or "") if current_user_short_name() != "onbekend" else ""
        )
    default_location = st.session_state["new_book_default_location"]
    location = st.text_input(
        "Locatie",
        value=default_location,
        key="new_book_location",  # geen versie: blijft bewust staan voor de volgende invoer
    )

    # Categorie 1 en 2 staan in een 'fragment': kies je de eerste categorie, dan wordt alleen dit stukje opnieuw
    # getekend (met de tweede lijst) en niet de hele pagina met al zijn opzoekingen. Dat scheelt de wachttijd.
    def _quick_category_label(pair):
        c1, c2 = pair
        label = category1_label_for_value(c1)
        label = label[:1].upper() + label[1:]
        return f"{label} › {category2_label_for_value(c1, c2)}" if c2 else label

    try:
        quick_pairs = get_top_category_pairs(8)
    except Exception:
        quick_pairs = []
    quick_by_label = {_quick_category_label(pair): pair for pair in quick_pairs}

    def _apply_quick_category():
        pair = quick_by_label.get(st.session_state.get(k("quick_category")))
        if not pair:
            return
        c1, c2 = pair
        st.session_state[k("category1")] = category1_label_for_value(c1)
        if c2 and c1 in CATEGORY2_OPTIONS:
            st.session_state[k(f"category2_dd_{c1}")] = category2_label_for_value(c1, c2)

    @st.fragment
    def category_pickers():
        if quick_by_label:
            st.radio("Snelkeuze categorie (de meest gebruikte)", list(quick_by_label), index=None, horizontal=True,
                     key=k("quick_category"), on_change=_apply_quick_category)
        chosen_cat1_label = st.selectbox("Categorie 1 *", CAT1_LABELS, key=k("category1"))
        cat1_value = CAT1_VALUES[CAT1_LABELS.index(chosen_cat1_label)]
        if cat1_value in CATEGORY2_OPTIONS:
            st.selectbox("Categorie 2", [label for _, label in CATEGORY2_OPTIONS[cat1_value]], key=k(f"category2_dd_{cat1_value}"))
        else:
            st.text_input("Categorie 2", key=k("category2_txt"))

    category_pickers()

    category3 = st.text_input("Categorie 3", key=k("category3"))
    language = st.text_input("Taal", value="NL", key=k("language"))
    short_description = st.text_area(
        "Bijzonderheden (jaar, pagina's, vorm, staat)", height=100, key=k("short")
    )
    long_description = st.text_area(
        "Meer info (in Nederlands)", height=150, key=k("long")
    )
    push_enabled_choice = st.selectbox(
        "Synchronisatie",
        options=["Push naar de webshop, Boekwinkeltjes en eventueel Bol", "Geen push (alleen lokaal bewaren)"],
        key=k("push_enabled"),
    )

st.divider()
st.subheader("📷 Afbeeldingen")
st.caption(
    "Kies de voorkant en eventueel twee extra foto's voor de webshop en overige foto's. De voorkant gaat als eerste naar Boekwinkeltjes en wordt daar de "
    "hoofdafbeelding. Daar hoef je na het opslaan dus niets meer aan te doen."
)


def collect_images():
    """Leest de gekozen foto's uit de sessie. Geeft (voorkant, overige, alle, automatisch_gevonden) terug."""
    front_file = st.session_state.get(k("front_upload"))
    other_files = st.session_state.get(k("images_upload")) or []
    external = st.session_state.get(k("external_images"), [])
    front = None
    if front_file is not None:
        front = {"data": front_file.getvalue(), "content_type": front_file.type or "image/jpeg", "label": front_file.name}
    elif external:
        front = external[0]
    web_files = st.session_state.get(k("web_upload")) or []
    web = [{"data": f.getvalue(), "content_type": f.type or "image/jpeg", "label": f.name, "web_extra": n < 2}
           for n, f in enumerate(web_files)]
    others = web + [{"data": f.getvalue(), "content_type": f.type or "image/jpeg", "label": f.name} for f in other_files]
    return front, others, ([front] if front else []) + others, external


# De foto's staan in een 'fragment': een foto kiezen of verwijderen tekent alleen dit stuk opnieuw en niet de hele
# pagina met al zijn opzoekingen. Bij opslaan lezen we de foto's uit de sessie (collect_images).
@st.fragment
def image_section():
    front_file = st.file_uploader(
        "Voorkant",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=False,
        key=k("front_upload"),
    )
    st.file_uploader(
        "Extra foto's voor de webshop (hoogstens 2, naast de voorkant)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=k("web_upload"),
        help="Bijvoorbeeld de achterkant. Deze foto's staan ook op de webshop; de overige foto's alleen bij Boekwinkeltjes en Bol.",
    )
    if len(st.session_state.get(k("web_upload")) or []) > 2:
        st.caption("Alleen de eerste twee gaan naar de webshop; de rest wordt een gewone foto.")
    st.file_uploader(
        "Overige foto's (alleen Boekwinkeltjes en Bol)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=k("images_upload"),
    )

    # De voorkant is de eigen upload, of anders de automatisch gevonden omslag (via ISBNdb/Google Books/Open Library).
    front_image, _others, all_images, external_images = collect_images()
    cover_note = st.session_state.get(k("cover_note"))
    if front_file is not None:
        if external_images:
            st.caption("De automatisch gevonden omslag wordt niet gebruikt, omdat je zelf een voorkant hebt gekozen.")
    elif not external_images and cover_note:
        st.caption(cover_note)

    if all_images and front_image is None:
        st.warning("Je hebt geen voorkant gekozen: de eerste overige foto wordt nu de hoofdafbeelding.")

    thumbs_per_row = 5
    for row_start in range(0, len(all_images), thumbs_per_row):
        row_images = all_images[row_start : row_start + thumbs_per_row]
        thumb_cols = st.columns(thumbs_per_row)
        for offset, img in enumerate(row_images):
            i = row_start + offset
            with thumb_cols[offset]:
                image_b64 = base64.b64encode(img["data"]).decode("ascii")
                st.markdown(
                    f'<img src="data:{img["content_type"]};base64,{image_b64}" '
                    f'style="height:320px; width:auto; max-width:100%; object-fit:contain;" />',
                    unsafe_allow_html=True,
                )
                st.caption(img["label"])
                if i == 0:
                    st.markdown(
                        "<div style='text-align:center;'>⭐ Hoofdafbeelding</div>",
                        unsafe_allow_html=True,
                    )
                if img in external_images and front_file is None:
                    if st.button("Verwijderen", key=k(f"remove_external_{i}")):
                        st.session_state[k("external_images")] = []
                        st.rerun(scope="fragment")


image_section()

just_saved = st.session_state.get("new_book_just_saved", False)
button_label = "Nog een boek toevoegen" if just_saved else "Boek toevoegen"

if st.button(button_label, key="new_book_submit"):
    if just_saved:
        # Terug naar de normale knop-tekst; het formulier is al leeg sinds het opslaan
        st.session_state["new_book_just_saved"] = False
        st.session_state.pop("new_book_success_msg", None)
        st.rerun()
    elif not title or price <= 0:
        st.error("Titel en een prijs groter dan €0 zijn verplicht.")
    elif st.session_state.get(k("category1"), "(leeg)") in ("(leeg)", ""):
        st.error("Categorie 1 is verplicht: zonder categorie is het boek in de webshop niet te vinden via de categorieën.")
    else:
        if chosen_publisher_option == NEW_PUBLISHER_SENTINEL and publisher.strip():
            add_known_publisher(publisher)
        # De categorieën staan in een fragment; hun waarde lezen we dus uit de sessie.
        _c1_label = st.session_state.get(k("category1"), "(leeg)")
        category1 = CAT1_VALUES[CAT1_LABELS.index(_c1_label)] if _c1_label in CAT1_LABELS else ""
        if category1 in CATEGORY2_OPTIONS:
            _c2_options = CATEGORY2_OPTIONS[category1]
            _c2_label = st.session_state.get(k(f"category2_dd_{category1}"), _c2_options[0][1])
            category2 = next((value for value, label in _c2_options if label == _c2_label), _c2_options[0][0])
        else:
            category2 = st.session_state.get(k("category2_txt"), "")
        temp_id = create_new_book_draft(
            {
                "title": title,
                "author": author,
                "publisher": publisher,
                "price": price,
                "shipping_cost": shipping_cost,
                "shipping_cost_bol": shipping_cost_bol,
                "amount": int(amount),
                "ean": ean,
                "location": location,
                "category1": category1,
                "category2": category2,
                "category3": category3,
                "language": language,
                "short_description": short_description,
                "long_description": long_description,
                "push_enabled": push_enabled_choice == "Push naar de webshop, Boekwinkeltjes en eventueel Bol",
                "length_cm": st.session_state.get(k("length_cm")),
                "width_cm": st.session_state.get(k("width_cm")),
                "thickness_cm": st.session_state.get(k("thickness_cm")),
            },
            user_short_name=current_user_short_name(),
        )

        _front_image, _other_images, all_images, _external = collect_images()
        if all_images:
            # De gekozen hoofdfoto vooraan: de volgorde bepaalt wat Boekwinkeltjes als eerste krijgt.
            images_to_save = order_main_first(
                [{"data": img["data"], "content_type": img["content_type"], "web_extra": bool(img.get("web_extra"))} for img in all_images],
                0,
            )
            save_uploaded_images(temp_id, images_to_save)

        load_books.clear()

        # Volgend, gegarandeerd leeg formulier: de versie omhoog zodat elk veld
        # (behalve Locatie) een verse widget krijgt
        st.session_state.pop("new_book_prefilled_isbn", None)
        st.session_state.pop("new_book_existing_for", None)
        st.session_state["new_book_form_version"] = v + 1

        if push_enabled_choice == "Push naar de webshop, Boekwinkeltjes en eventueel Bol":
            st.session_state["new_book_success_msg"] = (
                f"Het boek is klaargezet (tijdelijk id {temp_id}) en komt online:\n"
                "- Bij de webshop aan het einde van de komende nacht.\n"
                "- Bij Boekwinkeltjes gedurende de eerstvolgende druppeltijd.\n"
                "- Eventueel bij Bol na de eerstvolgende synchronisatie."
            )
        else:
            st.session_state["new_book_success_msg"] = (
                f"Boek lokaal opgeslagen (tijdelijk id {temp_id}) met 'Geen push' — "
                "dit boek wordt niet naar Boekwinkeltjes gestuurd totdat je dat aanpast."
            )
        st.session_state["new_book_just_saved"] = True
        st.rerun()

if just_saved and "new_book_success_msg" in st.session_state:
    st.success(st.session_state["new_book_success_msg"])

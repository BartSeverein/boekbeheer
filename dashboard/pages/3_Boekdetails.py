"""
pages/3_Boekdetails.py — Eén pagina om een boek in detail te bekijken en te bewerken, met bovenaan
filterknoppen (Voorradig, Niet voorradig, Nog niet op BW, Zonder categorie, Zonder uitgever) en filters op
invoerdatum, auteur en taal. Zonder filters zie je alle boeken.
Is alleen 'Niet voorradig' gekozen, dan zijn locatie en verzendkosten niet zichtbaar en heet de prijs
'Laatste prijs Boekwinkeltjes'.
"""

import re
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import streamlit as st
from i18n import category_label, category_option, is_english, t, tdf
from bijz_en import bijz_input, finalize as finalize_bijz, finalize_long, long_label
from pathlib import Path

from common import (
    load_books,
    get_long_description,
    load_orders,
    get_bol_offer_eans,
    get_bol_offer_stocks,
    find_main_image_index,
    get_all_images,
    image_identity_urls,
    format_price,
    format_datetime_nl,
    front_upload_allowed,
    save_front_image,
    format_isbn,
    normalize_isbn,
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
    delete_unplaced_book,
    lookup_bol_competing_offers,
    format_price_dot,
    determine_busstuk,
    get_shipping_costs,
    shipping_options_labels,
    shipping_format_label,
    lookup_abebooks_lowest_price,
    abebooks_box_line,
    get_top_category_pairs,
    set_web_extra,
    set_front_image,
    get_book_bol_and_sales,
    MAX_WEB_EXTRA,
)
from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS, category1_label_for_value, category2_label_for_value


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

MIN_PRIJS = 3.95   # lagere prijzen worden niet opgeslagen

st.set_page_config(page_title="Boekdetails", page_icon="📖", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
current_user = require_login()
is_admin = current_user is None or current_user.get("role") == "beheerder"
st.title(t('📖 Boekdetails'))

# Bevestiging van het zojuist opgeslagen boek: één keer tonen en daarna weghalen.
_saved_msg = st.session_state.pop("book_saved_msg", None)
if _saved_msg:
    st.success(_saved_msg)

    import time as _t
    import streamlit.components.v1 as _components
    # Na het opslaan naar boven springen, zodat de bevestiging meteen in beeld is.
    _components.html(
        "<script>/*" + str(_t.time()) + "*/(function(){var d=window.parent.document;"
        "var els=[d.querySelector('[data-testid=stMain]'),d.querySelector('section.main'),"
        "d.querySelector('[data-testid=stAppViewContainer]'),d.scrollingElement,d.documentElement];"
        "els.forEach(function(e){if(e){try{e.scrollTo({top:0,behavior:'instant'});}catch(x){e.scrollTop=0;}}});"
        "window.parent.scrollTo(0,0);})();</script>",
        height=0,
    )

books_all, orders = load_books(), load_orders()

# Filterknoppen (combineerbaar). Staan er geen filters aan, dan zie je alle boeken.
F_STOCK = "📗 Voorradig"
F_SOLD = "📕 Niet voorradig"
F_NEWBW = "📒 Nog niet op BW"
F_NOCAT = "🏷️ Zonder categorie"
F_NOPUB = "🏢 Zonder uitgever"
F_BOLZERO = "🧹 Voorraad 0 op Bol, zonder order"
FILTER_OPTIONS = [F_STOCK, F_SOLD, F_NEWBW, F_NOCAT, F_NOPUB, F_BOLZERO]
ALL_AUTHORS = "(alle auteurs)"
NO_LANGUAGE = ""   # staat voor boeken zonder taal in de taalfilter

# Invoerdatum: 'Te koop sinds' (listing_date van Boekwinkeltjes); voor boeken die nog niet bij Boekwinkeltjes staan de laatste wijziging in de app.
if not books_all.empty:
    _inv = pd.to_datetime(books_all["listing_date"], errors="coerce", utc=True).fillna(
        pd.to_datetime(books_all["local_updated_at"], errors="coerce", utc=True)
    )
    books_all = books_all.assign(_invoer=_inv.dt.tz_convert("Europe/Amsterdam").dt.tz_localize(None).dt.normalize())

preselect_id = st.session_state.pop("preselect_book_id", None)
if preselect_id is None:
    # Vanuit een link die in een nieuw tabblad is geopend (bijv. 'Naar boek' bij
    # Toevoegen in bulk) is er geen sessiestatus om uit te lezen — dan komt het
    # boek-id via de URL binnen.
    book_id_param = st.query_params.get("book_id")
    if book_id_param:
        try:
            preselect_id = int(book_id_param)
        except ValueError:
            preselect_id = None
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
            st.session_state["book_filters"] = [F_STOCK] if has_stock else [F_SOLD]
            # Overige filters leegmaken, zodat het boek zeker in de lijst staat.
            for _k in ("book_f_author", "book_f_lang", "book_f_from", "book_f_to"):
                st.session_state.pop(_k, None)

# Na het opslaan schuift de keuze mee met het boek (bijv. van 'Voorradig' naar 'Niet voorradig' als de
# voorraad op 0 is gezet). Dat moet vóór het aanmaken van de filterknoppen worden toegepast.
if "pending_view_filters" in st.session_state:
    st.session_state["book_filters"] = st.session_state.pop("pending_view_filters")
st.session_state.setdefault("book_filters", [F_STOCK])

filters = st.multiselect(
    t('Filters'), FILTER_OPTIONS, key="book_filters", format_func=t, placeholder=t('Kies filters (geen filter = alle boeken)'),
)
only_sold = F_SOLD in filters and F_STOCK not in filters
show_logistics = not only_sold  # locatie en verzendkosten
price_label = "Laatste prijs webshop en BW" if only_sold else "Prijs webshop en BW"

if not books_all.empty:
    _authors = sorted({a.strip() for a in books_all["author"].dropna().astype(str) if a.strip()}, key=str.lower)
    _lang_counts = books_all["language"].fillna("").astype(str).str.strip().value_counts()
    _lang_options = list(_lang_counts.index)

    def _lang_label(v):
        return f"(geen taal) ({_lang_counts[v]})" if v == NO_LANGUAGE else f"{v} ({_lang_counts[v]})"

    # Een bewaarde keuze die niet meer bestaat (auteur of taal is intussen aangepast) weghalen, anders geeft Streamlit een fout.
    if st.session_state.get("book_f_author", ALL_AUTHORS) not in [ALL_AUTHORS] + _authors:
        st.session_state.pop("book_f_author", None)
    if "book_f_lang" in st.session_state:
        st.session_state["book_f_lang"] = [v for v in st.session_state["book_f_lang"] if v in _lang_options]

    _c1, _c2, _c3, _c4 = st.columns(4)
    f_from = _c1.date_input(t('Invoerdatum vanaf'), value=None, format="DD-MM-YYYY", key="book_f_from")
    f_to = _c2.date_input(t('Invoerdatum tot en met'), value=None, format="DD-MM-YYYY", key="book_f_to")
    f_author = _c3.selectbox(t('Auteur'), [ALL_AUTHORS] + _authors, key="book_f_author")
    f_langs = _c4.multiselect(t('Taal'), _lang_options, format_func=_lang_label, key="book_f_lang", placeholder=t('Alle talen'))

    books = books_all
    _amount = books["amount"].fillna(0)
    if F_STOCK in filters and F_SOLD not in filters:
        books = books[_amount > 0]
    elif F_SOLD in filters and F_STOCK not in filters:
        books = books[_amount == 0]
    if F_NEWBW in filters:
        books = books[books["id"] < 0]
    if F_NOCAT in filters:
        books = books[books["category1"].fillna("").astype(str).str.strip().eq("")]
    if F_NOPUB in filters:
        books = books[books["publisher"].fillna("").astype(str).str.strip().eq("")
                      & books["publisher_name"].fillna("").astype(str).str.strip().eq("")]
    if F_BOLZERO in filters:
        # Voorraad 0, er staat nog een Bol-aanbieding, en er is nooit een bestelling voor het boek geweest: wel opruimen, maar eerst even nagaan waarom.
        _ordered = set(orders["book_id"].dropna().astype(int)) if not orders.empty else set()
        books = books[(books["amount"].fillna(0) == 0) & books["ean"].astype(str).isin(get_bol_offer_eans())
                      & ~books["id"].isin(_ordered)]
    if f_from:
        books = books[books["_invoer"] >= pd.Timestamp(f_from)]
    if f_to:
        books = books[books["_invoer"] <= pd.Timestamp(f_to)]
    if f_author != ALL_AUTHORS:
        books = books[books["author"].fillna("").astype(str).str.strip() == f_author]
    if f_langs:
        books = books[books["language"].fillna("").astype(str).str.strip().isin(f_langs)]
    # Bij een of meer smalle filters tonen we meteen de lijst (nieuwste bovenaan), anders pas na een zoekterm.
    has_extra = bool({F_NEWBW, F_NOCAT, F_NOPUB, F_BOLZERO} & set(filters)) or bool(f_from or f_to or f_langs) or f_author != ALL_AUTHORS
else:
    books = books_all
    has_extra = False

if books.empty:
    st.info(t("Geen boeken die aan de gekozen filters voldoen.") if not books_all.empty else t("Nog geen boeken om te tonen."))
else:
    default_search = ""
    if preselect_id is not None:
        match = books[books["id"] == preselect_id]
        if not match.empty:
            default_search = match.iloc[0]["title"]

    search = st.text_input(
        t('🔎 Zoek op titel, auteur, ISBN of locatie (bijv. 0412) binnen de gekozen filters.'), default_search, key="book_search_input"
    )

    if search or has_extra:
        s = search.strip().lower()
        # Locatie: alles wat met de zoekterm begint (0412 vindt 0412, 0412A, 0412B...); spaties negeren we, dus "0111 a" vindt "0111A".
        s_loc = s.replace(" ", "")
        loc_norm = books["location"].fillna("").astype(str).str.lower().str.replace(" ", "", regex=False)
        if 4 <= len(s_loc) <= 6 and s_loc[:4].isdigit():
            # Vier tot zes tekens waarvan de eerste vier cijfers zijn (0412, 0111A): dat is een locatie.
            # Dan alleen op locatie zoeken, anders komen ook ISBN's en boeknummers met die cijfers erin mee.
            mask = loc_norm.str.startswith(s_loc)
        else:
            mask = (
                books["title"].str.lower().str.contains(s, na=False, regex=False)
                | books["author"].str.lower().str.contains(s, na=False, regex=False)
                | books["book_number"].astype(str).str.contains(s, na=False, regex=False)
                | books["ean"].astype(str).str.contains(s, na=False, regex=False)
                | (loc_norm.str.startswith(s_loc) if s_loc else False)
            )
        filtered = books[mask] if search else books
        if search and loc_norm[mask].str.startswith(s_loc).any():
            # Staat er een locatie bij de resultaten, dan eerst op locatie sorteren (daarna titel), handig voor "alles van 0412".
            filtered = filtered.assign(_loc=loc_norm[mask]).sort_values(["_loc", "title"], na_position="last").drop(columns="_loc")

        if F_NOCAT in filters or (has_extra and not search):
            # Nieuwste bovenaan: boeken die nog niet bij Boekwinkeltjes staan (negatief id, nog aan te maken) eerst, daarna het hoogste boeknummer.
            _is_new = filtered["id"] < 0
            _fresh = filtered[_is_new].assign(_upd=pd.to_datetime(filtered[_is_new]["local_updated_at"], errors="coerce", utc=True)).sort_values(
                ["_upd", "id"], ascending=False, na_position="last").drop(columns="_upd")
            _rest = filtered[~_is_new].assign(_num=pd.to_numeric(filtered[~_is_new]["book_number"], errors="coerce").fillna(0)).sort_values(
                ["_num", "id"], ascending=False).drop(columns="_num")
            filtered = pd.concat([_fresh, _rest])

        if filtered.empty:
            st.warning(t('Geen boeken gevonden voor deze zoekterm.'))
        else:
            st.caption(
                t("{0} boeken, nieuwste bovenaan.", len(filtered)) if (has_extra and not search)
                else t("{0} resultaat/resultaten", len(filtered))
            )
            if F_BOLZERO in filters:
                # Loodslijst voor een ronde door de opslag: gesorteerd op locatie, zodat je de boeken in één keer kunt langslopen.
                _walk = filtered.assign(_loc=filtered["location"].fillna("").astype(str).str.strip())
                _walk = _walk.assign(_empty=_walk["_loc"].eq("")).sort_values(["_empty", "_loc", "title"])
                st.dataframe(
                    tdf(pd.DataFrame({
                        "Locatie": _walk["_loc"].replace("", "–"),
                        "Titel": _walk["title"],
                        "Auteur": _walk["author"].fillna(""),
                        "ISBN": [format_isbn(e) for e in _walk["ean"]],
                    })),
                    hide_index=True, width="stretch",
                )
            _MAX_OPTIONS = 500   # een keuzelijst met duizenden boeken wordt traag; verfijn dan de filters of de zoekterm
            _shown = filtered.head(_MAX_OPTIONS)
            if len(filtered) > _MAX_OPTIONS:
                st.caption(t('Alleen de eerste {0} staan in de lijst. Verfijn de filters of zoek op een term.', _MAX_OPTIONS))
            if current_id_pre := st.session_state.get("current_book_id"):
                # Het gekozen boek moet in de lijst blijven, ook als het buiten de eerste 500 valt.
                if current_id_pre in filtered["id"].values and current_id_pre not in _shown["id"].values:
                    _shown = pd.concat([filtered[filtered["id"] == current_id_pre], _shown])
            book_options = {
                f"{row['title']} — {row['author']} — ISBN {format_isbn(row['ean'])}"
                + (f" — locatie {row['location']}" if pd.notna(row.get("location")) and str(row.get("location")).strip() else ""): row["id"]
                for _, row in _shown.iterrows()
            }
            option_labels = list(book_options.keys())
            option_ids = list(book_options.values())
            current_id = st.session_state.get("current_book_id")
            default_index = option_ids.index(current_id) if current_id in option_ids else 0
            chosen_label = st.selectbox(
                t('Kies een boek uit de zoekresultaten'),
                option_labels,
                index=default_index,
                key="book_search_select",
            )
            st.session_state["current_book_id"] = book_options[chosen_label]
    else:
        st.caption(t('Typ om te zoeken in {0} boeken die aan de gekozen filters voldoen.', len(books)))

    chosen_id = st.session_state.get("current_book_id")

    if chosen_id is not None and chosen_id in books["id"].values:
        b = books[books["id"] == chosen_id].iloc[0]
        b = b.copy()
        b["long_description"] = get_long_description(int(chosen_id))   # load_books laat deze zware kolom weg

        st.markdown(f"## {b['title']}")  # titel van het boek: niet vertalen

        book_queued = bool(b["queued"]) if pd.notna(b["queued"]) else False
        book_push_enabled = bool(b["push_enabled"]) if pd.notna(b["push_enabled"]) else True
        if not book_push_enabled and not book_queued and chosen_id > 0:
            st.warning(
                t('⚠️ Synchronisatie met Boekwinkeltjes staat uit voor dit boek — vermoedelijk omdat het niet meer bij Boekwinkeltjes bestaat (bijv. daar handmatig verwijderd). Het boek wordt daardoor niet meer bijgewerkt.')
            )
            if st.button(t('Synchronisatie weer aanzetten'), key=f"reenable_push_{chosen_id}"):
                save_book_edits(
                    chosen_id,
                    {"push_enabled": True},
                    user_short_name=current_user_short_name(),
                    previous_values={"push_enabled": False},
                )
                load_books.clear()
                st.success(t('Synchronisatie weer aangezet. Wordt bij de eerstvolgende synchronisatie opnieuw geprobeerd.'))
                st.rerun()
        elif not book_push_enabled and not book_queued and chosen_id < 0:
            # Nieuw boek dat nog niet is vrijgegeven (bijv. omdat het nog bij de invoerder thuis ligt): alleen de beheerder geeft het vrij.
            st.info(t('⏳ Dit boek wacht op vrijgave door de beheerder. Het staat nog niet op de webshop, Boekwinkeltjes of Bol.'))
            if is_admin and st.button(t('✅ Vrijgeven voor synchronisatie'), key=f"release_push_{chosen_id}"):
                save_book_edits(
                    chosen_id,
                    {"push_enabled": True},
                    user_short_name=current_user_short_name(),
                    previous_values={"push_enabled": False},
                )
                load_books.clear()
                st.success(t('Vrijgegeven. Het boek wordt bij de eerstvolgende synchronisatie aangemaakt.'))
                st.rerun()

        # Per ongeluk dubbel opgeslagen? Een boek dat nog NIET bij Boekwinkeltjes staat kan hier worden verwijderd.
        _unplaced = chosen_id < 0 and bool(b.get("pending_create")) if pd.notna(b.get("pending_create")) else False
        if _unplaced:
            with st.expander(t('🗑️ Dit boek verwijderen (nog niet bij Boekwinkeltjes)')):
                st.caption(
                    t("Handig als je per ongeluk meerdere keren op opslaan hebt geklikt. Het boek en zijn foto's worden uit de app gehaald. Dit kan alleen zolang het boek nog op het druppelsysteem wacht.")
                )
                _sure = st.checkbox(t('Ja, verwijder dit boek'), key=f"del_unplaced_ok_{chosen_id}")
                if st.button(t('Verwijderen'), key=f"del_unplaced_btn_{chosen_id}", disabled=not _sure):
                    if delete_unplaced_book(chosen_id):
                        st.session_state.pop("current_book_id", None)
                        load_books.clear()
                        st.success(t('Het boek is verwijderd.'))
                        st.rerun()
                    else:
                        st.error(t('Niet verwijderd: het boek is ondertussen al bij Boekwinkeltjes geplaatst.'))

        images = get_all_images(chosen_id)
        main_image_url = b.get("main_image_url")

        # Een eigen voorkantkeuze (voor webshop en Bol) gaat voor op de hoofdfoto van Boekwinkeltjes: die foto is dan de voorkant.
        own_front_idx = next((n for n, im in enumerate(images) if im.get("is_front")), None)
        default_idx = own_front_idx if own_front_idx is not None else find_main_image_index(images, main_image_url)

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
                    t('<img src="{0}" style="height:320px; width:auto; max-width:100%; object-fit:contain;" />', big_url),
                    unsafe_allow_html=True,
                )
            else:
                st.caption(t('Geen afbeelding beschikbaar (of nog niet gesynchroniseerd).'))
        with info_col1:
            st.markdown(t('**Auteur:** {0}', na(b['author'])))
            st.markdown(t('**Uitgever:** {0}', na(b['publisher_name'])))
            st.markdown(t('**Adres:** {0}', na(b['publisher_address'])))
            st.markdown(t('**Contact:** {0}', na(b['publisher_contact'])))
            st.markdown(f"**{t(price_label)}:** €{format_price(b['price'])}")
            if pd.notna(b["shipping_cost_bol"]):
                st.markdown(t('**Prijs Bol:** €{0}', format_price(b['shipping_cost_bol'])))
            else:
                st.markdown(t('**Prijs Bol:** niet ingesteld in de app'))
            # Of er bij Bol daadwerkelijk een aanbieding bestaat, zoals de voorraadsync die het laatst zag.
            _bol_stock = get_bol_offer_stocks().get(str(b["ean"]).strip()) if pd.notna(b["ean"]) else None
            _has_isbn = pd.notna(b["ean"]) and str(b["ean"]).strip() not in ("", "0")
            _on_bol = "Nee, geen ISBN" if not _has_isbn else (f"Ja, voorraad Bol: {_bol_stock}" if _bol_stock is not None else "Nee")
            st.markdown(t('**Op Bol?** {0}', _on_bol))
            if show_logistics:
                st.markdown(t('**Verzendkosten webshop en BW:** €{0}', format_price(b['shipping_cost'])))
                st.markdown(t('**Verzendformaat:** {0}', shipping_format_label(b.get('shipping_format'))))
            st.markdown(t('**Voorraad:** {0}', na(b['amount'])))
        with info_col2:
            categories = [
                str(c).strip()
                for c in [b["category1"], b["category2"], b["category3"]]
                if pd.notna(c) and str(c).strip()
            ]
            st.markdown(t('**Categorie:** {0}', ' / '.join(category_label(c) for c in categories) if categories else '–'))
            st.markdown(t('**Taal:** {0}', na(b['language'])))
            st.markdown(t('**ISBN:** {0}', format_isbn(b['ean'])))
            _own_number = b.get("book_number")
            st.markdown(t('**Boeknummer:** {0}', int(_own_number) if pd.notna(_own_number) else '–'))
            st.markdown(t('**Boeknummer BW:** {0}', int(chosen_id)))
            try:
                _extra = get_book_bol_and_sales(int(chosen_id), str(b["ean"]).strip() if pd.notna(b["ean"]) else "")
            except Exception:
                _extra = None   # deze extra regels mogen de pagina nooit stoppen
            if _extra is not None:
                st.markdown(t('**Offer-ID Bol:** {0}', _extra['offer_id'] or '–'))
                st.markdown(t('**Conditie op Bol:** {0}', _extra['condition'] or '–'))
                _listed = pd.to_datetime(b.get("listing_date"), errors="coerce")
                st.markdown(t('**Toegevoegd op:** {0}', _listed.strftime('%d-%m-%Y') if pd.notna(_listed) else '–'))
                _sales = _extra["sales"]
                if _sales:
                    _parts = " / ".join(
                        f"{d or 'datum onbekend'}, {pl}" + (f", € {format_price(pr)}" if pr is not None else "")
                        for d, pl, pr in _sales)
                    st.markdown(t('**Verkoopgeschiedenis:** {0} keer verkocht ({1})', len(_sales), _parts))
                else:
                    st.markdown(t('**Verkoopgeschiedenis:** Nog niet verkocht'))
            if show_logistics:
                st.markdown(t('**Locatie:** {0}', na(b['location'])))

        st.markdown(t('<br>'), unsafe_allow_html=True)

        if len(images) > 1:
            st.markdown(
                t('\n                <style>\n                button[aria-label="🔍"], button[aria-label="🗑️"] {\n                    padding: 0.25rem !important;\n                    min-width: unset !important;\n                    width: auto !important;\n                    line-height: 1 !important;\n                }\n                </style>\n                '),
                unsafe_allow_html=True,
            )
            st.markdown(
                t("**Alle foto's** — klik op 🔍 voor een groter afbeelding en op 🗑️ om hem te verwijderen. De voorkant kies je bij het uploaden (onder 'Boek bewerken').")
            )
            n_web_extra = sum(1 for im in images[1:] if im.get("web_extra"))
            if n_web_extra:
                st.caption(
                    t("Op de webshop: de voorkant en {0} extra foto (hoogstens {1}).", n_web_extra, MAX_WEB_EXTRA)
                    if n_web_extra == 1 else
                    t("Op de webshop: de voorkant en {0} extra foto's (hoogstens {1}).", n_web_extra, MAX_WEB_EXTRA)
                )
            else:
                st.caption(t("Op de webshop: de eerste drie foto's. Wijs hieronder met 'Ook op webshop' zelf de (hoogstens twee) extra foto's aan."))
            _webextra_msg = st.session_state.pop(f"webextra_msg_{chosen_id}", None)
            if _webextra_msg:
                st.warning(_webextra_msg)
            thumbs_per_row = 5
            for row_start in range(0, len(images), thumbs_per_row):
                row_images = images[row_start : row_start + thumbs_per_row]
                thumb_cols = st.columns(thumbs_per_row)
                for offset, (col, img) in enumerate(zip(thumb_cols, row_images)):
                    i = row_start + offset
                    thumb_url = img["url_medium"] or img["url_large"] or img["url_small"]
                    with col:
                        st.markdown(
                            t('<img src="{0}" style="height:160px; width:auto; max-width:100%; object-fit:contain;" />', thumb_url),
                            unsafe_allow_html=True,
                        )
                        if img.get("is_placeholder"):
                            st.caption(t('⚠️ Lijkt een plaatshouder (geen echte omslag)'))
                        if img["source"] == "uploaded":
                            # Een eigen upload die nog niet door Boekwinkeltjes is verwerkt: de hoofdfoto is de eerste in
                            # de volgorde, dus die kies je door hem vooraan te zetten (zolang er nog niets van dit boek
                            # bij Boekwinkeltjes staat).
                            is_current_main = bool(img.get("is_main"))
                        else:
                            is_current_main = bool(main_image_url) and main_image_url in image_identity_urls(img)
                        if own_front_idx is not None:
                            is_current_main = i == own_front_idx
                        if is_current_main:
                            st.markdown(t("<div style='text-align:center;'>⭐ Voorkant</div>"), unsafe_allow_html=True)
                        if i == 0:
                            st.markdown(t("<div style='text-align:center;font-size:0.85em;'>op de webshop</div>"), unsafe_allow_html=True)
                        else:
                            on_web = bool(img.get("web_extra"))
                            if st.button(t("✔ Op webshop") if on_web else t("Ook op webshop"), key=f"webextra_btn_{chosen_id}_{i}",
                                         type="primary" if on_web else "secondary", use_container_width=True):
                                ok, msg = set_web_extra(chosen_id, img["source"], img["ref_id"], not on_web)
                                if not ok:
                                    st.session_state[f"webextra_msg_{chosen_id}"] = msg
                                st.rerun()
                        if img["source"] == "confirmed":
                            # Eigen voorkant voor de webshop en Bol, los van Boekwinkeltjes (daar ligt de eerste foto vast).
                            is_shop_front = bool(img.get("is_front"))
                            if st.button(t("✔ Voorkant webshop/Bol") if is_shop_front else t("Voorkant webshop/Bol"),
                                         key=f"front_btn_{chosen_id}_{i}", type="primary" if is_shop_front else "secondary",
                                         use_container_width=True,
                                         help=t('Deze foto als voorkant op de webshop en bij Bol. Nog eens klikken haalt de keuze weg; dan geldt weer de eerste foto. Bij Boekwinkeltjes verandert niets.')):
                                ok, msg = set_front_image(chosen_id, None if is_shop_front else img["ref_id"])
                                if not ok:
                                    st.session_state[f"webextra_msg_{chosen_id}"] = msg
                                else:
                                    st.session_state.pop(idx_key, None)   # de grote foto volgt de nieuwe voorkant
                                st.rerun()
                        b1, b2 = st.columns([1, 1])
                        with b1:
                            if st.button("🔍", key=f"thumb_btn_{chosen_id}_{i}"):
                                st.session_state[idx_key] = i
                                st.rerun()
                        with b2:
                            if st.button("🗑️", key=f"delete_img_btn_{chosen_id}_{i}"):
                                delete_book_image(chosen_id, img["source"], img["ref_id"])
                                st.session_state[idx_key] = 0
                                get_all_images.clear()
                                st.rerun()
        if b.get("weblink"):
            st.markdown(t('**Link:** [{0}]({1})', b['weblink'], b['weblink']))

        st.markdown(t('**Korte beschrijving**'))
        st.write(_s(b["short_description"]) or "—")
        st.markdown(t('**Lange beschrijving**'))
        st.write(_s(b["long_description"]) or "—")

        with st.expander(t('✏️ Boek bewerken')):
            st.caption(
                t("Pas aan wat je wilt en druk daarna op 'Opslaan' onderaan. Wijzigingen gaan naar Boekwinkeltjes bij de eerstvolgende sync (of klik op Home op '📚 Sync boeken nu').")
            )

            edit_prefix = f"edit_{chosen_id}"

            # ISBN, de Bol-prijs, en Categorie 1/2 staan buiten het formulier, omdat
            # ze direct moeten meebewegen met wat je hierboven kiest/typt (dat kan
            # een st.form niet, die update pas na op 'Opslaan' te klikken).
            isbn_col, bol_col = st.columns(2)
            with isbn_col:
                edit_ean_typed = st.text_input(t('ISBN'), value=_s(b["ean"]), key=f"{edit_prefix}_ean")
                # Een ISBN-10 wordt automatisch een ISBN-13 (ook bij het opslaan); alles hieronder gebruikt het ISBN-13.
                edit_ean = normalize_isbn(edit_ean_typed.strip()) if edit_ean_typed.strip() else edit_ean_typed
                if edit_ean_typed.strip() and edit_ean != edit_ean_typed.strip():
                    st.caption(t('ISBN-10 omgezet naar ISBN-13: {0}', edit_ean))
            isbn_present = len(re.sub(r"\D", "", edit_ean.strip())) >= 10
            with bol_col:
                if isbn_present:
                    current_bol_value = float(b["shipping_cost_bol"]) if pd.notna(b["shipping_cost_bol"]) else None
                    bol_unsuitable = st.checkbox(
                        t('Boek is ongeschikt voor Bol (bijv. verboden type)'),
                        value=False,
                        key=f"{edit_prefix}_bol_unsuitable",
                    )
                    if bol_unsuitable:
                        st.markdown(t('**Prijs Bol, incl. verzendkosten en commissie (€)**'))
                        st.write(t('Boek ongeschikt voor Bol'))
                        edit_shipping_cost_bol = None
                    else:
                        if current_bol_value is not None:
                            default_bol_value = current_bol_value
                        else:
                            base_price = float(b["price"]) if pd.notna(b["price"]) else 0.0
                            base_shipping = float(b["shipping_cost"]) if pd.notna(b["shipping_cost"]) else 0.0
                            default_bol_value = round(base_price + base_shipping + 2.25, 2)
                        edit_shipping_cost_bol = st.number_input(
                            t('Prijs Bol, incl. verzendkosten en commissie (€)'),
                            value=default_bol_value,
                            step=0.5,
                            key=f"{edit_prefix}_shipping_cost_bol",
                        )
                        if current_bol_value is None:
                            st.caption(t('(aanpasbare suggestie)'))
                else:
                    st.markdown(t('**Prijs Bol, incl. verzendkosten en commissie (€)**'))
                    st.write(t('Boek ongeschikt voor Bol'))
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
                        msg = t(
                            "Dit boek wordt momenteel {0} keer aangeboden op Boekwinkeltjes voor €{1} t/m €{2}",
                            active, format_price_dot(market_info.get('laagste_prijs')), format_price_dot(market_info.get('hoogste_prijs')),
                        )
                        if market_info.get("lastOrder"):
                            msg += t(", en is voor het laatst verkocht op {0} voor het bedrag van {1}", market_info['lastOrder'], market_info['lastPrice'])
                        bw_box_lines.append(msg + ".")
                    elif market_info.get("lastOrder"):
                        bw_box_lines.append(
                            t("Dit boek wordt momenteel niet aangeboden, maar is voor het laatst verkocht op {0} voor het bedrag van {1}.",
                              market_info['lastOrder'], market_info['lastPrice'])
                        )
                    else:
                        bw_box_lines.append(t("Dit boek wordt niet aangeboden op Boekwinkeltjes."))

                if bol_count is not None:
                    if bol_count > 0:
                        bw_box_lines.append(
                            t("Bij Bol wordt dit boek door {0} verkoper aangeboden voor €{1} t/m €{2}.", bol_count, format_price_dot(bol_laagste), format_price_dot(bol_hoogste))
                            if bol_count == 1 else
                            t("Bij Bol wordt dit boek door {0} verkopers aangeboden voor €{1} t/m €{2}.", bol_count, format_price_dot(bol_laagste), format_price_dot(bol_hoogste))
                        )
                    else:
                        bw_box_lines.append(t("Bij Bol wordt dit boek momenteel niet aangeboden."))

                # Nergens te koop (niet bij Boekwinkeltjes en niet bij Bol)? Dan als hulpmiddel de laagste prijs bij
                # Abebooks erbij, net als bij Nieuw boek.
                abebooks_result = None
                if market_info and (market_info.get("activeAmount") or 0) == 0 and bol_count == 0:
                    abebooks_result = lookup_abebooks_lowest_price(edit_ean)
                    bw_box_lines.append(abebooks_box_line(abebooks_result))

                if bw_box_lines:
                    info_box("<br>".join(bw_box_lines))

                if abebooks_result:
                    _abe_isbn = re.sub(r"\D", "", edit_ean or "")
                    st.link_button(
                        t('Zoek op Abebooks'),
                        f"https://www.abebooks.com/servlet/SearchResults?isbn={_abe_isbn}",
                    )

                if abebooks_result:
                    with st.expander(t('Technische details van de Abebooks-opzoeking')):
                        if abebooks_result["status"] == "price":
                            st.write(t('Bedrag gelezen uit het veld:'))
                            st.code(abebooks_result["source"] or "(onbekend)", language=None)
                            if abebooks_result.get("rate_note"):
                                st.write(t('Omrekening naar euro: {0}.', abebooks_result['rate_note']))
                        elif abebooks_result["message"]:
                            st.write(t('Melding: {0}.', abebooks_result['message']))
                        st.code(abebooks_result["raw"] or "(geen antwoord ontvangen)", language="json")

            # Snelkeuze: de 8 meest gebruikte categorieen (zelfde lijst als bij Nieuw boek); zet Categorie 1 en 2 hieronder in een klik.
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
            quick_key = f"{edit_prefix}_quick_category"

            def _apply_quick_category(by_label=quick_by_label, qkey=quick_key, prefix=edit_prefix):
                pair = by_label.get(st.session_state.get(qkey))
                if not pair:
                    return
                c1, c2 = pair
                st.session_state[f"{prefix}_category1"] = category1_label_for_value(c1)
                if c2 and c1 in CATEGORY2_OPTIONS:
                    st.session_state[f"{prefix}_category2_dd_{c1}"] = category2_label_for_value(c1, c2)

            if quick_by_label:
                st.radio(t('Snelkeuze categorie (de meest gebruikte)'), list(quick_by_label), index=None, horizontal=True,
                         key=quick_key, on_change=_apply_quick_category, format_func=category_option)

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
                    t('Categorie 1'), cat1_labels, index=cat1_index, key=f"{edit_prefix}_category1", format_func=category_option
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
                        t('Categorie 2'),
                        cat2_labels,
                        index=cat2_index,
                        key=f"{edit_prefix}_category2_dd_{edit_category1}",
                        format_func=category_option,
                    )
                    edit_category2 = cat2_values[cat2_labels.index(chosen_cat2_label)]
                else:
                    edit_category2 = st.text_input(
                        t('Categorie 2'), value=_s(b["category2"]), key=f"{edit_prefix}_category2_txt"
                    )

            with st.form(key=f"edit_book_form_{chosen_id}"):
                col_a, col_b = st.columns(2)
                with col_a:
                    edit_title = st.text_input(t('Titel'), value=_s(b["title"]), key=f"{edit_prefix}_title")
                    edit_author = st.text_input(t('Auteur'), value=_s(b["author"]), key=f"{edit_prefix}_author")

                    known_publishers = get_known_publishers()
                    current_publisher = _s(b["publisher"])
                    pub_options = list(known_publishers)
                    if current_publisher and current_publisher not in pub_options:
                        pub_options = [current_publisher] + pub_options
                    default_pub_index = pub_options.index(current_publisher) if current_publisher in pub_options else 0
                    chosen_publisher_option = st.selectbox(
                        t('Uitgever (kies uit de lijst)'),
                        pub_options,
                        index=default_pub_index,
                        key=f"{edit_prefix}_publisher_dd",
                    )
                    new_publisher_text = st.text_input(
                        t('Of vul hier een nieuwe uitgever in (laat leeg om de keuze hierboven te gebruiken; vorm: naam ; adres - plaats - NL ; contact)'),
                        value="",
                        key=f"{edit_prefix}_publisher_new",
                    )

                    edit_price = st.number_input(
                        f"{t(price_label)} (€)",
                        value=float(b["price"]) if pd.notna(b["price"]) else 0.0,
                        step=0.5,
                        key=f"{edit_prefix}_price",
                    )

                    if show_logistics:
                        # Het busstuk/pakket-advies is weggehaald: afmetingen waren te onbetrouwbaar.
                        shipping_briefpost, shipping_pakketpost = get_shipping_costs()

                    current_shipping_cost = float(b["shipping_cost"]) if pd.notna(b["shipping_cost"]) else 0.0
                    if show_logistics:
                        # De keuzelijst volgt de ingestelde bedragen (pagina 'Hulp en instellingen').
                        # Heeft dit boek een bedrag dat daar niet (meer) bij hoort — bijv. een oud
                        # tarief — dan staat de keuze op 'Vrije invoer' met het bestaande bedrag
                        # erin, zodat opslaan het niet ongemerkt verandert.
                        SHIPPING_BW_OPTIONS = shipping_options_labels(shipping_briefpost, shipping_pakketpost)
                        current_shipping_str = f"{current_shipping_cost:.2f}".replace(".", ",")
                        default_shipping_index = (
                            SHIPPING_BW_OPTIONS.index(current_shipping_str)
                            if current_shipping_str in SHIPPING_BW_OPTIONS
                            else 0
                        )
                        shipping_bw_key = f"{edit_prefix}_shipping_bw_choice"
                        if shipping_bw_key in st.session_state and st.session_state[shipping_bw_key] not in SHIPPING_BW_OPTIONS:
                            # Een eerdere keuze uit een lijst met andere bedragen (de instellingen zijn
                            # intussen gewijzigd): terug naar de standaard voor dit boek.
                            st.session_state[shipping_bw_key] = SHIPPING_BW_OPTIONS[default_shipping_index]
                        shipping_bw_choice = st.selectbox(
                            t('Verzendkosten webshop en BW (€)'),
                            SHIPPING_BW_OPTIONS,
                            index=default_shipping_index,
                            key=shipping_bw_key,
                        )
                        edit_shipping_cost_free = st.number_input(
                            t("Verzendkosten webshop en BW - vrij bedrag (alleen gebruikt als je hierboven 'Vrije invoer' kiest)"),
                            value=current_shipping_cost,
                            step=0.25,
                            key=f"{edit_prefix}_shipping_bw_free",
                        )

                    edit_amount = st.number_input(
                        t('Voorraad'),
                        value=int(b["amount"]) if pd.notna(b["amount"]) else 0,
                        step=1,
                        key=f"{edit_prefix}_amount",
                    )
                with col_b:
                    if show_logistics:
                        edit_location = st.text_input(
                            t('Locatie'),
                            value=_s(b["location"]),
                            key=f"{edit_prefix}_location",
                        )
                    else:
                        # Niet zichtbaar in deze weergave; de bestaande waarde blijft gewoon staan.
                        edit_location = _s(b["location"])

                    edit_category3 = st.text_input(
                        t('Categorie 3'), value=_s(b["category3"]), key=f"{edit_prefix}_category3"
                    )
                    edit_language = st.text_input(t('Taal'), value=_s(b["language"]), key=f"{edit_prefix}_language")
                    if is_english():
                        _short_key = f"{edit_prefix}_short"
                        if _short_key not in st.session_state:
                            st.session_state[_short_key] = _s(b["short_description"])
                        _bijz_state = bijz_input(_short_key, new=False, long_key=f"{edit_prefix}_long")
                        edit_short_desc = None   # wordt bij het opslaan samengesteld
                    else:
                        _bijz_state = None
                        edit_short_desc = st.text_area(
                            t("Bijzonderheden (jaar, pagina's, vorm, staat)"),
                            value=_s(b["short_description"]),
                            height=100,
                            key=f"{edit_prefix}_short",
                        )
                    edit_long_desc = st.text_area(
                        long_label(_bijz_state) if _bijz_state is not None else t('Meer info (in Nederlands)'),
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
                        t('Wachtrij?'),
                        options=["Nee", "Ja"],
                        index=0,  # staat het boek nog in de wachtrij, dan is 'Nee' verreweg het vaakst gewenst
                        key=f"{edit_prefix}_queued",
                        help=t("'Ja' = dit boek wacht op handmatige controle en wordt niet gesynchroniseerd. Zet je dit op 'Nee', dan gaat synchronisatie automatisch aan."),
                    )

                st.markdown(t('**📷 Afbeeldingen toevoegen**'))
                front_allowed = front_upload_allowed(images)
                new_front_file = st.file_uploader(
                    t('Voorkant (1 afbeelding, voor alle platformen)'),
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=False,
                    key=f"{edit_prefix}_front_upload",
                    disabled=not front_allowed,
                )
                if not front_allowed:
                    st.caption(
                        t("Er staan al foto's van dit boek bij Boekwinkeltjes. De eerste foto daar is de hoofdafbeelding en de app kan die niet meer wijzigen, dus een nieuwe voorkant kan hier niet. Nieuwe foto's kun je wel toevoegen bij 'Overige foto's'.")
                    )
                new_web_files = st.file_uploader(
                    t("Extra foto's (maximaal 2, de beste, voor alle platformen)"),
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=True,
                    key=f"{edit_prefix}_web_upload",
                )
                new_uploaded_files = st.file_uploader(
                    t("Overige foto's (onbeperkt aantal, de rest, niet voor webshop)"),
                    type=["jpg", "jpeg", "png"],
                    accept_multiple_files=True,
                    key=f"{edit_prefix}_images_upload",
                )

                submitted = st.form_submit_button(t('Opslaan'), type="primary")

            if submitted and int(edit_amount) > 0 and not (edit_category1 or "").strip():
                st.error(t('Categorie 1 is verplicht voor een voorradig boek: zonder categorie is het boek in de webshop niet te vinden via de categorieën.'))
                submitted = False

            if submitted and not (edit_price and float(edit_price) > 0):
                st.error(t('De prijs moet groter zijn dan €0: Boekwinkeltjes weigert een boek zonder prijs. Er is niets opgeslagen.'))
                submitted = False

            # Minimumprijs: een prijs onder €3,95 mag niet worden opgeslagen. Staat een bestaand boek al onder die prijs
            # en blijft de prijs gelijk, dan kun je gewoon andere velden (zoals de locatie) blijven aanpassen.
            _oude_prijs = float(b["price"]) if pd.notna(b["price"]) else None
            if submitted and float(edit_price) < MIN_PRIJS and (_oude_prijs is None or round(float(edit_price), 2) != round(_oude_prijs, 2)):
                st.error(t("De prijs is te laag: het minimum is €{0}. Er is niets opgeslagen.", f"{MIN_PRIJS:.2f}".replace(".", ",")))
                submitted = False

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
                if _bijz_state is not None:
                    edit_short_desc = finalize_bijz(_bijz_state)
                    edit_long_desc = finalize_long(_bijz_state, edit_long_desc)
                edit_push_enabled = current_push_enabled
                if current_queued and not edit_queued and is_admin:   # alleen de beheerder geeft een boek vrij
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

                if new_front_file is not None and front_allowed:
                    if not save_front_image(
                        chosen_id,
                        {"data": new_front_file.getvalue(), "content_type": new_front_file.type or "image/jpeg"},
                    ):
                        st.warning(t("De voorkant is niet opgeslagen: er staan inmiddels foto's van dit boek bij Boekwinkeltjes."))

                already_extra = sum(1 for im in images[1:] if im.get("web_extra"))
                room = max(0, MAX_WEB_EXTRA - already_extra)
                web_files = list(new_web_files or [])
                if web_files and len(web_files) > room:
                    st.warning(
                        t("Er is nog ruimte voor {0} extra foto('s) op de webshop; de overige {1} zijn als gewone foto toegevoegd.", room, len(web_files) - room)
                    )
                images_to_save = [
                    {"data": f.getvalue(), "content_type": f.type or "image/jpeg", "is_main": False, "web_extra": n < room}
                    for n, f in enumerate(web_files)
                ] + [
                    {"data": f.getvalue(), "content_type": f.type or "image/jpeg", "is_main": False}
                    for f in (new_uploaded_files or [])
                ]
                if images_to_save:
                    save_uploaded_images(chosen_id, images_to_save)

                load_books.clear()
                get_all_images.clear()
                # Het boek blijft zichtbaar: wisselt de voorraad tussen 0 en meer dan 0, dan wisselt de voorraadknop mee.
                _f = list(filters)
                if int(edit_amount) <= 0 and F_STOCK in _f:
                    _f = [x for x in _f if x != F_STOCK] + ([] if F_SOLD in _f else [F_SOLD])
                    st.session_state["pending_view_filters"] = _f
                elif int(edit_amount) > 0 and F_SOLD in _f:
                    _f = [x for x in _f if x != F_SOLD] + ([] if F_STOCK in _f else [F_STOCK])
                    st.session_state["pending_view_filters"] = _f
                _online = not edit_queued and edit_push_enabled
                _lines = [t('✅ "{0}" is aangepast en opgeslagen (id {1})', edit_title, chosen_id) + (t(" en komt online:") if _online else ":")]
                if edit_queued:
                    _lines.append("- " + t("Het boek staat in de wachtrij en gaat nergens naartoe totdat je dat op 'Nee' zet."))
                elif not edit_push_enabled:
                    _lines.append("- " + t("Synchronisatie staat uit ('Geen push'): het boek gaat niet naar Boekwinkeltjes, de webshop of Bol."))
                else:
                    if int(edit_amount) <= 0:
                        _lines.append("- " + t("Niet op de webshop want er is geen voorraad."))
                    else:
                        _lines.append("- " + t("Op de webshop: prijs en voorraad bij de eerstvolgende catalogus-run, de rest aan het einde van de komende nacht."))
                    _both = chosen_id >= 0 and isbn_present and not bol_unsuitable
                    if _both:
                        _lines.append("- " + t("Bij Bol en Boekwinkeltjes na de eerstvolgende synchronisatie."))
                    elif chosen_id < 0:
                        _lines.append("- " + t("Bij Boekwinkeltjes gedurende de eerstvolgende druppeltijd."))
                    else:
                        _lines.append("- " + t("Bij Boekwinkeltjes na de eerstvolgende synchronisatie."))
                    if _both:
                        pass
                    elif not isbn_present:
                        _lines.append("- " + t("Niet bij Bol want er is geen ISBN."))
                    elif bol_unsuitable:
                        _lines.append("- " + t("Niet bij Bol want het boek is daarvoor ongeschikt."))
                    else:
                        _lines.append("- " + t("Bij Bol na de eerstvolgende synchronisatie."))
                st.session_state["book_saved_msg"] = "\n".join(_lines)
                st.rerun()

        st.divider()

        current_push_status = "Push" if (pd.isna(b["push_enabled"]) or b["push_enabled"]) else "Geen push"
        st.caption(t('Synchronisatiestatus: **{0}**', current_push_status))
        st.caption(t('Laatst gesynchroniseerd met Boekwinkeltjes: {0}', format_datetime_nl(b['last_synced_at'])))

        activity_rows = get_book_activity(chosen_id)
        if activity_rows:
            action_labels = {"created": t("aangemaakt"), "edited": t("aangepast")}
            for row in activity_rows:
                if row["action"] == "stock_change":
                    status_label = (row.get("changed_fields") or "").split("|")[0]
                    st.caption(
                        t("{0} — op '{1}' gezet — {2}", row['user_short_name'], status_label, format_datetime_nl(row['occurred_at']))
                    )
                    continue
                action_label = action_labels.get(row["action"], row["action"])
                changed = row.get("changed_fields")
                if changed:
                    field_list = changed.split("|")
                    if len(field_list) > 3:
                        shown = field_list[:3]
                        extra = len(field_list) - 3
                        fields_str = (t("{0}, en nog {1} veld", ", ".join(t(x) for x in shown), extra) if extra == 1
                                      else t("{0}, en nog {1} velden", ", ".join(t(x) for x in shown), extra))
                    else:
                        fields_str = ", ".join(t(x) for x in field_list)
                    action_label = f"{action_label} ({fields_str})"
                st.caption(
                    t("{0} — {1} — {2}", row['user_short_name'], action_label, format_datetime_nl(row['occurred_at']))
                )


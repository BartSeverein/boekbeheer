"""
pages/7_Orderdetails.py — Aparte pagina om één order in detail te bekijken, met zoekfunctie.
"""

import pandas as pd
import psycopg2
import streamlit as st
from pathlib import Path

from common import get_db_url, load_books, load_orders, get_all_image_urls, format_price, format_order_status, format_payment_status, format_datetime_nl, format_order_datetime, format_isbn, render_logo, require_login, na


def _s(value):
    """Zet NaN/None om naar een lege string; anders de waarde zelf."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value


st.set_page_config(page_title="Orderdetails", page_icon="🛒", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🛒 Orderdetails")

_PLATFORM_NAMES = {"Webshop": "Webshop", "Bol": "Bol", "BW": "Boekwinkeltjes", "": "Boekwinkeltjes"}


def _set_webshop_status(lo, hi, new):
    """Zet de status in de database en onthoudt hem in de sessie, zodat de pagina direct klopt zonder alles opnieuw te laden."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = %(s)s WHERE platform = 'Webshop' AND id BETWEEN %(lo)s AND %(hi)s",
                        {"s": new, "lo": lo, "hi": hi})
        conn.commit()
    finally:
        conn.close()
    st.session_state.setdefault("status_override", {})[(lo, hi)] = new


books, orders = load_books(), load_orders()
for (_lo_, _hi_), _new_ in st.session_state.get("status_override", {}).items():
    orders.loc[(orders["platform"] == "Webshop") & orders["id"].between(_lo_, _hi_), "status"] = _new_

preselect_id = st.session_state.pop("preselect_order_id", None)

if orders.empty:
    st.info("Nog geen orders om te tonen.")
else:
    default_search = ""
    if preselect_id is not None:
        match = orders[orders["id"] == preselect_id]
        if not match.empty:
            default_search = match.iloc[0]["book_title"] or ""

    search = st.text_input(
        "🔎 Zoek op boektitel, ISBN, koper of stad", default_search, key="order_search_input"
    )

    if search:
        s = search.strip().lower()
        mask = (
            orders["book_title"].str.lower().str.contains(s, na=False, regex=False)
            | orders["book_ean"].fillna("").astype(str).str.lower().str.contains(s, na=False, regex=False)
            | orders["buyer_name"].fillna("").str.lower().str.contains(s, na=False, regex=False)
            | orders["buyer_last_name"].fillna("").str.lower().str.contains(s, na=False, regex=False)
            | orders["buyer_city"].fillna("").str.lower().str.contains(s, na=False, regex=False)
        )
        filtered = orders[mask]
    else:
        filtered = orders  # orders zijn doorgaans veel minder dan boeken, dus dit is prima om standaard te tonen

    # Meest recente order bovenaan
    filtered = filtered.sort_values("order_date", ascending=False, na_position="last")

    if search and filtered.empty:
        st.warning("Geen orders gevonden voor deze zoekterm.")
    else:
        st.caption(f"{len(filtered)} resultaat/resultaten")
        order_options = {
            f"{format_order_datetime(row['order_date'])} — {row['book_title']} — {_s(row['buyer_city']) or 'onbekende plaats'}": row["id"]
            for _, row in filtered.iterrows()
        }
        option_labels = list(order_options.keys())
        default_index = 0
        if preselect_id is not None:
            for i, _id in enumerate(order_options.values()):
                if _id == preselect_id:
                    default_index = i
                    break
        chosen_label = st.selectbox(
            "Kies een order", option_labels, index=default_index, key="order_search_select"
        )
        chosen_id = order_options[chosen_label]
        o = orders[orders["id"] == chosen_id].iloc[0]

        st.markdown(f"## {o['book_title']}")

        image_urls = get_all_image_urls(o["book_id"]) if pd.notna(o["book_id"]) else []

        image_col, info_col1, info_col2 = st.columns([1, 1, 1])
        with image_col:
            if image_urls:
                st.markdown(
                    f'<img src="{image_urls[0]}" style="height:320px; width:auto; max-width:100%; object-fit:contain;" />',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("Geen afbeelding beschikbaar (boek mogelijk verkocht/verwijderd vóór sync, of nog niet gesynchroniseerd).")
        with info_col1:
            # Bol-orders bevatten geen auteur: dan halen we die uit het boek zelf (via boek-id, anders via ISBN).
            _author = o["book_author"]
            if (_author is None or pd.isna(_author) or not str(_author).strip()) and "author" in books.columns:
                _m = books[books["id"] == o["book_id"]] if pd.notna(o["book_id"]) else books.iloc[0:0]
                if _m.empty and pd.notna(o["book_ean"]) and str(o["book_ean"]).strip():
                    _m = books[books["ean"].astype(str) == str(o["book_ean"]).strip()]
                if not _m.empty and pd.notna(_m.iloc[0]["author"]):
                    _author = _m.iloc[0]["author"]
            st.markdown(f"**Auteur:** {na(_author)}")
            st.markdown(f"**ISBN:** {format_isbn(o['book_ean'])}")
            _loc = books.loc[books["id"] == o["book_id"], "location"] if pd.notna(o["book_id"]) and "location" in books.columns else []
            _loc = _s(_loc.iloc[0]) if len(_loc) else ""
            st.markdown(f"**Locatie:** {_loc or '–'}")
            st.markdown(f"**Prijs boek:** €{format_price(o['book_price'])}")
            st.markdown(f"**Verzendkosten:** €{format_price(o['book_shipping_cost'])}")
            st.markdown(f"**Totaal (omzet):** €{format_price(o['revenue'])}")
        with info_col2:
            st.markdown(f"**Status:** {format_order_status(o['status'])}")
            st.markdown(f"**Waar:** {_PLATFORM_NAMES.get(_s(o.get('platform')), 'Boekwinkeltjes')}")
            st.markdown(f"**Betaalstatus:** {format_payment_status(o['online_payment_status'])}")
            st.markdown(f"**Datum:** {format_order_datetime(o['order_date'])}")
            st.markdown(f"**Notitie:** {_s(o['note']) or '–'}")

        # Webshop-bestellingen krijgen hun status nergens anders: hier zet je ze op verstuurd (of weer terug).
        if _s(o.get("platform")) == "Webshop":
            _shop_nr = (-int(o["id"]) - 3_000_000_000) // 1000
            _lo, _hi = -(3_000_000_000 + _shop_nr * 1000 + 999), -(3_000_000_000 + _shop_nr * 1000)
            _shipped = o["status"] == "shipped_paid"
            _label = "↩️ Zet terug op 'Wachten op verzending'" if _shipped else "📦 Markeer als verstuurd"
            st.write("")
            st.button(_label, key=f"ship_{o['id']}", on_click=_set_webshop_status,
                      args=(_lo, _hi, "wait_for_shipping" if _shipped else "shipped_paid"))

        st.markdown("**Koper:**")
        buyer_name =_s(o["buyer_name"]) or f"{_s(o['buyer_first_name'])} {_s(o['buyer_last_name'])}".strip()
        st.write(buyer_name or "—")
        st.write(_s(o["buyer_email"]) or "—")
        st.write(_s(o["buyer_phone"]) or "—")

        st.markdown("**Adres:**")
        address_line = f"{_s(o['buyer_street'])} {_s(o['buyer_number'])}{_s(o['buyer_number_extra'])}".strip()
        st.write(address_line or "—")
        st.write(f"{_s(o['buyer_zip_code'])} {_s(o['buyer_city'])}".strip() or "—")
        st.write(_s(o["buyer_country"]) or "—")

        st.caption(f"Laatst gesynchroniseerd met Boekwinkeltjes: {format_datetime_nl(o['last_synced_at'])}")

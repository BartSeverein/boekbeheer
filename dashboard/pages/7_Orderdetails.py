"""
pages/7_Orderdetails.py — Aparte pagina om één order in detail te bekijken, met zoekfunctie.
"""

import pandas as pd
import streamlit as st
from pathlib import Path

from common import load_books, load_orders, get_all_image_urls, format_price, format_order_status, format_payment_status, format_datetime_nl, format_order_datetime, format_isbn, render_logo, require_login, na


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

books, orders = load_books(), load_orders()

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
            orders["book_title"].str.lower().str.contains(s, na=False)
            | orders["book_ean"].fillna("").astype(str).str.lower().str.contains(s, na=False)
            | orders["buyer_name"].fillna("").str.lower().str.contains(s, na=False)
            | orders["buyer_last_name"].fillna("").str.lower().str.contains(s, na=False)
            | orders["buyer_city"].fillna("").str.lower().str.contains(s, na=False)
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
            st.markdown(f"**Auteur:** {na(o['book_author'])}")
            st.markdown(f"**ISBN:** {format_isbn(o['book_ean'])}")
            st.markdown(f"**Prijs boek:** €{format_price(o['book_price'])}")
            st.markdown(f"**Verzendkosten:** €{format_price(o['book_shipping_cost'])}")
            st.markdown(f"**Totaal (omzet):** €{format_price(o['revenue'])}")
        with info_col2:
            st.markdown(f"**Status:** {format_order_status(o['status'])}")
            st.markdown(f"**Betaalstatus:** {format_payment_status(o['online_payment_status'])}")
            st.markdown(f"**Datum:** {format_order_datetime(o['order_date'])}")
            st.markdown(f"**Notitie:** {_s(o['note']) or '–'}")

        st.markdown("**Koper:**")
        buyer_name = _s(o["buyer_name"]) or f"{_s(o['buyer_first_name'])} {_s(o['buyer_last_name'])}".strip()
        st.write(buyer_name or "—")
        st.write(_s(o["buyer_email"]) or "—")
        st.write(_s(o["buyer_phone"]) or "—")

        st.markdown("**Adres:**")
        address_line = f"{_s(o['buyer_street'])} {_s(o['buyer_number'])}{_s(o['buyer_number_extra'])}".strip()
        st.write(address_line or "—")
        st.write(f"{_s(o['buyer_zip_code'])} {_s(o['buyer_city'])}".strip() or "—")
        st.write(_s(o["buyer_country"]) or "—")

        st.caption(f"Laatst gesynchroniseerd met Boekwinkeltjes: {format_datetime_nl(o['last_synced_at'])}")

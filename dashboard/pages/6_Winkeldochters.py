"""
pages/6_Winkeldochters.py — Overzicht van traag verkopende boeken ("winkeldochters"):
de 100 boeken die het langst onafgebroken in voorraad staan, met actuele
marktinfo van Boekwinkeltjes (BW) erbij.

'Dagen op voorraad' is vandaag min 'Te koop sinds' (listing_date, zoals
Boekwinkeltjes dat zelf bijhoudt — dat veld staat als tekst opgeslagen en
wordt hier geparsed). Let op: dat veld kan in theorie meebewegen als het boek
zelf ooit is bewerkt, dat is hoe Boekwinkeltjes het veld zelf beheert, geen
keuze van deze app.

Dit doet per boek een live opzoeking bij Boekwinkeltjes en Bol (voor de laagste
prijs/laatste verkoop/aantal aanbieders) — die opzoekingen gebeuren gelijktijdig
voor meerdere boeken tegelijk, maar het laden kan bij 100 boeken nog steeds
enkele tientallen seconden duren.
"""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import streamlit as st

from common import render_logo, require_login, get_slow_movers, format_isbn, format_price, format_price_dot, lookup_boekwinkeltjes_market_info, lookup_bol_competing_offers

st.set_page_config(page_title="Winkeldochters", page_icon="🕸", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🕸 Winkeldochters")

st.caption(
    "De 100 boeken die het langst onafgebroken in voorraad staan. De rechtse "
    "kolommen tonen actuele gegevens van andere verkopers op Boekwinkeltjes "
    "(BW) en Bol: de laagste prijs en hoeveel er op dat platform te koop staan."
)
st.caption(
    "Gebruik die informatie om eventueel onze prijs aan te passen. Klik op "
    "het vierkantje in de kantlijn om een boek te openen in Boekdetails."
)

st.warning("Dit doet meerdere opzoekingen per boek bij Boekwinkeltjes en Bol en kan enige tijd duren.")

books = get_slow_movers(limit=100)


def _fetch_market_data(book):
    """Haalt voor één boek de Boekwinkeltjes- en Bol-marktinfo op (gebruikt door de
    parallelle pool hieronder — deze twee opzoekingen hebben niks met elkaar te
    maken, maar worden hier bewust niet óók nog intern parallel gedaan: de winst
    zit 'm al in het gelijktijdig verwerken van veel boeken tegelijk)."""
    isbn = book.get("ean")
    laagste_prijs = last_order = active_amount = bol_count = bol_laagste = None
    if isbn and str(isbn).strip() and str(isbn).strip() != "0":
        market_info, _ = lookup_boekwinkeltjes_market_info(isbn)
        if market_info:
            laagste_prijs = market_info.get("laagste_prijs")
            last_order = market_info.get("lastOrder")
            active_amount = market_info.get("activeAmount")
        bol_count, bol_laagste, _ = lookup_bol_competing_offers(isbn)
    return laagste_prijs, last_order, active_amount, bol_count, bol_laagste


if not books:
    st.info("Geen boeken gevonden.")
else:
    today = dt.date.today()
    progress = st.progress(0, text="Marktinfo ophalen...")
    results = [None] * len(books)
    completed = 0

    # Tien boeken tegelijk verwerken in plaats van één voor één na elkaar — dat
    # scheelt bij 100 boeken enkele minuten laadtijd. Niet te hoog, om Boekwinkeltjes
    # en Bol niet onnodig te belasten.
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_index = {executor.submit(_fetch_market_data, book): i for i, book in enumerate(books)}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            completed += 1
            progress.progress(completed / len(books), text=f"{completed}/{len(books)} boeken verwerkt")
    progress.empty()

    rows = []
    for book, (laagste_prijs, last_order, active_amount, bol_count, bol_laagste) in zip(books, results):
        listing_day = None
        listing_date_raw = book.get("listing_date")
        if listing_date_raw:
            parsed = pd.to_datetime(listing_date_raw, errors="coerce")
            if pd.notna(parsed):
                listing_day = parsed.date()
        days_in_stock = (today - listing_day).days if listing_day else None
        isbn = book.get("ean")

        rows.append(
            {
                "id": book["id"],
                "ISBN": format_isbn(isbn) if isbn else "Geen",
                "Titel": book.get("title") or "(geen titel)",
                "Te koop sinds": listing_day.strftime("%d-%m-%Y") if listing_day else "–",
                "Dagen": days_in_stock if days_in_stock is not None else "–",
                "Prijs BW": f"€{format_price(book.get('price'))}",
                "Laatst verkocht op BW": last_order or "–",
                "Laagste prijs BW": f"€{format_price_dot(laagste_prijs)}" if laagste_prijs else "–",
                "Aantal BW": active_amount if active_amount is not None else "–",
                "Laagste prijs Bol": f"€{format_price_dot(bol_laagste)}" if bol_laagste else "–",
                "Aantal Bol": bol_count if bol_count is not None else "–",
            }
        )

    df = pd.DataFrame(rows)
    event = st.dataframe(
        df.drop(columns=["id"]),
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="winkeldochters_table",
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_book_id = int(df.iloc[selected_rows[0]]["id"])
        st.session_state["preselect_book_id"] = selected_book_id
        del st.session_state["winkeldochters_table"]
        st.switch_page("pages/3_Boekdetails.py")

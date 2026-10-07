"""
pages/12_Backup_bekijken.py — Een back-up (zip uit Dropbox, of losse CSV-bestanden) inzien en vergelijken met de
huidige database. De bestanden worden alleen in het geheugen gelezen en nergens opgeslagen.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from backup_view import (
    BackupReadError,
    COMPARE_BOOK_FIELDS,
    add_titles,
    compare_books,
    compare_orders,
    read_backup,
    search_frame,
)
from common import (
    FIELD_LABELS,
    format_order_status,
    format_payment_status,
    load_books,
    load_orders,
    render_logo,
    require_login,
    shipping_format_label,
)

st.set_page_config(page_title="Back-up bekijken", page_icon="💾", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("💾 Back-up bekijken")
st.caption(
    "Open de dagelijkse back-up (de zip uit Dropbox, of de losse CSV-bestanden) en zoek erin, of vergelijk hem met "
    "de huidige database. De bestanden worden alleen hier gelezen en nergens opgeslagen. Let op: de bestellingen "
    "bevatten gegevens van kopers."
)

ORDER_LABELS = {
    "id": "Ordernummer", "order_date": "Datum", "status": "Status", "online_payment_status": "Betaling",
    "platform": "Platform", "book_id": "Boeknummer", "book_title": "Titel", "book_author": "Auteur",
    "book_price": "Prijs", "book_shipping_cost": "Verzendkosten", "book_ean": "ISBN", "buyer_name": "Koper",
    "buyer_email": "E-mail", "buyer_phone": "Telefoon", "buyer_street": "Straat", "buyer_number": "Nummer",
    "buyer_zip_code": "Postcode", "buyer_city": "Plaats", "buyer_country": "Land", "buyer_company": "Bedrijf",
    "note": "Opmerking",
}
BOOK_LABELS = {"id": "Boeknummer", "book_number": "Intern nummer", "queued": "Wachtrij",
               "pending_push": "Wacht op verzenden", "pending_create": "Nog aan te maken", **FIELD_LABELS}
BOOK_DEFAULT_COLUMNS = ["id", "title", "author", "location", "amount", "price", "shipping_cost", "shipping_format",
                        "ean"]
ORDER_DEFAULT_COLUMNS = ["id", "order_date", "status", "online_payment_status", "platform", "book_title", "buyer_name",
                         "book_price", "book_shipping_cost"]

uploaded = st.file_uploader(
    "Back-up kiezen (zip, of de CSV-bestanden van boeken en bestellingen)",
    type=["zip", "csv"],
    accept_multiple_files=True,
)
if not uploaded:
    st.info("Kies hierboven een back-upbestand. Je vindt ze in Dropbox, onder Apps, in de map 'back-ups'.")
    st.stop()

try:
    backup_books, backup_orders, info = read_backup([(f.name, f.getvalue()) for f in uploaded])
except BackupReadError as e:
    st.error(str(e))
    st.stop()

date_text = f" van {info['date']}" if info.get("date") else ""
st.success(
    f"Back-up{date_text} gelezen: "
    f"{0 if backup_books is None else len(backup_books)} boeken, "
    f"{0 if backup_orders is None else len(backup_orders)} bestellingen."
)


def _pretty_books(frame):
    out = frame.copy()
    if "shipping_format" in out.columns:
        out["shipping_format"] = [shipping_format_label(None if pd.isna(v) else int(v)) for v in out["shipping_format"]]
    return out


tab_books, tab_orders, tab_compare = st.tabs(["Boeken", "Bestellingen", "Vergelijken met nu"])

with tab_books:
    if backup_books is None:
        st.info("Geen boekenbestand in deze back-up.")
    else:
        col1, col2 = st.columns([3, 1])
        query = col1.text_input("Zoeken (titel, auteur, ISBN, locatie of boeknummer)", key="bk_q")
        stock = col2.selectbox("Voorraad", ["Alle", "Op voorraad", "Uitverkocht"], key="bk_stock")
        shown = backup_books
        if stock == "Op voorraad":
            shown = shown[shown["amount"].fillna(0) > 0]
        elif stock == "Uitverkocht":
            shown = shown[shown["amount"].fillna(0) <= 0]
        shown = search_frame(shown, query, ["id", "title", "author", "ean", "location"])
        columns = st.multiselect(
            "Kolommen", list(backup_books.columns),
            default=[c for c in BOOK_DEFAULT_COLUMNS if c in backup_books.columns],
            format_func=lambda c: BOOK_LABELS.get(c, c), key="bk_cols",
        )
        st.caption(f"{len(shown)} van {len(backup_books)} boeken")
        table = _pretty_books(shown)[columns].rename(columns=BOOK_LABELS)
        st.dataframe(table, hide_index=True, width="stretch")
        pick = st.number_input("Alle gegevens van boeknummer", min_value=0, step=1, value=0, key="bk_pick")
        if pick:
            row = backup_books[backup_books["id"] == pick]
            if row.empty:
                st.warning(f"Boek {pick} staat niet in deze back-up.")
            else:
                detail = _pretty_books(row).iloc[0]
                st.dataframe(
                    pd.DataFrame({"Veld": [BOOK_LABELS.get(c, c) for c in detail.index],
                                  "Waarde": ["" if pd.isna(v) else str(v) for v in detail.values]}),
                    hide_index=True, width="stretch",
                )

with tab_orders:
    if backup_orders is None:
        st.info("Geen bestellingenbestand in deze back-up.")
    else:
        col1, col2 = st.columns([3, 1])
        query = col1.text_input("Zoeken (koper, titel, e-mail, plaats of ordernummer)", key="or_q")
        statuses = sorted(backup_orders["status"].dropna().unique()) if "status" in backup_orders.columns else []
        chosen = col2.multiselect("Status", statuses, format_func=format_order_status, key="or_status")
        shown = backup_orders
        if chosen:
            shown = shown[shown["status"].isin(chosen)]
        shown = search_frame(shown, query, ["id", "buyer_name", "book_title", "buyer_email", "buyer_city", "book_ean"])
        columns = st.multiselect(
            "Kolommen", list(backup_orders.columns),
            default=[c for c in ORDER_DEFAULT_COLUMNS if c in backup_orders.columns],
            format_func=lambda c: ORDER_LABELS.get(c, c), key="or_cols",
        )
        st.caption(f"{len(shown)} van {len(backup_orders)} bestellingen")
        table = shown[columns].copy()
        if "status" in table.columns:
            table["status"] = table["status"].map(format_order_status)
        if "online_payment_status" in table.columns:
            table["online_payment_status"] = table["online_payment_status"].map(format_payment_status)
        st.dataframe(table.rename(columns=ORDER_LABELS), hide_index=True, width="stretch")

with tab_compare:
    st.write(
        "Legt de back-up naast wat er nu in de database staat. Zo zie je wat er sinds de back-up is veranderd, "
        "bijvoorbeeld na een herstel bij Boekwinkeltjes."
    )
    if st.button("Huidige database opnieuw laden"):
        load_books.clear()
        load_orders.clear()
    if backup_books is None:
        st.info("Geen boekenbestand in deze back-up.")
    else:
        now_books = load_books()
        only_backup, only_now, diffs = compare_books(backup_books, now_books)
        diffs = add_titles(diffs, backup_books, now_books)
        changed_books = diffs["id"].nunique() if not diffs.empty else 0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Boeken in back-up", len(backup_books))
        m2.metric("Boeken nu", len(now_books[now_books["id"] > 0]))
        m3.metric("Alleen in back-up", len(only_backup))
        m4.metric("Alleen nu", len(only_now))
        st.metric("Boeken met een verschil", changed_books)
        if diffs.empty:
            st.success("Geen verschillen in de vergeleken velden.")
        else:
            counts = diffs.groupby("veld")["id"].nunique().rename("Boeken").reset_index()
            counts["Veld"] = counts["veld"].map(lambda f: BOOK_LABELS.get(f, f))
            st.dataframe(counts[["Veld", "Boeken"]].sort_values("Boeken", ascending=False), hide_index=True)
            field_choice = st.multiselect(
                "Alleen deze velden tonen", COMPARE_BOOK_FIELDS, format_func=lambda f: BOOK_LABELS.get(f, f),
                key="cmp_fields",
            )
            shown = diffs[diffs["veld"].isin(field_choice)] if field_choice else diffs
            shown = shown.assign(veld=shown["veld"].map(lambda f: BOOK_LABELS.get(f, f)))
            shown = shown.rename(columns={"id": "Boeknummer", "titel": "Titel", "veld": "Veld",
                                          "back-up": "In back-up", "nu": "Nu"})
            st.caption(f"{len(shown)} verschillen")
            st.dataframe(shown, hide_index=True, width="stretch")
            st.download_button("Verschillen downloaden (CSV)", shown.to_csv(index=False, sep=";").encode("utf-8-sig"),
                               file_name="verschillen_boeken.csv", mime="text/csv")
        with st.expander("Boeken die alleen in de back-up of alleen nu bestaan"):
            titles_b = dict(zip(backup_books["id"], backup_books["title"]))
            titles_n = dict(zip(now_books["id"], now_books["title"]))
            st.write(f"Alleen in de back-up ({len(only_backup)}):")
            st.dataframe(pd.DataFrame({"Boeknummer": only_backup, "Titel": [titles_b.get(i, "") for i in only_backup]}),
                         hide_index=True, width="stretch")
            st.write(f"Alleen nu ({len(only_now)}):")
            st.dataframe(pd.DataFrame({"Boeknummer": only_now, "Titel": [titles_n.get(i, "") for i in only_now]}),
                         hide_index=True, width="stretch")
    if backup_orders is not None:
        st.divider()
        st.subheader("Bestellingen")
        now_orders = load_orders()
        o_only_b, o_only_n, o_diffs = compare_orders(backup_orders, now_orders)
        c1, c2, c3 = st.columns(3)
        c1.metric("Alleen in back-up", len(o_only_b))
        c2.metric("Alleen nu (nieuwer dan de back-up)", len(o_only_n))
        c3.metric("Bestellingen met een verschil", o_diffs["id"].nunique() if not o_diffs.empty else 0)
        if not o_diffs.empty:
            o_show = o_diffs.assign(veld=o_diffs["veld"].map(lambda f: ORDER_LABELS.get(f, f)))
            st.dataframe(o_show.rename(columns={"id": "Ordernummer", "veld": "Veld", "back-up": "In back-up",
                                                "nu": "Nu"}), hide_index=True, width="stretch")
        if o_only_b:
            st.warning(
                f"{len(o_only_b)} bestellingen staan wel in de back-up maar niet meer in de database. "
                "Controleer die: ze kunnen door een herstel zijn verdwenen."
            )

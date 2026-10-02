"""
app.py — Boekwinkeltjes-dashboard (overzichtspagina).

Losse pagina's voor boek- en orderdetails staan in de map pages/ en verschijnen
automatisch in de zijbalk.

Lokaal draaien:
    streamlit run app.py

Op Streamlit Community Cloud:
    Zet SUPABASE_DB_URL in de app-instellingen onder "Secrets".
"""

import time

import plotly.express as px
import psycopg2
import pandas as pd
import streamlit as st
from pathlib import Path

from common import (
    get_db_url,
    load_books,
    load_orders,
    format_price,
    format_order_status,
    format_datetime_nl,
    format_isbn,
    trigger_github_workflow,
    render_logo,
    require_login,
    get_isbndb_usage_history,
    get_database_size_mb,
)

st.set_page_config(page_title="Boekbeheersysteem", page_icon="📚", layout="wide")

render_logo(Path(__file__).parent / "assets" / "logo_mail.png")
require_login()

st.title("📚 Boekbeheersysteem")

books, orders = load_books(), load_orders()


def _poll_sync_completion(timeout_seconds=360, quiet_seconds=6):
    """
    Peilt de sync_log-tabel na het starten van een GitHub Action: laat zien zodra
    er nieuwe regels verschijnen, en meldt 'voltooid' zodra het een tijdje stil is
    gebleven (geen exacte voortgangsbalk — de app ziet niet wat er op GitHub
    zelf gebeurt, alleen wat er uiteindelijk in de log wordt bijgeschreven).
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM sync_log")
            start_id = cur.fetchone()[0]
    finally:
        conn.close()

    placeholder = st.empty()
    placeholder.info("Sync gestart, wachten op de eerste update...")
    start_time = time.time()
    last_new_row_time = None
    latest_detail = None
    seen_any = False

    while time.time() - start_time < timeout_seconds:
        conn = psycopg2.connect(get_db_url())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, detail FROM sync_log WHERE id > %(id)s ORDER BY id ASC",
                    {"id": start_id},
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if rows:
            seen_any = True
            start_id = rows[-1][0]
            latest_detail = rows[-1][1]
            last_new_row_time = time.time()
            placeholder.info(f"Bezig... laatste update: {latest_detail}")
        elif seen_any and last_new_row_time and (time.time() - last_new_row_time) > quiet_seconds:
            placeholder.success(f"Sync voltooid. Laatste update: {latest_detail}")
            load_books.clear()
            load_orders.clear()
            return

        time.sleep(2)

    placeholder.warning(
        "Nog niet klaar na een paar minuten — controleer 'Laatste sync-runs' hieronder."
    )


btn_col1, btn_col2, btn_col3, _spacer = st.columns([1, 1, 1, 4])

with btn_col1:
    if st.button("🔄 Ververs data"):
        st.cache_data.clear()
        st.rerun()

with btn_col2:
    if st.button("📚 Sync boeken nu"):
        ok, msg = trigger_github_workflow("sync.yml")
        if ok:
            _poll_sync_completion()
        else:
            st.error(msg)

with btn_col3:
    if st.button("🖼️ Sync afbeeldingen nu"):
        ok, msg = trigger_github_workflow("sync-images.yml")
        if ok:
            _poll_sync_completion()
        else:
            st.error(msg)


# ---------- Kerncijfers ----------

# Boeken in de wachtrij (na bulk-import, nog niet handmatig gecontroleerd) horen
# nog niet mee te tellen in het gewone overzicht.
queued_mask = books["queued"].fillna(False).astype(bool) if not books.empty else pd.Series(dtype=bool)
amount_mask = books["amount"].fillna(0) > 0 if not books.empty else pd.Series(dtype=bool)
visible_books = books[(~queued_mask) & amount_mask] if not books.empty else books
n_queued = int(queued_mask.sum()) if not books.empty else 0

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Boeken in voorraad", int(visible_books["amount"].fillna(0).sum()) if not visible_books.empty else 0)
col2.metric("Unieke titels", len(visible_books))
col3.metric("Boeken in bulkwachtrij", n_queued)

if not books.empty:
    bw_queue_mask = books["pending_create"].fillna(False).astype(bool) & books["push_enabled"].fillna(True).astype(bool)
    n_bw_queue = int(bw_queue_mask.sum())
else:
    n_bw_queue = 0
col4.metric("Boeken in BW-wachtrij", n_bw_queue)

if not orders.empty:
    seven_days_ago = pd.Timestamp.now() - pd.Timedelta(days=7)
    recent_orders = orders[orders["order_date"] >= seven_days_ago]
    order_platform = recent_orders["platform"].fillna("BW")
    n_orders_bw = int((order_platform == "BW").sum())
    n_orders_bol = int((order_platform == "Bol").sum())
else:
    n_orders_bw = 0
    n_orders_bol = 0
col5.metric("Orders laatste 7 dagen (BW/Bol)", f"{n_orders_bw}/{n_orders_bol} ({n_orders_bw + n_orders_bol})")

st.divider()

# ---------- Tabellen ----------

# "Laatste orders" staat vooraan, zodat die bij het openen van Home meteen zichtbaar is
# (dat overzicht toont wat actie vraagt).
orders_tab, books_tab, sync_tab = st.tabs(["Laatste orders", "Laatst toegevoegde boeken", "Laatste sync-runs"])

with books_tab:
    if visible_books.empty:
        st.info("Geen voorradige boeken gevonden.")
    else:
        books_sorted = visible_books.copy()
        # Sorteren op id (hoogste eerst) i.p.v. listing_date: het id verandert
        # nooit meer na aanmaak, terwijl listing_date mogelijk meebeweegt bij
        # latere bewerkingen aan het boek.
        books_sorted = books_sorted.sort_values("id", ascending=False).reset_index(drop=True)

        # Boeken met voorraad 0 niet tonen in dit overzicht (onbekende/lege
        # voorraad blijft wel zichtbaar, alleen expliciet 0 wordt uitgesloten).
        amount_ok = books_sorted["amount"].isna() | (books_sorted["amount"] != 0)
        books_sorted = books_sorted[amount_ok].reset_index(drop=True)

        book_cols = ["title", "author", "ean", "amount", "price", "category1", "location"]
        books_display = books_sorted[book_cols].copy()
        books_display["price"] = books_display["price"].apply(format_price)
        books_display["ean"] = books_display["ean"].apply(format_isbn)
        books_display["category1"] = books_sorted.apply(
            lambda r: f"{r['category1']}, {r['category2']}"
            if pd.notna(r["category2"]) and str(r["category2"]).strip()
            else r["category1"],
            axis=1,
        )
        books_display = books_display.rename(
            columns={
                "title": "Titel",
                "author": "Auteur",
                "ean": "ISBN",
                "amount": "Aantal",
                "price": "Prijs",
                "category1": "Categorie",
                "location": "Locatie",
            }
        )
        event = st.dataframe(
            books_display,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="books_overview_table",
        )
        st.caption("Klik op een rij om de boekdetails te openen. Gesorteerd op laatst toegevoegd.")
        selected_rows = event.selection.rows if event and event.selection else []
        if selected_rows:
            selected_book_id = int(books_sorted.iloc[selected_rows[0]]["id"])
            st.session_state["preselect_book_id"] = selected_book_id
            del st.session_state["books_overview_table"]  # voorkomt dat de selectie blijft hangen
            st.switch_page("pages/3_Boekdetails.py")

with orders_tab:
    if orders.empty:
        st.dataframe(orders, use_container_width=True)
    else:
        orders_sorted = orders.sort_values(
            "order_date", ascending=False, na_position="last"
        ).reset_index(drop=True)

        orders_sorted["koper"] = orders_sorted["buyer_name"].fillna("").where(
            orders_sorted["buyer_name"].fillna("").str.strip() != "",
            (orders_sorted["buyer_first_name"].fillna("") + " " + orders_sorted["buyer_last_name"].fillna("")).str.strip(),
        )
        orders_sorted["platform"] = orders_sorted["platform"].fillna("BW").map(
            lambda p: f"🟡{p}" if p == "BW" else f"🔵{p}"
        )

        order_cols = ["order_date", "platform", "status", "book_title", "book_ean", "revenue", "koper"]
        orders_display = orders_sorted[order_cols].copy()
        orders_display["revenue"] = orders_display["revenue"].apply(format_price)
        orders_display["status"] = orders_display["status"].map(format_order_status)
        orders_display["book_ean"] = orders_display["book_ean"].apply(format_isbn)
        orders_display = orders_display.rename(
            columns={
                "order_date": "Besteldatum",
                "platform": "Met",
                "status": "Status",
                "book_title": "Titel",
                "book_ean": "ISBN",
                "revenue": "Prijs",
                "koper": "Koper",
            }
        )
        event = st.dataframe(
            orders_display,
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="orders_overview_table",
        )
        st.caption("Klik op een rij om de orderdetails te openen. Gesorteerd op orderdatum.")
        selected_rows = event.selection.rows if event and event.selection else []
        if selected_rows:
            selected_order_id = int(orders_sorted.iloc[selected_rows[0]]["id"])
            st.session_state["preselect_order_id"] = selected_order_id
            del st.session_state["orders_overview_table"]
            st.switch_page("pages/7_Orderdetails.py")

with sync_tab:
    conn = psycopg2.connect(get_db_url())
    log = pd.read_sql("SELECT * FROM sync_log ORDER BY run_at DESC LIMIT 20", conn)
    conn.close()
    log["run_at"] = log["run_at"].apply(format_datetime_nl)
    log["trigger"] = log["trigger"].fillna("Handmatig")
    log["platform"] = log["platform"].fillna("BW").map(lambda p: f"🟡{p}" if p == "BW" else f"🔵{p}")
    log = log.rename(
        columns={
            "id": "ID",
            "run_at": "Datum en tijd",
            "platform": "Met",
            "trigger": "Type",
            "direction": "Richting",
            "resource": "Bron",
            "status": "Status",
            "detail": "Details",
        }
    )
    log = log[["ID", "Datum en tijd", "Met", "Type", "Richting", "Bron", "Status", "Details"]]
    st.dataframe(log, use_container_width=True)

st.divider()

# ---------- Omzet per week ----------

left, right = st.columns(2)

with left:
    st.subheader("Omzet per week")
    if not orders.empty and orders["order_date"].notna().any():
        weekly = (
            orders.dropna(subset=["order_date"])
            .set_index("order_date")
            .resample("W")["revenue"]
            .sum()
            .reset_index()
        )
        fig = px.bar(weekly, x="order_date", y="revenue", labels={"order_date": "Week", "revenue": "Omzet (€)"})
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Nog geen orders met een geldige datum om te tonen.")

# ---------- Orderstatus overzicht ----------

with right:
    st.subheader("Orders per status")
    if not orders.empty:
        status_counts = orders["status"].map(format_order_status).value_counts().reset_index()
        status_counts.columns = ["status", "aantal"]
        fig = px.pie(status_counts, names="status", values="aantal")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Nog geen orders om te tonen.")

st.divider()

# ---------- Categorie-overzicht ----------

st.subheader("Voorraad per categorie (top 20)")
if not books.empty:
    combined_category = books.apply(
        lambda r: f"{r['category1']}, {r['category2']}"
        if pd.notna(r["category2"]) and str(r["category2"]).strip()
        else r["category1"],
        axis=1,
    )
    cat_counts = combined_category.fillna("onbekend").value_counts().head(20).reset_index()
    cat_counts.columns = ["categorie", "aantal titels"]
    fig = px.bar(cat_counts, x="categorie", y="aantal titels")
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Nog geen boeken om te tonen.")

st.divider()

left2, right2 = st.columns(2)

# ---------- Boeken per uitgever ----------

with left2:
    st.subheader("Boeken per uitgever (top 20)")
    if not books.empty:
        pub_series = books["publisher_name"].fillna("").astype(str).str.strip()
        pub_counts = pub_series[pub_series != ""].value_counts().head(20).reset_index()
        pub_counts.columns = ["uitgever", "aantal boeken"]
        fig = px.bar(pub_counts, x="uitgever", y="aantal boeken")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Nog geen boeken om te tonen.")

# ---------- Boeken van Rick (op basis van 'R' in de locatie) ----------

with right2:
    st.subheader("Boeken van Rick")
    if not books.empty:
        is_rick = books["location"].fillna("").str.contains("r", case=False)
        rick_counts = is_rick.map({True: "Van Rick", False: "Niet van Rick"}).value_counts().reset_index()
        rick_counts.columns = ["wie", "aantal boeken"]
        fig = px.pie(rick_counts, names="wie", values="aantal boeken")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Gebaseerd op een 'r' (hoofd- of kleine letter) in het locatieveld.")
    else:
        st.info("Nog geen boeken om te tonen.")

st.divider()

# ---------- Omzet per dag ----------

st.subheader("Omzet per dag (boekprijs, zonder verzendkosten)")
if not orders.empty and orders["order_date"].notna().any():
    daily = (
        orders.dropna(subset=["order_date"])
        .set_index("order_date")
        .resample("D")["book_price"]
        .sum()
        .reset_index()
    )
    fig = px.line(daily, x="order_date", y="book_price", labels={"order_date": "Datum", "book_price": "Omzet (€)"})
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Nog geen orders met een geldige datum om te tonen.")

st.divider()

# ---------- ISBNdb API-gebruik ----------

st.subheader("ISBNdb API-gebruik (resterend dagquotum)")
usage_history = get_isbndb_usage_history()
if usage_history:
    usage_df = pd.DataFrame(usage_history)
    fig = px.line(
        usage_df,
        x="checked_at",
        y="daily_remaining",
        labels={"checked_at": "Moment", "daily_remaining": "Resterend dagquotum"},
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Elke opzoekactie bij ISBNdb (via 'Nieuw boek') legt vast hoeveel van het "
        "dagquotum nog over was op dat moment — vandaar dat dit alleen bijwerkt "
        "als er daadwerkelijk een ISBN is opgezocht."
    )
else:
    st.info("Nog geen ISBNdb-gebruik geregistreerd.")

st.divider()

# ---------- Supabase-opslag ----------

st.subheader("Supabase-opslag")

db_size_mb = get_database_size_mb()
DB_LIMIT_MB = 500.0
db_used_pct = min(db_size_mb / DB_LIMIT_MB * 100, 100)

fig_db = px.pie(
    values=[db_size_mb, max(DB_LIMIT_MB - db_size_mb, 0)],
    names=["Gebruikt", "Vrij"],
    title=f"Databaseopslag: {db_used_pct:.1f}% van {DB_LIMIT_MB:.0f} MB",
    hole=0.4,
)
storage_chart_col, _storage_spacer = st.columns([1, 2])
with storage_chart_col:
    st.plotly_chart(fig_db, use_container_width=True)

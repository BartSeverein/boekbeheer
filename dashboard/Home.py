"""
app.py — Boekwinkeltjes-dashboard (overzichtspagina).

Losse pagina's voor boek- en orderdetails staan in de map pages/ en verschijnen
automatisch in de zijbalk.

Lokaal draaien:
    streamlit run app.py

Op Streamlit Community Cloud:
    Zet SUPABASE_DB_URL in de app-instellingen onder "Secrets".
"""

import datetime as dt
import time

import plotly.express as px
import psycopg2
import pandas as pd
import streamlit as st
from pathlib import Path

from common import (
    get_db_url,
    load_books,
    count_books_without_shipping_format,
    get_queued_books,
    queued_counts,
    current_user_short_name,
    load_orders,
    format_price,
    format_order_status,
    format_datetime_nl,
    format_order_datetime,
    AMSTERDAM_TZ,
    cron_status_label,
    failed_cron_jobs,
    get_cron_job_status,
    get_github_token_expiry,
    github_token_banner,
    get_cron_job_history,
    format_isbn,
    trigger_github_workflow,
    render_logo,
    require_login,
    get_database_size_mb,
    get_bol_condition_rows,
    bol_condition_counts,
    BOL_CONDITION_ORDER,
)

st.set_page_config(page_title="Boekbeheersysteem", page_icon="📚", layout="wide")

render_logo(Path(__file__).parent / "assets" / "logo_mail.png")
require_login()

st.title("📚 Boekbeheersysteem")

books, orders = load_books(), load_orders()

# Zonder verzendformaat kan Boekwinkeltjes een boek niet verkopen: laat zien als er boeken zijn die dat nog missen.
try:
    _missing_format = count_books_without_shipping_format(books)
    if _missing_format:
        st.warning(
            f"⚠️ {_missing_format} {'boek heeft' if _missing_format == 1 else 'boeken hebben'} nog geen verzendformaat "
            "bij Boekwinkeltjes en "
            f"{'kan' if _missing_format == 1 else 'kunnen'} daar niet worden verkocht. "
            "Zie de workflow 'Verzendformaat vullen (eenmalig)' in GitHub Actions."
        )
except Exception:
    pass  # een hulpmelding mag Home nooit laten crashen


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
col1.metric("In voorraad", int(visible_books["amount"].fillna(0).sum()) if not visible_books.empty else 0)
col2.metric("Unieke titels", len(visible_books))
# Bulkwachtrij: eerst wat ik zelf heb toegevoegd, dan het totaal (bijv. 20/44).
try:
    _n_queued_mine, _n_queued_total = queued_counts(get_queued_books(), current_user_short_name())
    _queue_value = f"{_n_queued_mine}/{_n_queued_total}"
except Exception:
    _queue_value = f"–/{n_queued}"  # wie wat heeft toegevoegd kon niet worden opgehaald; het totaal klopt wel
col3.metric("Bulkwachtrij (ik/alles)", _queue_value)

if not books.empty:
    bw_queue_mask = books["pending_create"].fillna(False).astype(bool) & books["push_enabled"].fillna(True).astype(bool)
    n_bw_queue = int(bw_queue_mask.sum())
else:
    n_bw_queue = 0
col4.metric("BW-wachtrij", n_bw_queue)

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
            width="stretch",
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
        st.dataframe(orders, width="stretch")
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
        orders_display["order_date"] = orders_display["order_date"].apply(format_order_datetime)
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
            width="stretch",
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
    st.dataframe(log, width="stretch")

st.divider()

# ---------- Geplande taken (cron-job.org) ----------

st.subheader("Geplande taken")

# De status van cron-job.org wordt 6 uur onthouden (zijn daglimiet is klein). Deze knop haalt hem nu opnieuw op: dat kost
# één aanroep. De geschiedenis voor het duurgrafiek blijft bewust onthouden (die kost er veel meer, en duurt een minuut).
if st.button("🔄 Status verversen", key="refresh_cron_status", help="Haalt de actuele status van cron-job.org op (kost één van de 100 aanroepen per dag)."):
    get_cron_job_status.clear()
    st.rerun()

try:
    # De einddatum van de GitHub-sleutel: verloopt die, dan stoppen alle geplande taken. Een storing hier mag Home niet breken.
    banner_kind, banner_text = github_token_banner(get_github_token_expiry())
    getattr(st, banner_kind)(banner_text)
except Exception:
    st.caption("De einddatum van de GitHub-sleutel kon nu niet worden opgevraagd.")

cron_jobs, cron_error = get_cron_job_status()
if not cron_jobs:
    if cron_error:
        st.info(f"Geen gegevens van cron-job.org beschikbaar: {cron_error}")
    else:
        st.info("Geen gegevens van cron-job.org beschikbaar (geen taken gevonden).")
else:
    # --- mislukte taken (begin) ---
    failed_jobs = failed_cron_jobs(cron_jobs)
    if failed_jobs:
        failed_names = ", ".join(job.get("title", "(naamloos)") for job in failed_jobs)
        st.error(
            f"🚨 {len(failed_jobs)} geplande {'taak is' if len(failed_jobs) == 1 else 'taken zijn'} mislukt bij de laatste "
            f"uitvoering: {failed_names}. Open de taak bij cron-job.org en kijk in de geschiedenis naar de HTTP-code. "
            f"Deze tabel kan tot 6 uur oud zijn; met de knop hierboven haal je de actuele status op."
        )
        with st.expander("Wat betekent de HTTP-code?"):
            st.markdown(
                """
Alle taken roepen GitHub aan om een workflow te starten. Bij een HTTP-fout is dit meestal de oorzaak:
- **401**: de GitHub-sleutel in de kopregel `Authorization` klopt niet, is ingetrokken of vervangen. Let op: `Bearer ` met een spatie ervoor.
- **403**: de sleutel mist het recht *Actions: Read and write*.
- **404**: de sleutel heeft geen toegang tot de repository `boekbeheer`, of de URL of bestandsnaam van de workflow klopt niet.
- **422**: de workflow heeft geen handmatige start, of de gegevens in de body kloppen niet.
- **429**: te veel aanroepen kort na elkaar.
                """
            )
    # --- mislukte taken (einde) ---

    rows = []
    for job in cron_jobs:
        last_exec = job.get("lastExecution") or 0
        next_exec = job.get("nextExecution") or 0
        rows.append(
            {
                "Taak": job.get("title", "(naamloos)"),
                "Laatste run": format_datetime_nl(dt.datetime.fromtimestamp(last_exec, tz=dt.timezone.utc)) if last_exec else "–",
                "Status": cron_status_label(job.get("lastStatus")),
                "Duur": f"{job.get('lastDuration', 0) / 1000:.1f}s" if job.get("lastDuration") else "–",
                "Volgende run": format_datetime_nl(dt.datetime.fromtimestamp(next_exec, tz=dt.timezone.utc)) if next_exec else "–",
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("Duur van de taken")
    history_frames = []
    with st.spinner("Geschiedenis per taak ophalen (kan, vanwege cron-job.org's limiet, bij een koude cache circa een minuut duren)..."):
        for job in cron_jobs:
            job_history = get_cron_job_history(job["jobId"])
            for item in job_history:
                date_val = item.get("date") or item.get("time")
                if date_val:
                    history_frames.append(
                        {
                            "Taak": job.get("title", "(naamloos)"),
                            "Moment": dt.datetime.fromtimestamp(date_val, tz=dt.timezone.utc).astimezone(AMSTERDAM_TZ),
                            "Duur (s)": (item.get("duration") or 0) / 1000,
                        }
                    )
    if history_frames:
        history_df = pd.DataFrame(history_frames).sort_values("Moment")
        fig_cron = px.line(
            history_df, x="Moment", y="Duur (s)", color="Taak", markers=True,
            labels={"Duur (s)": "Duur (seconden)"},
        )
        fig_cron.update_xaxes(tickformat="%d-%m-%Y<br>%H:%M:%S", hoverformat="%d-%m-%Y %H:%M:%S")
        st.plotly_chart(fig_cron, width="stretch")
    else:
        st.caption("Nog geen uitvoeringsgeschiedenis beschikbaar.")

st.divider()

# ---------- Orderstatus overzicht + Boeken van Rick ----------

left1, right1 = st.columns(2)

with left1:
    st.subheader("Orders Boekwinkeltjes per status")
    orders_bw = orders[orders["platform"].fillna("BW") == "BW"] if not orders.empty else orders
    if not orders_bw.empty:
        status_counts = orders_bw["status"].map(format_order_status).value_counts().reset_index()
        status_counts.columns = ["status", "aantal"]
        fig = px.pie(status_counts, names="status", values="aantal")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Nog geen orders om te tonen.")

with right1:
    st.subheader("Boeken van Rick")
    if not books.empty:
        is_rick = books["location"].fillna("").str.contains("r", case=False)
        rick_counts = is_rick.map({True: "Van Rick", False: "Niet van Rick"}).value_counts().reset_index()
        rick_counts.columns = ["wie", "aantal boeken"]
        fig = px.pie(rick_counts, names="wie", values="aantal boeken")
        st.plotly_chart(fig, width="stretch")
        st.caption("Gebaseerd op een 'r' (hoofd- of kleine letter) in het locatieveld.")
    else:
        st.info("Nog geen boeken om te tonen.")

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
    st.plotly_chart(fig, width="stretch")
else:
    st.info("Nog geen boeken om te tonen.")

st.divider()

# ---------- Boeken per uitgever + Supabase-opslag ----------

left2, middle2, right2 = st.columns(3)

with left2:
    st.subheader("Boeken per uitgever (top 20)")
    if not books.empty:
        pub_series = books["publisher_name"].fillna("").astype(str).str.strip()
        pub_counts = pub_series[pub_series != ""].value_counts().head(20).reset_index()
        pub_counts.columns = ["uitgever", "aantal boeken"]
        fig = px.bar(pub_counts, x="uitgever", y="aantal boeken")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Nog geen boeken om te tonen.")

with middle2:
    st.subheader("Conditie Bol-aanbod")
    try:
        condition_rows = get_bol_condition_rows()
        condition_error = None
    except Exception as error:
        condition_rows = None
        condition_error = type(error).__name__
    if condition_error:
        st.info(f"De conditie van het Bol-aanbod kon nu niet worden opgevraagd ({condition_error}).")
    elif condition_rows is None:
        st.info("Nog niet beschikbaar: de conditie wordt bij de eerstvolgende voorraadsync met Bol opgeslagen.")
    else:
        condition_counts = bol_condition_counts(condition_rows)
        if not condition_counts:
            st.info("Nog geen conditie bekend. Die wordt bij de eerstvolgende voorraadsync met Bol opgeslagen.")
        else:
            condition_df = pd.DataFrame(condition_counts, columns=["conditie", "aantal"])
            fig_condition = px.pie(
                condition_df,
                names="conditie",
                values="aantal",
                category_orders={"conditie": BOL_CONDITION_ORDER},
                color="conditie",
                # Tinten blauw uit de rest van de app. 'Als nieuw' en 'Redelijk' zijn het lichtste en donkerste blauw
                # van de opslaggrafiek hiernaast. 'Goed' ligt precies halverwege; elke stap (D) is even groot in
                # helderheid, dus 'Nieuw' is D lichter dan 'Als nieuw' en 'Matig' is D donkerder dan 'Redelijk'.
                color_discrete_map={
                    "Nieuw": "#E0F1FF", "Als nieuw": "#83C9FF", "Goed": "#269CFF",
                    "Redelijk": "#0068C9", "Matig": "#00386C", "Onbekend": "#9E9E9E",
                },
            )
            fig_condition.update_traces(sort=False, textinfo="label+percent")
            st.plotly_chart(fig_condition, width="stretch")
            st.caption(f"{len(condition_rows)} aanbiedingen met voorraad bij Bol.")

with right2:
    st.subheader("Supabase-opslag")
    DB_LIMIT_MB = 500.0
    try:
        # Zelf omzetten naar een gewoon kommagetal: Postgres levert een som als Decimal, en die laat zich niet
        # delen door de limiet hieronder. Lukt het meten niet, dan een melding; één grafiek mag de pagina niet breken.
        db_size_mb = float(get_database_size_mb())
        db_error = None
    except Exception as error:
        db_size_mb = None
        db_error = type(error).__name__
    if db_size_mb is None:
        st.info(f"De grootte van de database kon nu niet worden opgevraagd ({db_error}).")
    else:
        db_used_pct = min(db_size_mb / DB_LIMIT_MB * 100, 100)
        fig_db = px.pie(
            values=[db_size_mb, max(DB_LIMIT_MB - db_size_mb, 0)],
            names=["Gebruikt", "Vrij"],
            title=f"Databaseopslag: {db_used_pct:.1f}% van {DB_LIMIT_MB:.0f} MB",
            hole=0.4,
        )
        st.plotly_chart(fig_db, width="stretch")

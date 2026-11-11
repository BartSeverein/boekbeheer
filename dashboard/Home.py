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
import plotly.graph_objects as go
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
    load_revenue_history,
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
    format_isbn,
    trigger_github_workflow,
    render_logo,
    require_login,
    get_database_size_mb,
    get_largest_objects,
    get_search_console_data,
    get_search_console_queries,
    get_github_workflow_runs,
    github_run_for_cron_job,
    github_run_label,
    github_run_duration_label,
    get_mollie_payments,
    WEBSHOP_FIRST_REAL_ORDER,
    GSC_PERIODS,
    get_bol_condition_rows,
    get_weekly_stock_snapshots,
    get_current_stock_snapshot,
    bol_condition_counts,
    BOL_CONDITION_ORDER,
)

st.set_page_config(page_title="Boekbeheersysteem", page_icon="📚", layout="wide")

# Kleurbolletje voor de kolom "Met": Boekwinkeltjes geel, Bol blauw, eigen webshop paars.
PLATFORM_EMOJI = {"BW": "🟡", "Bol": "🔵", "Webshop": "🟣"}

render_logo(Path(__file__).parent / "assets" / "logo_mail.png")
current_user = require_login()
# Omzetcijfers en betalingen zijn alleen voor beheerders (Gebruikersbeheer); zonder gebruikers (opstartfase) is alles zichtbaar.
can_see_finance = current_user is None or current_user.get("role") == "beheerder"

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

col1, col2, col_total, col3, col4, col5 = st.columns(6)
col1.metric("In voorraad", int(visible_books["amount"].fillna(0).sum()) if not visible_books.empty else 0,
             help="Alle boeken in de database die te koop staan (van sommige titels meerdere)")
col2.metric("Unieke titels", len(visible_books), help="Alle titels in de database die te koop staan")
# Alle titels in de database die al op Boekwinkeltjes staan, dus ook verkochte boeken (voorraad 0); zonder bulkwachtrij en zonder boeken die nog niet op BW staan.
col_total.metric("Titels totaal", int(((~queued_mask) & (books["id"] > 0)).sum()) if not books.empty else 0,
                 help="Alle boeken in de database, ook die verkocht zijn en niet meer op voorraad")
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
    n_orders_ws = int((order_platform == "Webshop").sum())
else:
    n_orders_bw = 0
    n_orders_bol = 0
    n_orders_ws = 0
col5.metric("Orders laatste 7 dagen (BW/Bol/WS)", f"{n_orders_bw}/{n_orders_bol}/{n_orders_ws} ({n_orders_bw + n_orders_bol + n_orders_ws})")

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
        # Nieuwe boeken die nog op het druppelsysteem wachten hebben een tijdelijk NEGATIEF id (hoe nieuwer, hoe
        # lager). Die horen bovenaan: daarna komen de boeken met een echt id, hoogste eerst.
        books_sorted["_volgorde"] = books_sorted["id"].where(books_sorted["id"] > 0, 10**12 - books_sorted["id"])
        books_sorted = books_sorted.sort_values("_volgorde", ascending=False).drop(columns="_volgorde").reset_index(drop=True)

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
        st.caption("Klik op een rij om de boekdetails te openen. Gesorteerd op laatst toegevoegd; boeken die nog op het druppelsysteem wachten staan bovenaan.")
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
            lambda p: PLATFORM_EMOJI.get(p, "🔵") + p
        )

        _locs = books.drop_duplicates("id").set_index("id")["location"] if "location" in books.columns else {}
        orders_sorted["location"] = orders_sorted["book_id"].map(_locs).fillna("") if len(_locs) else ""
        order_cols = ["order_date", "platform", "status", "book_title", "location", "book_ean", "revenue", "koper"]
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
                "location": "Locatie",
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
            st.switch_page("pages/5_Orderdetails.py")

with sync_tab:
    conn = psycopg2.connect(get_db_url())
    log = pd.read_sql("SELECT * FROM sync_log ORDER BY run_at DESC LIMIT 20", conn)
    conn.close()
    log["run_at"] = log["run_at"].apply(format_datetime_nl)
    log["trigger"] = log["trigger"].fillna("Handmatig")
    log["platform"] = log["platform"].fillna("BW").map(lambda p: PLATFORM_EMOJI.get(p, "🔵") + p)
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
# één aanroep.
if st.button("🔄 Status verversen", key="refresh_cron_status", help="Haalt de actuele status van cron-job.org op (kost één van de 100 aanroepen per dag)."):
    get_cron_job_status.clear()
    get_github_workflow_runs.clear()
    st.rerun()

try:
    # De einddatum van de GitHub-sleutel: verloopt die, dan stoppen alle geplande taken. Een storing hier mag Home niet breken.
    banner_kind, banner_text = github_token_banner(get_github_token_expiry())
    getattr(st, banner_kind)(banner_text)
except Exception:
    st.caption("De einddatum van de GitHub-sleutel kon nu niet worden opgevraagd.")

try:
    # Blokkade door Boekwinkeltjes: de laatste 12 uur gemeld en sindsdien geen geslaagde ophaalactie van boeken.
    _bconn = psycopg2.connect(get_db_url())
    with _bconn.cursor() as _bcur:
        _bcur.execute("SELECT max(run_at) FROM sync_log WHERE resource = 'blokkade' AND run_at > now() - interval '12 hours'")
        _blocked_at = _bcur.fetchone()[0]
        _ok_after = None
        if _blocked_at:
            _bcur.execute("SELECT max(run_at) FROM sync_log WHERE direction = 'pull' AND resource = 'books' AND status = 'ok' AND run_at > %s",
                          (_blocked_at,))
            _ok_after = _bcur.fetchone()[0]
    _bconn.close()
    if _blocked_at and not _ok_after:
        st.error(f"🚫 Boekwinkeltjes weigert onze aanroepen (403 Forbidden van de webserver), laatst gemeld {format_datetime_nl(_blocked_at)}. "
                 "Waarschijnlijk is het IP-adres van GitHub geblokkeerd. De synchronisatie met Boekwinkeltjes stopt direct tot dit is opgelost; "
                 "neem contact op met Boekwinkeltjes. Deze melding verdwijnt zodra een ophaalactie weer lukt.")
except Exception:
    pass

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
    github_failed = []
    for job in cron_jobs:
        last_exec = job.get("lastExecution") or 0
        next_exec = job.get("nextExecution") or 0
        try:
            gh_run, gh_error = github_run_for_cron_job(job)
        except Exception:
            gh_run, gh_error = None, "onbekend"
        if gh_run is not None and gh_run["conclusion"] in ("failure", "timed_out"):
            github_failed.append(job.get("title", "(naamloos)"))
        rows.append(
            {
                "Taak": job.get("title", "(naamloos)"),
                "Laatste run": format_datetime_nl(dt.datetime.fromtimestamp(last_exec, tz=dt.timezone.utc)) if last_exec else "–",
                "Status": cron_status_label(job.get("lastStatus")),
                "Uitkomst bij GitHub": "⚠️ niet op te halen" if gh_error else github_run_label(gh_run),
                "Duur": "–" if gh_error else github_run_duration_label(gh_run),
                "Volgende run": format_datetime_nl(dt.datetime.fromtimestamp(next_exec, tz=dt.timezone.utc)) if next_exec else "–",
            }
        )
    if github_failed:
        st.error(f"🚨 Bij GitHub is de laatste uitvoering mislukt van: {', '.join(github_failed)}. De aanroep zelf slaagde, maar de taak "
                 f"liep daarna fout. Open GitHub onder Actions voor de foutmelding.")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("'Status' is of cron-job.org GitHub heeft aangeroepen; 'Uitkomst bij GitHub' en 'Duur' gaan over de taak zelf bij GitHub.")

st.divider()

# Blauwtinten voor de taarten Boeken per conditie, Boeken per taal en Orders per status: van licht (#83C9FF) naar donker
# (#00386C), gelijkmatig verdeeld; bij taal en status hoe hoger de waarde hoe lichter.
_BLUE_SCALE = ["#83C9FF", "#269CFF", "#0068C9", "#00386C"]


def _blue_shades(n):
    """n tinten uit _BLUE_SCALE, gelijkmatig verdeeld van licht naar donker (bij 1 stuk het lichtste)."""
    if n <= 0:
        return []
    if n == 1:
        return [_BLUE_SCALE[0]]
    rgb = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in _BLUE_SCALE]
    out = []
    for k in range(n):
        pos = k * (len(rgb) - 1) / (n - 1)
        lo = int(pos)
        hi = min(lo + 1, len(rgb) - 1)
        frac = pos - lo
        mix = tuple(round(rgb[lo][j] + (rgb[hi][j] - rgb[lo][j]) * frac) for j in range(3))
        out.append("#%02X%02X%02X" % mix)
    return out


def _blue_pie(df, names, values, grey=()):
    """Taart waarbij de grootste punt het lichtst is en de kleinste het donkerst; namen in 'grey' (Onbekend, Overig) worden grijs."""
    df = df.sort_values(values, ascending=False).reset_index(drop=True)
    colored = [n for n in df[names] if n not in grey]
    shades = dict(zip(colored, _blue_shades(len(colored))))
    cmap = {n: shades.get(n, "#9E9E9E") for n in df[names]}
    fig = px.pie(df, names=names, values=values, color=names, color_discrete_map=cmap)
    fig.update_traces(sort=False)
    return fig


# ---------- Orderstatus overzicht + Boeken van Rick ----------

left1, right1 = st.columns(2)

with left1:
    st.subheader("Orders Boekwinkeltjes per status")
    orders_bw = orders[orders["platform"].fillna("BW") == "BW"] if not orders.empty else orders
    if not orders_bw.empty:
        status_counts = orders_bw["status"].map(format_order_status).value_counts().reset_index()
        status_counts.columns = ["status", "aantal"]
        fig = _blue_pie(status_counts, "status", "aantal")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Nog geen orders om te tonen.")

with right1:
    st.subheader("Boeken van Rick")
    if not books.empty:
        _rick_sale = books[(books["amount"].fillna(0) > 0) & (books["id"] > 0)]
        if "queued" in _rick_sale.columns:
            _rick_sale = _rick_sale[~_rick_sale["queued"].fillna(False).astype(bool)]
        is_rick = _rick_sale["location"].fillna("").str.contains("r", case=False)
        rick_counts = is_rick.map({True: "Wel van Rick", False: "Niet van Rick"}).value_counts().reset_index()
        rick_counts.columns = ["wie", "aantal boeken"]
        fig = px.pie(rick_counts, names="wie", values="aantal boeken")
        st.plotly_chart(fig, width="stretch")
        st.caption("Van de verkoopvoorraad.")
    else:
        st.info("Nog geen boeken om te tonen.")


st.divider()

# ---------- Conditie + Boeken per taal ----------

left3, right3 = st.columns(2)

with left3:
    st.subheader("Boeken per conditie")
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
                # Zelfde blauwtinten als de andere taarten (_blue_shades): 'Nieuw' het lichtst, 'Matig' het donkerst.
                color_discrete_map={**dict(zip(["Nieuw", "Als nieuw", "Goed", "Redelijk", "Matig"], _blue_shades(5))), "Onbekend": "#9E9E9E"},
            )
            fig_condition.update_traces(sort=False, textinfo="label+percent")
            st.plotly_chart(fig_condition, width="stretch")
            st.caption("Van de verkoopvoorraad.")

with right3:
    st.subheader("Boeken per taal")
    # Alle boeken in de verkoop (voorraad, niet in de wachtrij). Schrijfwijzen van dezelfde taal tellen samen (gb/en/English -> Engels).
    _LANG_NAMES = {"nl": "Nederlands", "en": "Engels", "de": "Duits", "fr": "Frans", "es": "Spaans", "it": "Italiaans"}
    _LANG_ALIASES = {
        "en": "en", "gb": "en", "uk": "en", "en-gb": "en", "en-us": "en", "us": "en", "eng": "en", "english": "en", "engels": "en",
        "nl": "nl", "nld": "nl", "dut": "nl", "dutch": "nl", "nederlands": "nl",
        "de": "de", "deu": "de", "ger": "de", "german": "de", "duits": "de",
        "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "frans": "fr",
        "es": "es", "spa": "es", "spanish": "es", "spaans": "es",
        "it": "it", "ita": "it", "italian": "it", "italiaans": "it",
    }
    if not books.empty:
        _sale = books[(books["amount"].fillna(0) > 0) & (books["id"] > 0)]
        if "queued" in _sale.columns:
            _sale = _sale[~_sale["queued"].fillna(False).astype(bool)]
        _raw = _sale["language"].fillna("").astype(str).str.strip().str.lower()

        def _lang_label(value):
            if not value:
                return "Onbekend"
            code = _LANG_ALIASES.get(value, value)
            return _LANG_NAMES.get(code, value.upper() if len(value) <= 3 else value.capitalize())

        _counts = _raw.map(_lang_label).value_counts()
        if _counts.empty:
            st.info("Nog geen boeken om te tonen.")
        else:
            # Kleine talen (onder 1%) samenvoegen, anders wordt de taart onleesbaar.
            _small = _counts[_counts / _counts.sum() < 0.01]
            _counts = _counts.drop(_small.index)
            if len(_small):
                _counts["Overig (" + str(len(_small)) + " talen)"] = _small.sum()
            _df_lang = _counts.reset_index()
            _df_lang.columns = ["taal", "aantal boeken"]
            fig_lang = _blue_pie(_df_lang, "taal", "aantal boeken", grey=("Onbekend",) + tuple(n for n in _df_lang["taal"] if str(n).startswith("Overig")))
            fig_lang.update_traces(textinfo="label+percent")
            st.plotly_chart(fig_lang, width="stretch")
            st.caption("Van de verkoopvoorraad.")
    else:
        st.info("Nog geen boeken om te tonen.")

# ---------- Boeken per uitgavejaar ----------

st.subheader("Boeken per uitgavejaar")
if not books.empty:
    _ysale = books[(books["amount"].fillna(0) > 0) & (books["id"] > 0)]
    if "queued" in _ysale.columns:
        _ysale = _ysale[~_ysale["queued"].fillna(False).astype(bool)]
    # Jaar = het eerste viercijferige getal in Bijzonderheden; boeken zonder (geloofwaardig) jaar tellen niet mee.
    _this_year = dt.date.today().year
    _years = pd.to_numeric(
        _ysale["short_description"].fillna("").astype(str).str.extract(r"(?<!\d)(\d{4})(?!\d)")[0], errors="coerce"
    ).dropna().astype(int)
    _years = _years[(_years >= 1400) & (_years <= _this_year + 1)]
    _OLD = 1926
    if _years.empty:
        st.info("Nog geen boeken met een uitgavejaar in Bijzonderheden.")
    else:
        _years = _years.clip(lower=_OLD)
        _ycounts = _years.value_counts().reindex(range(_OLD, int(_years.max()) + 1), fill_value=0).sort_index()
        _ticks = [_OLD] + list(range(1930, int(_years.max()) + 1, 5))
        fig_year = go.Figure(go.Bar(
            x=_ycounts.index, y=_ycounts.values,
            customdata=["1926 of ouder" if y == _OLD else str(y) for y in _ycounts.index],
            hovertemplate="%{customdata}: %{y} boeken<extra></extra>",
        ))
        fig_year.update_layout(
            xaxis=dict(tickmode="array", tickvals=_ticks, ticktext=["1926 of ouder" if t == _OLD else str(t) for t in _ticks],
                       tickangle=-45, title=None),
            yaxis=dict(title="aantal boeken"), bargap=0.15, margin=dict(t=10),
        )
        st.plotly_chart(fig_year, width="stretch")
        st.caption(f"Van de verkoopvoorraad; {len(_years)} van {len(_ysale)} boeken hebben een jaartal in Bijzonderheden.")
else:
    st.info("Nog geen boeken om te tonen.")

st.divider()

# ---------- Wekelijks aanbod: titels en exemplaren, met het deel dat ook op Bol staat ----------

def _euro(value):
    """Bedrag als € 1.234,50 (Nederlandse schrijfwijze)."""
    return "€ " + f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _nl_number(value):
    """Nederlandse schrijfwijze van een heel getal: punt als duizendtalscheiding, maar pas vanaf 10.000 (dus 3400, 12.345)."""
    value = int(round(float(value)))
    return f"{value:,}".replace(",", ".") if abs(value) >= 10000 else str(value)


def _weekly_stock_chart(snapshots, total_key, bol_key, y_title, money=False):
    """Gestapelde staaf per meting: onderaan wat ook op Bol staat, daarboven de rest van wat in de verkoop is (samen = totaal)."""
    data = pd.DataFrame(snapshots)
    data["week"] = pd.to_datetime(data["measured_on"]).dt.strftime("%m-%Y")   # een staaf per maand
    if "label" in data.columns:   # de laatste, actuele staaf heet 'Vandaag' in plaats van een datum
        data["week"] = data["label"].where(data["label"].notna(), data["week"])
    # Hele getallen (ook bij euro's): eerst afronden, dan pas 'alleen Boekwinkeltjes' = totaal min Bol,
    # zodat de getoonde delen precies bij het totaal optellen.
    data[total_key] = data[total_key].astype(float).round(0)
    data[bol_key] = data[bol_key].astype(float).round(0)
    prefix = "€ " if money else ""
    long_rows = []
    for _, row in data.iterrows():
        alone = max(float(row[total_key]) - float(row[bol_key]), 0)
        long_rows.append({"week": row["week"], "soort": "Ook op Bol", "aantal": float(row[bol_key]),
                          "label": prefix + _nl_number(row[bol_key])})
        long_rows.append({"week": row["week"], "soort": "Alleen BW en webshop", "aantal": alone,
                          "label": prefix + _nl_number(alone)})
    fig = px.bar(
        pd.DataFrame(long_rows), x="week", y="aantal", color="soort", text="label",
        category_orders={"week": list(data["week"]), "soort": ["Ook op Bol", "Alleen BW en webshop"]},
        # Dezelfde blauwtinten als de andere grafieken: donker = ook op Bol, licht = alleen BW en webshop.
        color_discrete_map={"Ook op Bol": "#0068C9", "Alleen BW en webshop": "#83C9FF"},
    )
    fig.update_traces(texttemplate="%{text}", hovertemplate="%{x}<br>%{fullData.name}: %{text}<extra></extra>")
    # Komma voor decimalen, punt voor duizendtallen (voor de aslabels; de getallen in de staven zijn hierboven al gezet).
    fig.update_layout(barmode="stack", xaxis_title=None, yaxis_title=y_title, legend_title_text=None, separators=",.")
    top = float(data[total_key].max()) if len(data) else 0
    fig.update_yaxes(tickformat=",.0f" if top >= 10000 else ".0f", tickprefix=prefix)
    # Het totaal (hele staaf) boven elke staaf.
    fig.add_scatter(
        x=list(data["week"]), y=[float(v) for v in data[total_key]], mode="text",
        text=[prefix + _nl_number(v) for v in data[total_key]],
        textposition="top center", showlegend=False, hoverinfo="skip",
    )
    return fig


try:
    weekly_snapshots = get_weekly_stock_snapshots()
    weekly_error = None
    if weekly_snapshots:
        # Laatste staaf = de stand van nu (ververst elke paar minuten); een meting uit de huidige maand maakt daar plaats voor.
        try:
            live = get_current_stock_snapshot()
            this_month = (live["measured_on"].year, live["measured_on"].month)
            weekly_snapshots = [r for r in weekly_snapshots
                                if (r["measured_on"].year, r["measured_on"].month) != this_month] + [live]
        except Exception:
            pass
except Exception as error:
    weekly_snapshots = None
    weekly_error = type(error).__name__

week_left, week_right = st.columns(2)
with week_left:
    st.subheader("Verkoopvoorraad, aantallen")
    if weekly_error:
        st.info(f"De wekelijkse metingen konden nu niet worden opgevraagd ({weekly_error}).")
    elif not weekly_snapshots:
        st.info("Nog geen metingen. De eerste komt aan het einde van de maand, of start de workflow 'Maandelijkse voorraadmeting' handmatig.")
    else:
        st.plotly_chart(_weekly_stock_chart(weekly_snapshots, "bw_titles", "bol_titles", "aantal boeken"), width="stretch")
        st.caption("Titels met voorraad boven 0. Het donkere deel staat ook op Bol. De laatste staaf is de stand na de laatste sync, de andere zijn metingen aan het einde van iedere maand.")
with week_right:
    st.subheader("Verkoopvoorraad, in waarde")
    if weekly_error:
        st.info(f"De wekelijkse metingen konden nu niet worden opgevraagd ({weekly_error}).")
    elif not weekly_snapshots:
        st.info("Nog geen metingen.")
    else:
        st.plotly_chart(_weekly_stock_chart(weekly_snapshots, "bw_value", "bol_value", "verkoopwaarde (€)", money=True), width="stretch")
        st.caption("Gebaseerd op de prijzen op Boekwinkeltjes en de webshop, van alle boeken met voorraad boven 0. Het donkere deel staat ook op Bol.")

st.divider()

# ---------- Bruto omzet per maand en platform ----------

# Kleuren als de bolletjes in "Laatste orders": Boekwinkeltjes geel, Bol blauw, eigen webshop paars.
PLATFORM_COLORS = {"Boekwinkeltjes": "#F5C518", "Bol": "#0068C9", "Webshop": "#8E5BD9"}
# Orders die niet tot omzet leiden: geannuleerd, geen reactie, niet te vinden, retour.
NO_REVENUE_STATUSES = {"CANCELLED", "CANCELLATION_REQUESTED", "no_response", "sold_or_unavailable", "return_received"}


def _gross_revenue_chart(orders_df, history=None, months=12):
    """
    Gestapelde staaf per maand: bruto boekprijs (zonder verzendkosten) per platform, met het maandtotaal erboven.
    history = aangeleverde bedragen uit het verleden (month, platform, amount): voor zo'n maand en platform geldt dat bedrag
    in plaats van de som van de orders. months=None toont alle maanden.
    """
    df = orders_df[~orders_df["status"].isin(NO_REVENUE_STATUSES)].dropna(subset=["order_date"]).copy()
    sums = {}
    if not df.empty:
        df["maand"] = df["order_date"].dt.to_period("M")
        df["platform"] = df["platform"].fillna("BW").map({"BW": "Boekwinkeltjes", "Bol": "Bol", "Webshop": "Webshop"}).fillna("Boekwinkeltjes")
        sums = {k: float(v) for k, v in df.groupby(["maand", "platform"])["book_price"].sum().fillna(0).items()}
    if history is not None and not history.empty:
        for _, h in history.iterrows():
            try:
                period = pd.Period(str(h["month"]), freq="M")
            except Exception:
                continue
            name = {"BW": "Boekwinkeltjes", "Bol": "Bol", "Webshop": "Webshop"}.get(h["platform"], h["platform"])
            sums[(period, name)] = float(h["amount"])
    if not sums:
        return None
    this_month = pd.Period(dt.datetime.now(AMSTERDAM_TZ).strftime("%Y-%m"), freq="M")
    first, last = min(k[0] for k in sums), max(max(k[0] for k in sums), this_month)
    all_months = pd.period_range(start=first, end=last, freq="M") if months is None else pd.period_range(end=last, periods=months, freq="M")
    labels = [p.strftime("%m-%Y") for p in all_months]
    long_rows, totals = [], []
    for period, label in zip(all_months, labels):
        total = 0.0
        for platform in PLATFORM_COLORS:
            value = round(sums.get((period, platform), 0.0))
            total += value
            long_rows.append({"maand": label, "platform": platform, "omzet": value, "label": "€ " + _nl_number(value) if value else ""})
        totals.append(total)
    fig = px.bar(pd.DataFrame(long_rows), x="maand", y="omzet", color="platform", text="label",
                 category_orders={"maand": labels, "platform": list(PLATFORM_COLORS)}, color_discrete_map=PLATFORM_COLORS)
    fig.update_traces(texttemplate="%{text}", hovertemplate="%{x}<br>%{fullData.name}: %{text}<extra></extra>")
    # Legenda van onder naar boven in dezelfde volgorde als de blokken in de staaf (Boekwinkeltjes onderaan, Webshop bovenaan).
    fig.update_layout(barmode="stack", xaxis_title=None, yaxis_title="bruto omzet (€)", legend_title_text=None, separators=",.",
                      legend=dict(traceorder="reversed"))
    fig.update_yaxes(tickformat=",.0f" if max(totals) >= 10000 else ".0f", tickprefix="€ ")
    fig.add_scatter(x=labels, y=totals, mode="text", text=["€ " + _nl_number(t) for t in totals],
                    textposition="top center", showlegend=False, hoverinfo="skip")
    return fig


def _daily_sales_chart(orders_df, days=180):
    """
    Gestapelde staaf per dag: aantal verkochte boeken (orders) per platform over de laatste `days` dagen, in dezelfde kleuren
    en volgorde als de omzet per maand. Geannuleerde orders, orders zonder reactie en retouren tellen niet mee.
    Met een groene lijn bij 10 verkopen per dag.
    """
    df = orders_df[~orders_df["status"].isin(NO_REVENUE_STATUSES)].dropna(subset=["order_date"]).copy()
    if df.empty:
        return None
    dates = pd.to_datetime(df["order_date"])
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_convert(AMSTERDAM_TZ)
    df["dag"] = dates.dt.normalize().dt.tz_localize(None)
    df["platform"] = df["platform"].fillna("BW").map({"BW": "Boekwinkeltjes", "Bol": "Bol", "Webshop": "Webshop"}).fillna("Boekwinkeltjes")
    today = pd.Timestamp(dt.datetime.now(AMSTERDAM_TZ).date())
    all_days = pd.date_range(end=today, periods=days, freq="D")
    counts = df.groupby(["dag", "platform"]).size().to_dict()
    rows = [{"dag": d, "platform": pl, "aantal": int(counts.get((d, pl), 0))} for d in all_days for pl in PLATFORM_COLORS]
    fig = px.bar(pd.DataFrame(rows), x="dag", y="aantal", color="platform",
                 category_orders={"platform": list(PLATFORM_COLORS)}, color_discrete_map=PLATFORM_COLORS)
    fig.update_traces(hovertemplate="%{x|%d-%m-%Y}<br>%{fullData.name}: %{y}<extra></extra>")
    fig.update_layout(barmode="stack", xaxis_title=None, yaxis_title="verkochte boeken per dag", legend_title_text=None,
                      legend=dict(traceorder="reversed"), bargap=0.15)
    fig.update_xaxes(tickformat="%d-%m", dtick="M1" if days > 120 else 7 * 86400000)
    fig.add_hline(y=10, line_dash="solid", line_color="green", line_width=2, annotation_text="10", annotation_position="top left",
                  annotation_font_color="green")
    return fig


if can_see_finance:
    st.subheader("Bruto omzet per maand")
    try:
        revenue_history = load_revenue_history()
    except Exception:
        revenue_history = None   # een ontbrekende of onleesbare geschiedenis mag de grafiek nooit stoppen
    revenue_period = st.radio("Periode omzet", ["Laatste 12 maanden", "Alles"], index=0, horizontal=True,
                              key="revenue_period", label_visibility="collapsed")
    revenue_fig = _gross_revenue_chart(orders, revenue_history, months=12 if revenue_period == "Laatste 12 maanden" else None)
    if revenue_fig is None:
        st.info("Nog geen orders om te tonen.")
    else:
        st.plotly_chart(revenue_fig, width="stretch")
        st.caption("Prijs van de boeken per orderdatum, zonder verzendkosten en zonder aftrek van commissie of andere kosten. "
                   "Geannuleerde orders, orders zonder reactie en retouren tellen niet mee. Voor maanden met een aangeleverd bedrag "
                   "uit het verleden geldt dat bedrag. De laatste staaf is de lopende maand.")
    st.subheader("Verkopen per dag (laatste 180 dagen)")
    daily_fig = _daily_sales_chart(orders, days=180)
    if daily_fig is None:
        st.info("Nog geen orders om te tonen.")
    else:
        st.plotly_chart(daily_fig, width="stretch")
        st.caption("Aantal verkochte boeken (orders) per dag en platform. Geannuleerde orders, orders zonder reactie en retouren tellen niet mee. "
                   "De groene lijn staat bij 10 verkochte boeken per dag, het verwachte minimum.")
    st.divider()

# ---------- Categorie-overzicht + Boeken per uitgever ----------

left2, right2 = st.columns(2)

with left2:
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

with right2:
    st.subheader("Boeken per uitgever (top 20)")
    if not books.empty:
        pub_series = books["publisher_name"].fillna("").astype(str).str.strip()
        pub_counts = pub_series[pub_series != ""].value_counts().head(20).reset_index()
        pub_counts.columns = ["uitgever", "aantal boeken"]
        fig = px.bar(pub_counts, x="uitgever", y="aantal boeken")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Nog geen boeken om te tonen.")

st.divider()

# ---------- Supabase-opslag + grootste objecten ----------

left_db, right_db = st.columns(2)

with left_db:
    st.subheader("Supabase-opslag")
    DB_LIMIT_MB = 8192.0  # Supabase Pro: 8 GB; houd gelijk aan STORAGE_LIMIT_MB in sync.py
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

with right_db:
    st.subheader("Grootste objecten in de database")
    try:
        _obj_rows, _obj_total = get_largest_objects(8)
        _obj_df = pd.DataFrame(
            [
                {
                    "Object": name,
                    "Grootte": f"{size / (1024 * 1024):.2f} MB".replace(".", ","),
                    "Aandeel": f"{size / _obj_total * 100:.1f}%".replace(".", ",") if _obj_total else "–",
                }
                for name, size in _obj_rows
            ]
        )
        st.dataframe(_obj_df, hide_index=True, width="stretch")
        st.caption("Tabellen inclusief indexen en bijbehorende opslag; het aandeel is van de hele database.")
    except Exception as error:
        st.info(f"De grootste objecten konden nu niet worden opgevraagd ({type(error).__name__}).")

# ---------- Zoekverkeer Google (Search Console) ----------

st.divider()


@st.fragment
def search_console_section():
    st.subheader("Zoekverkeer in Google voor webshop")
    period = st.radio("Periode", list(GSC_PERIODS), index=list(GSC_PERIODS).index("28 dagen"), horizontal=True, key="gsc_period", label_visibility="collapsed")
    try:
        data = get_search_console_data(period)
    except Exception as error:
        st.info(f"Het zoekverkeer kon nu niet worden opgevraagd ({error}).")
        return
    if data is None:
        st.info("Nog niet gekoppeld. Zet in de secrets GSC_SERVICE_ACCOUNT (de sleutel van het serviceaccount) en GSC_SITE_URL "
                "(bijvoorbeeld sc-domain:severein.nl).")
        return
    if data.empty:
        st.info("Nog geen gegevens voor deze periode.")
        return
    clicks = float(data["clicks"].sum())
    impressions = float(data["impressions"].sum())
    ctr = clicks / impressions if impressions else 0.0
    position = float((data["position"] * data["impressions"]).sum() / impressions) if impressions else 0.0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Klikken", _nl_number(clicks))
    m2.metric("Impressies", _nl_number(impressions))
    m3.metric("CTR", f"{ctr * 100:.1f}%".replace(".", ","))
    m4.metric("Gemiddelde positie", f"{position:.1f}".replace(".", ","))
    fig = go.Figure()
    # Zelfde blauwtinten als de andere grafieken: licht = impressies, donker = CTR (beide lijnen).
    fig.add_scatter(x=data["moment"], y=data["impressions"], name="Impressies", mode="lines+markers",
                    line=dict(color="#83C9FF", width=3),
                    hovertemplate="%{x}<br>Impressies: %{y}<extra></extra>")
    fig.add_scatter(x=data["moment"], y=(data["ctr"] * 100).round(2), name="CTR (%)", mode="lines+markers",
                    line=dict(color="#0068C9", width=3), yaxis="y2",
                    hovertemplate="%{x}<br>CTR: %{y:.1f}%<extra></extra>")
    fig.update_layout(
        yaxis=dict(title="impressies", rangemode="tozero"),
        yaxis2=dict(title="CTR (%)", overlaying="y", side="right", rangemode="tozero", showgrid=False, ticksuffix="%"),
        xaxis_title=None, legend_title_text=None, legend=dict(orientation="h", y=1.12), separators=",.",
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch")
    st.caption("Bron: Google Search Console (zoektype Web). De cijfers van de laatste uren zijn nog voorlopig en worden elk half uur ververst.")

    # De 20 zoekopdrachten met de meeste impressies: laatste 24 uur en laatste 3 maanden (los van de gekozen periode hierboven).
    q_left, q_right = st.columns(2)
    for column, query_period, title in ((q_left, "24 uur", "Top 20 zoekopdrachten, laatste 24 uur"),
                                        (q_right, "3 maanden", "Top 20 zoekopdrachten, laatste 3 maanden")):
        with column:
            st.markdown(f"**{title}**")
            try:
                queries = get_search_console_queries(query_period)
            except Exception as error:
                st.info(f"De zoekopdrachten konden nu niet worden opgevraagd ({error}).")
                continue
            if queries is None or queries.empty:
                st.caption("Nog geen zoekopdrachten voor deze periode.")
                continue
            st.dataframe(
                queries.rename(columns={"query": "Zoekopdracht", "impressions": "Impressies", "clicks": "Klikken"}),
                hide_index=True, width="stretch",
            )
    st.caption("Google laat zeldzame zoekopdrachten weg (privacy), dus de lijst is nooit helemaal compleet.")


search_console_section()


# ---------- Betalingen volgens Mollie ----------

if can_see_finance:
    st.divider()


@st.fragment
def mollie_section():
    st.subheader("Betalingen in Mollie")
    try:
        payments = get_mollie_payments()
    except Exception as error:
        st.info(f"De betalingen konden nu niet worden opgevraagd bij Mollie ({error}).")
        return
    if payments is None:
        st.info("Nog niet gekoppeld. Zet in de secrets MOLLIE_API_KEY (dezelfde Mollie-sleutel als WEBSHOP_MOLLIE_API_KEY in GitHub).")
        return
    if payments.empty:
        st.info("Nog geen betalingen.")
        return
    paid = payments[payments["paid"]]
    m1, m2 = st.columns(2)
    m1.metric("Betaald in deze lijst", _euro(paid["bedrag"].sum()))
    m2.metric("Aantal betaalde betalingen", len(paid))
    shown = pd.DataFrame({
        "Datum": payments["moment"].apply(lambda t: t.strftime("%d-%m-%Y %H:%M") if pd.notna(t) else ""),
        "Bedrag": payments["bedrag"].apply(_euro),
        "Status": payments["status"],
        "Omschrijving": payments["omschrijving"],
        "Methode": payments["methode"],
        "Betaler": payments["betaler"],
    })
    st.dataframe(shown, hide_index=True, width="stretch")
    st.caption("Bron: Mollie, de laatste 100 betalingen (nieuwste eerst), elke 5 minuten ververst. "
               f"Testbestellingen vóór webshopbestelling #{WEBSHOP_FIRST_REAL_ORDER} worden niet getoond.")


if can_see_finance:
    mollie_section()

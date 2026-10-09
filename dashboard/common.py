"""
common.py — Gedeelde functies voor alle dashboard-pagina's (app.py + pages/*.py).
"""

import os
import time
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # leest .env, ook als die in de bovenliggende (project)map staat

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")


import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
import streamlit as st
from dotenv import load_dotenv

from categories import CATEGORY1_OPTIONS, CATEGORY2_OPTIONS
from cover_check import check_cover_image

load_dotenv()  # leest .env, ook als die in de bovenliggende (project)map staat

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")


def na(value):
    """Toont 'N.v.t.' voor None/NaN; anders de waarde zelf ongewijzigd."""
    if value is None:
        return "N.v.t."
    try:
        if pd.isna(value):
            return "N.v.t."
    except (TypeError, ValueError):
        pass
    return value


def _dict_connect():
    """Verbinding die rijen als dict teruggeeft (handig voor tabellen met veel kolommen)."""
    return psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)


# ---------- Authenticatie ----------

def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return digest, salt


def get_user_count():
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app_users")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _get_user_by_short_name(short_name):
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM app_users WHERE short_name = %(s)s AND is_active = TRUE",
                {"s": short_name},
            )
            return cur.fetchone()
    finally:
        conn.close()


def _attempt_login(short_name, password):
    user = _get_user_by_short_name(short_name)
    if not user:
        return None
    digest, _ = _hash_password(password, user["password_salt"])
    if digest != user["password_hash"]:
        return None
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_users SET last_login_at = now() WHERE id = %(id)s", {"id": user["id"]}
            )
        conn.commit()
    finally:
        conn.close()
    return dict(user)


def update_last_seen(short_name):
    if not short_name or short_name == "onbekend":
        return
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_users SET last_seen_at = now() WHERE short_name = %(s)s", {"s": short_name}
            )
        conn.commit()
    finally:
        conn.close()


def get_active_users(exclude_short_name, window_minutes=5):
    """Korte namen van andere gebruikers die de afgelopen 'window_minutes' nog een pagina laadden."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT short_name FROM app_users
                WHERE short_name != %(exclude)s
                  AND last_seen_at IS NOT NULL
                  AND last_seen_at > now() - (%(minutes)s || ' minutes')::interval
                ORDER BY short_name
                """,
                {"exclude": exclude_short_name or "", "minutes": window_minutes},
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _join_dutch_list(names):
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " en " + names[-1]


def require_login():
    """
    Toont een inlogscherm als er nog niemand is ingelogd (of nog geen gebruikers
    bestaan — dan mag iedereen erbij, tot de eerste gebruiker is aangemaakt via
    Gebruikersbeheer). Geeft de ingelogde gebruiker (dict) terug, of None in de
    opstartfase zonder gebruikers.
    """
    if st.session_state.get("user"):
        user = st.session_state["user"]
        update_last_seen(user["short_name"])
        with st.sidebar:
            st.caption(f"Ingelogd als **{user['full_name']}**")
            other_active = get_active_users(user["short_name"])
            if other_active:
                st.caption(f"Ook ingelogd: {_join_dutch_list(other_active)}")
            if st.button("Uitloggen", key="logout_button"):
                del st.session_state["user"]
                st.rerun()
        return user

    if get_user_count() == 0:
        st.info(
            "Er zijn nog geen gebruikers aangemaakt. Ga naar 'Gebruikersbeheer' om de "
            "eerste gebruiker aan te maken — tot die tijd is de app open voor iedereen "
            "met de link."
        )
        return None

    st.title("🔒 Inloggen")
    with st.form("login_form"):
        short_name = st.text_input("Gebruikersnaam")
        password = st.text_input("Wachtwoord", type="password")
        submitted = st.form_submit_button("Inloggen")
    if submitted:
        user = _attempt_login(short_name.strip(), password)
        if user:
            st.session_state["user"] = user
            st.rerun()
        else:
            st.error("Onjuiste gebruikersnaam of wachtwoord.")
    st.stop()


def current_user_short_name():
    user = st.session_state.get("user")
    return user["short_name"] if user else "onbekend"


# ---------- Gebruikersbeheer ----------

def get_users_with_stats():
    """Lijst van gebruikers met hun leesbare wachtwoord, rol, laatste locatie, laatste login en activiteitentellingen."""
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id, u.full_name, u.short_name, u.email, u.password_plain, u.role,
                    u.last_location, u.last_login_at,
                    COALESCE(SUM(CASE WHEN l.action = 'created' THEN 1 ELSE 0 END), 0) AS aantal_toegevoegd,
                    COALESCE(SUM(CASE WHEN l.action = 'edited' THEN 1 ELSE 0 END), 0) AS aantal_aangepast
                FROM app_users u
                LEFT JOIN book_activity_log l ON l.user_short_name = u.short_name
                GROUP BY u.id, u.full_name, u.short_name, u.email, u.password_plain, u.role,
                    u.last_location, u.last_login_at
                ORDER BY u.full_name ASC
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def create_user(full_name, short_name, email, password):
    """Nieuwe gebruikers krijgen altijd de rol 'gebruiker' — 'beheerder' wordt niet via de UI uitgedeeld."""
    digest, salt = _hash_password(password)
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_users (full_name, short_name, email, password_hash, password_salt, password_plain, role)
                VALUES (%(full_name)s, %(short_name)s, %(email)s, %(hash)s, %(salt)s, %(plain)s, 'gebruiker')
                """,
                {
                    "full_name": full_name,
                    "short_name": short_name,
                    "email": email,
                    "hash": digest,
                    "salt": salt,
                    "plain": password,
                },
            )
        conn.commit()
    finally:
        conn.close()


def update_user(user_id, full_name, short_name, email, new_password=None, role=None):
    """'role' alleen meegeven als een beheerder de rol mag/wil wijzigen; anders blijft de rol ongemoeid."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            if new_password:
                digest, salt = _hash_password(new_password)
                cur.execute(
                    """
                    UPDATE app_users
                    SET full_name = %(full_name)s, short_name = %(short_name)s, email = %(email)s,
                        password_hash = %(hash)s, password_salt = %(salt)s, password_plain = %(plain)s
                        {role_clause}
                    WHERE id = %(id)s
                    """.format(role_clause=", role = %(role)s" if role is not None else ""),
                    {
                        "full_name": full_name,
                        "short_name": short_name,
                        "email": email,
                        "hash": digest,
                        "salt": salt,
                        "plain": new_password,
                        "role": role,
                        "id": user_id,
                    },
                )
            else:
                cur.execute(
                    """
                    UPDATE app_users
                    SET full_name = %(full_name)s, short_name = %(short_name)s, email = %(email)s
                        {role_clause}
                    WHERE id = %(id)s
                    """.format(role_clause=", role = %(role)s" if role is not None else ""),
                    {"full_name": full_name, "short_name": short_name, "email": email, "role": role, "id": user_id},
                )
        conn.commit()
    finally:
        conn.close()


def get_user_last_location(short_name):
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_location FROM app_users WHERE short_name = %(s)s", {"s": short_name}
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _update_last_location(user_short_name, location):
    if not user_short_name or not location:
        return
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_users SET last_location = %(loc)s WHERE short_name = %(s)s",
                {"loc": location, "s": user_short_name},
            )
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id):
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_users WHERE id = %(id)s", {"id": user_id})
        conn.commit()
    finally:
        conn.close()


# ---------- Activiteitenlog per boek ----------

FIELD_LABELS = {
    "title": "Titel",
    "author": "Auteur",
    "publisher": "Uitgever",
    "price": "Prijs Boekwinkeltjes",
    "shipping_cost": "Verzendkosten Boekwinkeltjes",
    "shipping_cost_bol": "Prijs Bol",
    "shipping_format": "Verzendformaat",
    "amount": "Voorraad",
    "ean": "ISBN",
    "location": "Locatie",
    "category1": "Categorie 1",
    "category2": "Categorie 2",
    "category3": "Categorie 3",
    "language": "Taal",
    "short_description": "Bijzonderheden",
    "long_description": "Meer info",
    "push_enabled": "Synchronisatie",
    "queued": "Wachtrij",
    "length_cm": "Lengte (cm)",
    "width_cm": "Breedte (cm)",
    "thickness_cm": "Dikte (cm)",
}


def _normalize_for_compare(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return str(value).strip()


def log_book_activity(user_short_name, book_id, action, changed_fields=None):
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO book_activity_log (book_id, user_short_name, action, changed_fields)
                VALUES (%(book_id)s, %(user)s, %(action)s, %(changed)s)
                """,
                {
                    "book_id": int(book_id),
                    "user": user_short_name,
                    "action": action,
                    "changed": "|".join(changed_fields) if changed_fields else None,
                },
            )
        conn.commit()
    finally:
        conn.close()


def get_book_activity(book_id):
    """Activiteit voor één boek, meest recent eerst."""
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_short_name, action, changed_fields, occurred_at FROM book_activity_log
                WHERE book_id = %(book_id)s
                ORDER BY occurred_at DESC
                """,
                {"book_id": int(book_id)},
            )
            return cur.fetchall()
    finally:
        conn.close()


def render_logo(path):
    """Toont het logo linksboven in de zijbalk, groter dan Streamlit's standaard (24px)."""
    st.logo(str(path))
    st.markdown(
        """
        <style>
        img[data-testid="stLogo"] {
            height: 63px !important;
            width: auto !important;
        }
        div[data-testid="stSidebarHeader"] img {
            height: 63px !important;
            width: auto !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _get_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


def trigger_github_workflow(workflow_file, ref="main"):
    """
    Start een GitHub Actions workflow op afstand (workflow_dispatch) via de
    GitHub REST API. Vereist GITHUB_REPO ('eigenaar/repo-naam') en GITHUB_TOKEN
    (personal access token met workflow-rechten) in secrets/.env.
    Geeft (True, bericht) bij succes, (False, foutbericht) bij een probleem.
    """
    repo = _get_secret("GITHUB_REPO")
    token = _get_secret("GITHUB_TOKEN")

    if not repo or not token:
        return False, "GITHUB_REPO en/of GITHUB_TOKEN zijn niet ingesteld in de secrets."

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": ref},
            timeout=15,
        )
    except requests.RequestException as e:
        return False, f"Netwerkfout: {e}"

    # GitHub documenteert 204 (geen inhoud) als antwoord. In december 2025 gaf het een paar uur een 200 met een
    # beschrijving van de gestarte run terug; dan was de workflow ook gewoon gestart. Beide tellen dus als gelukt.
    if resp.status_code in (200, 204):
        return True, "Gestart! Dit duurt een paar minuten — check het Actions-tabblad op GitHub voor de voortgang."
    return False, f"GitHub gaf een fout terug ({resp.status_code}): {resp.text[:300]}"


def get_db_url():
    try:
        if "SUPABASE_DB_URL" in st.secrets:
            return st.secrets["SUPABASE_DB_URL"]
    except Exception:
        pass  # geen secrets.toml aanwezig (normaal bij lokaal draaien) -> geen probleem
    return os.environ["SUPABASE_DB_URL"]


@st.cache_data(ttl=300)
def load_books():
    conn = psycopg2.connect(get_db_url())
    try:
        return pd.read_sql("SELECT * FROM books", conn)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_orders():
    conn = psycopg2.connect(get_db_url())
    try:
        orders = pd.read_sql("SELECT * FROM orders", conn)
    finally:
        conn.close()
    if not orders.empty:
        orders["order_date"] = pd.to_datetime(orders["order_date"], errors="coerce")
        orders["revenue"] = orders["book_price"].fillna(0) + orders["book_shipping_cost"].fillna(0)
    return orders


def load_data():
    """
    Historisch gemakslaagje: geeft (boeken, orders) samen terug. Boeken en orders
    hebben ELK hun eigen cache (load_books()/load_orders()) — zo hoeft het bewaren
    van een boekwijziging niet ook de (los daarvan onveranderde, en vaak veel
    grotere) orders-tabel opnieuw uit de database te laden.
    """
    return load_books(), load_orders()


@st.cache_data(ttl=300)
def get_all_image_urls(book_id, size="medium"):
    """Geeft alle afbeelding-URL's voor een boek terug, in volgorde (eerste = aangenomen hoofdafbeelding)."""
    column = {"large": "url_large", "medium": "url_medium", "small": "url_small"}[size]
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {column} AS url FROM book_images
                WHERE book_id = %(book_id)s AND image_id != -1
                ORDER BY position ASC
                """,
                {"book_id": int(book_id)},
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def format_isbn(value):
    """Toont 'geen' voor een ontbrekend/nul-ISBN in plaats van een kale 0."""
    if value is None or pd.isna(value):
        return "geen"
    s = str(value).strip()
    if s in ("", "0", "0.0"):
        return "geen"
    return s


def format_price(value):
    """Formatteert een bedrag als '12,50' — komma als decimaalteken, altijd 2 decimalen."""
    if value is None or pd.isna(value):
        return "–"
    return f"{float(value):.2f}".replace(".", ",")


ORDER_STATUS_LABELS = {
    "new_order": "Nieuwe bestelling",
    "wait_for_customer": "Wachten op klant",
    "wait_for_payment": "Wachten op betaling",
    "wait_for_shipping": "Wachten op verzending",
    "wait_for_pickup": "Wachten op afhalen",
    "shipped_wait_for_payment": "Verstuurd, wachten op betaling",
    "shipped_paid": "Verstuurd en betaald",
    "picked_up_paid": "Afgehaald en betaald",
    "sold_or_unavailable": "Onvindbaar",
    "no_response": "Geen reactie",
    "paymentlink_mailed": "Betaallink verstuurd",
    "return_received": "Retour ontvangen",
    "lost_in_transit": "Onderweg zoekgeraakt",
    # Bol-orderstatussen, naar dezelfde Nederlandse termen als Boekwinkeltjes
    "OPEN": "Wachten op verzending",
    "HANDLED": "Verstuurd en betaald",
    "SHIPPED": "Verstuurd en betaald",
    "CANCELLED": "Geannuleerd",
    "CANCELLATION_REQUESTED": "Annuleringsaanvraag",
}


def format_order_status(value):
    """Zet een ruwe orderstatuscode om naar een net Nederlands woord/zin."""
    if not value or pd.isna(value):
        return "Onbekend"
    return ORDER_STATUS_LABELS.get(value, value)


PAYMENT_STATUS_LABELS = {
    "paid": "Betaald",
    "open": "Niet betaald",
    "refunded": "Terugbetaald",
}


def format_payment_status(value):
    """Zet de ruwe betaalstatus (paid/open/refunded) om naar een net Nederlands woord."""
    if not value or pd.isna(value):
        return "Onbekend"
    return PAYMENT_STATUS_LABELS.get(value, value)


def format_datetime_nl(value):
    """
    Zet een (UTC-)timestamp om naar Europese tijd (CEST in de zomer, CET in de
    winter) en formatteert 'm als 'dd-mm-jjjj uu:mm:ss'.
    """
    if value is None or pd.isna(value):
        return "–"
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts = ts.tz_convert(AMSTERDAM_TZ)
    return ts.strftime("%d-%m-%Y %H:%M:%S")


def format_order_datetime(value):
    """
    Besteldatum als 'dd-mm-jjjj uu:mm:ss'. Heeft de waarde een tijdzone, dan wordt die omgezet naar Nederlandse tijd;
    heeft hij geen tijdzone (zoals Boekwinkeltjes die meestal geeft), dan wordt de tijd ongewijzigd getoond.
    """
    if value is None:
        return "–"
    try:
        if pd.isna(value):
            return "–"
    except (TypeError, ValueError):
        pass
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return str(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(AMSTERDAM_TZ)
    return ts.strftime("%d-%m-%Y %H:%M:%S")


def set_main_image_url(book_id, url):
    """Zet het main_image_url-veld van een boek handmatig (overschrijft de og:image-detectie)."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE books SET main_image_url = %(url)s WHERE id = %(id)s",
                {"url": url, "id": int(book_id)},
            )
        conn.commit()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_known_publishers():
    """Alfabetisch gesorteerde lijst van bekende uitgevers (ruwe waarde)."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM known_publishers ORDER BY value ASC")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def add_known_publisher(value):
    """Voegt een nieuwe uitgever toe aan de bekende-lijst (negeert duplicaten)."""
    value = (value or "").strip()
    if not value:
        return
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO known_publishers (value) VALUES (%(value)s) ON CONFLICT (value) DO NOTHING",
                {"value": value},
            )
        conn.commit()
    finally:
        conn.close()


def replace_known_publishers(values):
    """
    Vervangt de volledige bekende-uitgeverslijst door 'values' (dubbelen en lege
    waarden worden genegeerd). Gebruikt door de beheerpagina om in één keer
    toevoegingen, wijzigingen en verwijderingen door te voeren.
    """
    cleaned = sorted({v.strip() for v in values if v and v.strip()})
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM known_publishers")
            if cleaned:
                cur.executemany(
                    "INSERT INTO known_publishers (value) VALUES (%s)",
                    [(v,) for v in cleaned],
                )
        conn.commit()
    finally:
        conn.close()


EDITABLE_BOOK_FIELDS = [
    "title",
    "author",
    "publisher",
    "price",
    "shipping_cost",
    "shipping_cost_bol",
    "shipping_format",
    "amount",
    "location",
    "category1",
    "category2",
    "category3",
    "language",
    "ean",
    "short_description",
    "long_description",
    "push_enabled",
    "queued",
    "length_cm",
    "width_cm",
    "thickness_cm",
]


# Verzendformaat: het verplichte Boekwinkeltjes-veld 'shippingFormat'. Waarden zoals in hun eigen keuzelijst.
SHIPPING_FORMAT_LABELS = {
    0: "Alleen afhalen mogelijk",
    1: "Brievenbuspakje",
    2: "Klein pakket",
    3: "Normaal pakket",
    4: "Groot of zwaar pakket",
}


def shipping_format_label(value):
    """1 -> 'Brievenbuspakje'; leeg of onbekend -> 'nog niet ingesteld'."""
    try:
        if value is None or pd.isna(value):
            return "nog niet ingesteld"
        return SHIPPING_FORMAT_LABELS.get(int(value), f"onbekend ({int(value)})")
    except (TypeError, ValueError):
        return "nog niet ingesteld"


def shipping_format_for_new_cost(cost, briefpost, pakketpost):
    """
    Het verzendformaat dat bij een verzendkostenbedrag hoort, voor boeken die we zelf aanmaken of wijzigen:
    precies de briefpost-kosten -> Brievenbuspakje (1), precies de pakketpost-kosten -> Normaal pakket (3).
    Een ander bedrag (vrije invoer): niet hoger dan briefpost -> Brievenbuspakje, hoger -> Normaal pakket.
    Zonder bedrag -> None. Een boek zonder verzendformaat kan niet worden verkocht, dus liever een
    voorspelbare keuze dan een leeg veld.
    """
    try:
        if cost is None or pd.isna(cost):
            return None
        cost = round(float(cost), 2)
    except (TypeError, ValueError):
        return None
    if cost == round(float(pakketpost), 2):
        return 3
    if cost <= round(float(briefpost), 2):
        return 1
    return 3


def count_books_without_shipping_format(books):
    """
    Hoeveel boeken (met echte Boekwinkeltjes-id, waarvan de synchronisatie aanstaat) hebben nog geen
    verzendformaat? Die kunnen op Boekwinkeltjes niet worden verkocht. Ontbreekt de kolom nog helemaal
    (de sync heeft hem nog niet aangemaakt), dan tellen alle boeken mee.
    """
    if books is None or len(books) == 0:
        return 0
    eligible = books[books["id"] > 0]
    if "pending_create" in eligible.columns:
        # Zelfde regel als de workflow: boeken die nog op hun eerste aanmaak bij Boekwinkeltjes wachten tellen niet mee.
        eligible = eligible[~eligible["pending_create"].fillna(False).astype(bool)]
    if "amount" in eligible.columns:
        # Verkochte boeken (voorraad 0) staan niet meer te koop; een ontbrekend verzendformaat is daar geen probleem.
        eligible = eligible[eligible["amount"].fillna(0) > 0]
    if "push_enabled" in eligible.columns:
        eligible = eligible[eligible["push_enabled"].fillna(True).astype(bool)]
    if "shipping_format" not in eligible.columns:
        return int(len(eligible))
    return int(eligible["shipping_format"].isna().sum())


_SHIPPING_FORMAT_COLUMN_CHECKED = False


def _ensure_shipping_format_column(cur):
    """Zorgt dat books.shipping_format bestaat (één keer per programma-run), ook als de sync dat nog niet deed."""
    global _SHIPPING_FORMAT_COLUMN_CHECKED
    if not _SHIPPING_FORMAT_COLUMN_CHECKED:
        # Eerst kijken of de kolom er al is: een ALTER TABLE vraagt altijd een exclusief slot op de tabel en
        # kan dan blijven wachten achter een lopende sync.
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'books' AND column_name = 'shipping_format'"
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE books ADD COLUMN IF NOT EXISTS shipping_format INTEGER")
        _SHIPPING_FORMAT_COLUMN_CHECKED = True


def save_book_edits(book_id, fields, user_short_name=None, previous_values=None):
    """
    Slaat handmatige wijzigingen aan een bestaand boek op in Supabase en zet
    pending_push = TRUE, zodat de eerstvolgende sync (of de knop 'Sync boeken
    nu') ze naar Boekwinkeltjes doorstuurt.
    'fields' is een dict met alleen de kolommen uit EDITABLE_BOOK_FIELDS die je
    wilt bijwerken. 'previous_values' zijn (optioneel) de waarden zoals de
    gebruiker ze zag toen het formulier opende — als je die meegeeft, wordt
    daartegen vergeleken (in plaats van een verse databasequery), zodat een
    tussentijdse sync nooit een vals-positieve wijziging kan opleveren.
    """
    fields = {k: v for k, v in fields.items() if k in EDITABLE_BOOK_FIELDS}
    if not fields:
        return
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            _ensure_shipping_format_column(cur)
            cols = list(fields.keys())
            if previous_values is not None:
                current_values = {col: previous_values.get(col) for col in cols}
            else:
                # Terugvaloptie: een verse databasewaarde ophalen om tegen te vergelijken
                cur.execute(f"SELECT {', '.join(cols)} FROM books WHERE id = %(id)s", {"id": int(book_id)})
                current_row = cur.fetchone()
                current_values = dict(zip(cols, current_row)) if current_row else {}

            changed_cols = [
                col
                for col in cols
                if _normalize_for_compare(current_values.get(col)) != _normalize_for_compare(fields[col])
            ]

            # Het verzendformaat volgt de verzendkosten: als die veranderen, of als het boek nog geen
            # verzendformaat heeft (zonder kan het niet worden verkocht). Een verzendformaat dat al staat
            # en waarvan de kosten niet veranderen, blijft zoals het is (bijv. 'Klein pakket' dat op
            # Boekwinkeltjes zelf is gekozen).
            db_fields = dict(fields)
            if "shipping_cost" in fields and "shipping_format" not in fields:
                cur.execute("SELECT shipping_format FROM books WHERE id = %(id)s", {"id": int(book_id)})
                row = cur.fetchone()
                current_format = row[0] if row else None
                if "shipping_cost" in changed_cols or current_format is None:
                    new_format = shipping_format_for_new_cost(fields["shipping_cost"], *get_shipping_costs())
                    if new_format is not None:
                        db_fields["shipping_format"] = new_format

            set_clause = ", ".join(f"{col} = %({col})s" for col in db_fields)
            cur.execute(
                f"""
                UPDATE books
                SET {set_clause}, pending_push = TRUE, local_updated_at = %(now)s
                WHERE id = %(id)s
                """,
                {**db_fields, "now": pd.Timestamp.utcnow(), "id": int(book_id)},
            )
        conn.commit()
    finally:
        conn.close()


    if user_short_name and changed_cols:
        changed_labels = [FIELD_LABELS.get(c, c) for c in changed_cols]
        log_book_activity(user_short_name, book_id, "edited", changed_fields=changed_labels)

    if user_short_name and fields.get("location"):
        _update_last_location(user_short_name, fields["location"])


def create_new_book_draft(fields, user_short_name=None):
    """
    Maakt lokaal een nieuw, nog-niet-bij-Boekwinkeltjes-bestaand boek aan met
    een tijdelijk negatief id (pending_create = TRUE). De eerstvolgende sync
    (of 'Sync boeken nu') maakt 'm echt aan en vervangt het tijdelijke id door
    het echte. Geeft het tijdelijke id terug.
    """
    fields = {k: v for k, v in fields.items() if k in EDITABLE_BOOK_FIELDS}
    # Zonder verzendformaat kan Boekwinkeltjes het boek niet verkopen: afleiden uit de verzendkosten.
    if fields.get("shipping_format") is None and fields.get("shipping_cost") is not None:
        derived_format = shipping_format_for_new_cost(fields["shipping_cost"], *get_shipping_costs())
        if derived_format is not None:
            fields["shipping_format"] = derived_format
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            if "shipping_format" in fields:
                _ensure_shipping_format_column(cur)
            cur.execute("SELECT COALESCE(MIN(id), 0) FROM books")
            row = cur.fetchone()
            lowest = row[0] if row else 0
            temp_id = min(lowest, 0) - 1

            columns = ["id", "pending_create", "pending_push", "created_via_app"] + list(fields.keys())
            placeholders = ["%(id)s", "TRUE", "FALSE", "TRUE"] + [f"%({k})s" for k in fields.keys()]
            cur.execute(
                f"INSERT INTO books ({', '.join(columns)}) VALUES ({', '.join(placeholders)})",
                {**fields, "id": temp_id},
            )
        conn.commit()
    finally:
        conn.close()

    if user_short_name:
        log_book_activity(user_short_name, temp_id, "created")
        if fields.get("location"):
            _update_last_location(user_short_name, fields["location"])

    return temp_id


def image_identity_urls(img):
    """
    Alle links waaraan je een foto kunt herkennen: de echte links naar Boekwinkeltjes (die blijven
    gelijk, ook als het lokale bestand wordt opgeruimd) en wat er getoond wordt (kan een
    data-URI zijn, voor oudere voorkanten die zo zijn opgeslagen).
    """
    urls = list(img.get("real_urls") or [])
    urls += [img.get("url_large"), img.get("url_medium"), img.get("url_small")]
    return [u for u in urls if u]


def star_url_for(img):
    """
    Wat in books.main_image_url hoort als je een foto met de ster aanwijst: een echte link naar
    Boekwinkeltjes, nooit een data-URI. Een data-URI is de hele afbeelding als tekst, en zet dan
    honderden kB in de boekenrij. None als de foto nog geen echte link heeft (nog niet verwerkt
    door Boekwinkeltjes); die kan dan nog niet worden aangewezen.
    """
    for url in img.get("real_urls") or []:
        if url:
            return url
    return None


def find_main_image_index(images, main_image_url):
    """Welke foto in de lijst is de hoofdafbeelding? De eerste die bij main_image_url past, anders de eerste."""
    if main_image_url:
        for i, img in enumerate(images):
            if main_image_url in image_identity_urls(img):
                return i
    return 0


@st.cache_data(ttl=300)
def get_all_images(book_id):
    """
    Geeft alle afbeeldingen van een boek terug als dicts met url_large/url_medium/url_small,
    plus 'source' ('uploaded' of 'confirmed') en 'ref_id' (nodig om de afbeelding te
    kunnen verwijderen via delete_book_image), in volgorde. Bevat zowel bevestigde
    Boekwinkeltjes-afbeeldingen (URL, of lokaal binair opgeslagen als we ze zelf hebben
    geüpload en teruggehaald) als eigen geüploade originelen die nog niet bevestigd/
    verwerkt zijn (als data-URI).
    """
    book_id = int(book_id)
    results = []
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            # Eigen geüploade originelen die nog niet (bevestigd) gepusht zijn
            cur.execute(
                """
                SELECT id, image_data, content_type, pushed_to_boekwinkeltjes FROM book_uploaded_images
                WHERE book_id = %(book_id)s
                ORDER BY position ASC, id ASC
                """,
                {"book_id": book_id},
            )
            for row_id, image_data, content_type, pushed in cur.fetchall():
                raw_bytes = bytes(image_data)
                data_uri = f"data:{content_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
                results.append(
                    {
                        "url_large": data_uri,
                        "url_medium": data_uri,
                        "url_small": data_uri,
                        "real_urls": [],  # nog niet door Boekwinkeltjes verwerkt: er is nog geen echte link
                        "source": "uploaded",
                        "ref_id": row_id,
                        "pushed": bool(pushed),
                        "is_placeholder": check_cover_image(raw_bytes) == "placeholder",
                    }
                )

            # Bevestigde afbeeldingen: lokaal binair opgeslagen (eigen upload, teruggehaald)
            # of anders de URL van Boekwinkeltjes zelf (bestaande boeken)
            cur.execute(
                """
                SELECT image_id, url_large, url_medium, url_small, image_large_data, image_medium_data
                FROM book_images
                WHERE book_id = %(book_id)s AND image_id != -1
                ORDER BY position ASC
                """,
                {"book_id": book_id},
            )
            for image_id, url_large, url_medium, url_small, large_data, medium_data in cur.fetchall():
                real_urls = [u for u in (url_large, url_medium, url_small) if u]
                if large_data or medium_data:
                    large_src = (
                        f"data:image/jpeg;base64,{base64.b64encode(bytes(large_data)).decode('ascii')}"
                        if large_data
                        else None
                    )
                    medium_src = (
                        f"data:image/jpeg;base64,{base64.b64encode(bytes(medium_data)).decode('ascii')}"
                        if medium_data
                        else None
                    )
                    results.append(
                        {
                            "url_large": large_src or medium_src,
                            "url_medium": medium_src or large_src,
                            "url_small": medium_src or large_src,
                            "real_urls": real_urls,
                            "source": "confirmed",
                            "ref_id": image_id,
                        }
                    )
                else:
                    results.append(
                        {
                            "url_large": url_large,
                            "url_medium": url_medium,
                            "url_small": url_small,
                            "real_urls": real_urls,
                            "source": "confirmed",
                            "ref_id": image_id,
                        }
                    )
    finally:
        conn.close()

    # Welke eigen upload is de hoofdfoto? De eerste in de volgorde, want die krijgt Boekwinkeltjes als eerste. Een
    # andere kiezen kan alleen zolang er nog niets van dit boek bij Boekwinkeltjes staat (geen gepushte of
    # bevestigde foto's): daarna is de eerste foto daar al bepaald.
    uploaded = [img for img in results if img["source"] == "uploaded"]
    has_confirmed = any(img["source"] == "confirmed" for img in results)
    can_be_main = bool(uploaded) and not has_confirmed and not any(img["pushed"] for img in uploaded)
    for index, img in enumerate(uploaded):
        img["can_be_main"] = can_be_main
        img["is_main"] = can_be_main and index == 0
    return results


def delete_book_image(book_id, source, ref_id):
    """Verwijdert één afbeelding van een boek — 'source' is 'uploaded' of 'confirmed'
    (zoals teruggegeven door get_all_images), 'ref_id' de bijbehorende id daaruit."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            if source == "uploaded":
                cur.execute(
                    "DELETE FROM book_uploaded_images WHERE id = %(id)s AND book_id = %(book_id)s",
                    {"id": ref_id, "book_id": int(book_id)},
                )
            else:
                cur.execute(
                    "DELETE FROM book_images WHERE book_id = %(book_id)s AND image_id = %(id)s",
                    {"book_id": int(book_id), "id": ref_id},
                )
        conn.commit()
    finally:
        conn.close()


def save_uploaded_images(book_id, images):
    """
    Slaat zelf geüploade afbeeldingen lokaal op, klaar om bij de eerstvolgende
    sync naar Boekwinkeltjes gepusht te worden.
    'images' is een lijst van dicts: {"data": bytes, "content_type": str, "is_main": bool}.
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            # Nieuwe foto's komen NA de bestaande: de volgorde (position) bepaalt welke foto Boekwinkeltjes als
            # eerste krijgt en dus als hoofdfoto toont. Zonder dit begon elke nieuwe reeks weer bij 0.
            cur.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM book_uploaded_images WHERE book_id = %(book_id)s",
                {"book_id": int(book_id)},
            )
            start_position = cur.fetchone()[0]
            for offset, img in enumerate(images):
                position = start_position + offset
                cur.execute(
                    """
                    INSERT INTO book_uploaded_images (book_id, image_data, content_type, is_main, position, pushed_to_boekwinkeltjes)
                    VALUES (%(book_id)s, %(data)s, %(ct)s, %(main)s, %(pos)s, FALSE)
                    """,
                    {
                        "book_id": int(book_id),
                        "data": psycopg2.Binary(img["data"]),
                        "ct": img["content_type"],
                        "main": img["is_main"],
                        "pos": position,
                    },
                )
        conn.commit()
    finally:
        conn.close()


def front_upload_allowed(images):
    """
    Mag er nog een voorkant worden gekozen? Alleen zolang er nog niets van dit boek bij Boekwinkeltjes staat: de eerste
    foto daar wordt de hoofdfoto en de API kan dat achteraf niet wijzigen. 'images' is de lijst van get_all_images.
    """
    for img in images:
        if img.get("source") == "confirmed" or img.get("pushed"):
            return False
    return True


def save_front_image(book_id, image):
    """
    Slaat een nieuwe voorkant op als EERSTE foto van het boek (de bestaande eigen uploads schuiven een plaats op en
    zijn geen hoofdfoto meer). Dat kan alleen zolang er nog niets van dit boek bij Boekwinkeltjes staat (geen gepushte
    of bevestigde foto's). Geeft True terug als het gelukt is, anders False (en wordt er niets gewijzigd).
    'image' is {"data": bytes, "content_type": str}.
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM book_uploaded_images WHERE book_id = %(book_id)s AND pushed_to_boekwinkeltjes = TRUE",
                {"book_id": int(book_id)},
            )
            already_pushed = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM book_images WHERE book_id = %(book_id)s AND image_id != -1",
                {"book_id": int(book_id)},
            )
            already_confirmed = cur.fetchone()[0]
            if already_pushed or already_confirmed:
                return False
            cur.execute(
                "UPDATE book_uploaded_images SET position = position + 1, is_main = FALSE WHERE book_id = %(book_id)s",
                {"book_id": int(book_id)},
            )
            cur.execute(
                """
                INSERT INTO book_uploaded_images (book_id, image_data, content_type, is_main, position, pushed_to_boekwinkeltjes)
                VALUES (%(book_id)s, %(data)s, %(ct)s, TRUE, 0, FALSE)
                """,
                {"book_id": int(book_id), "data": psycopg2.Binary(image["data"]), "ct": image["content_type"]},
            )
        conn.commit()
        return True
    finally:
        conn.close()


def order_main_first(images, main_index):
    """
    Zet de gekozen hoofdfoto vooraan en markeert alleen die als hoofdfoto. De volgorde bepaalt wat
    Boekwinkeltjes als eerste krijgt, en dus als hoofdfoto toont; de vlag is_main alleen is daarvoor niet
    genoeg. Een ongeldige keuze valt terug op de eerste foto.
    """
    if not images:
        return []
    if not (isinstance(main_index, int) and 0 <= main_index < len(images)):
        main_index = 0
    ordered = [images[main_index]] + [img for i, img in enumerate(images) if i != main_index]
    return [{**img, "is_main": i == 0} for i, img in enumerate(ordered)]


def set_uploaded_main(book_id, upload_id):
    """
    Maakt een eigen upload de hoofdfoto door hem vooraan te zetten. Dat werkt alleen zolang er nog niets van dit boek
    bij Boekwinkeltjes staat (geen gepushte of bevestigde foto's): daarna is de eerste foto daar al bepaald.
    Geeft True terug als het gelukt is, anders False (en wordt er niets gewijzigd).
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM book_uploaded_images WHERE id = %(id)s AND book_id = %(book_id)s",
                {"id": int(upload_id), "book_id": int(book_id)},
            )
            if cur.fetchone()[0] == 0:
                return False
            cur.execute(
                "SELECT COUNT(*) FROM book_uploaded_images WHERE book_id = %(book_id)s AND pushed_to_boekwinkeltjes = TRUE",
                {"book_id": int(book_id)},
            )
            already_pushed = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM book_images WHERE book_id = %(book_id)s AND image_id != -1",
                {"book_id": int(book_id)},
            )
            already_confirmed = cur.fetchone()[0]
            if already_pushed or already_confirmed:
                return False
            cur.execute(
                """
                WITH ordered AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY (id = %(id)s) DESC, position, id) - 1 AS new_position
                    FROM book_uploaded_images WHERE book_id = %(book_id)s
                )
                UPDATE book_uploaded_images u
                SET position = o.new_position, is_main = (u.id = %(id)s)
                FROM ordered o WHERE u.id = o.id
                """,
                {"id": int(upload_id), "book_id": int(book_id)},
            )
        conn.commit()
        return True
    finally:
        conn.close()


def find_placeholder_uploads():
    """
    Zoekt eigen uploads die nog NIET naar Boekwinkeltjes zijn gestuurd en een plaatshouder blijken te zijn
    ("BOOK COVER NOT AVAILABLE"). Geeft een lijst dicts {upload_id, book_id, title, ean}. Alleen kleine plaatjes
    worden opgehaald (een plaatshouder is klein), zodat dit niet alle foto's door de lijn trekt.
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.book_id, b.title, b.ean, u.image_data
                FROM book_uploaded_images u JOIN books b ON b.id = u.book_id
                WHERE u.pushed_to_boekwinkeltjes = FALSE AND octet_length(u.image_data) < 200000
                ORDER BY u.book_id, u.position
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"upload_id": r[0], "book_id": r[1], "title": r[2], "ean": r[3]}
        for r in rows
        if check_cover_image(bytes(r[4])) == "placeholder"
    ]


def delete_placeholder_uploads(upload_ids):
    """
    Verwijdert de opgegeven eigen uploads, maar alleen als ze nog niet gepusht zijn én nog steeds een plaatshouder
    zijn (dat wordt hier opnieuw gecontroleerd). Geeft het aantal verwijderde foto's terug.
    """
    ids = [int(i) for i in upload_ids]
    if not ids:
        return 0
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, image_data FROM book_uploaded_images WHERE id = ANY(%(ids)s) AND pushed_to_boekwinkeltjes = FALSE",
                {"ids": ids},
            )
            confirmed = [row[0] for row in cur.fetchall() if check_cover_image(bytes(row[1])) == "placeholder"]
            if not confirmed:
                return 0
            cur.execute(
                "DELETE FROM book_uploaded_images WHERE id = ANY(%(ids)s) AND pushed_to_boekwinkeltjes = FALSE",
                {"ids": confirmed},
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


@st.cache_resource(ttl=600)
def _boekwinkeltjes_login_session():
    """
    Logt in op de WEBSITE van Boekwinkeltjes (niet de officiële verkoper-API,
    die daar los van staat) en geeft een ingelogde requests.Session terug.
    Gebruikt BOEKWINKELTJES_USERNAME en BOEKWINKELTJES_PASSWORD uit secrets/.env
    (dit is het gewone inlogaccount, niet de API-sleutel). Onofficieel/niet-
    gegarandeerd: dit kan stoppen met werken als de website hun inlogformulier
    wijzigt. Geeft (session, None) bij succes, (None, foutmelding) bij een fout.

    Gecached met cache_resource (niet cache_data, want een Session-object is
    geen data om te serialiseren maar een levende verbinding) zodat niet bij
    elke afzonderlijke ISBN-opzoeking opnieuw hoeft te worden ingelogd.
    """
    username = _get_secret("BOEKWINKELTJES_USERNAME")
    password = _get_secret("BOEKWINKELTJES_PASSWORD")
    if not username or not password:
        return None, "Geen BOEKWINKELTJES_USERNAME/BOEKWINKELTJES_PASSWORD ingesteld in secrets."

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        session.get("https://www.boekwinkeltjes.nl/login/", timeout=10)
        session.post(
            "https://www.boekwinkeltjes.nl/login/",
            data={
                "boekwinkeltje": username,
                "wachtwoord": password,
                "persistentCookie": "yes",
                "form": "login",
                "submit": "Inloggen",
            },
            timeout=10,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return None, f"Kon niet inloggen bij Boekwinkeltjes: {e}"

    if "secureID" not in session.cookies.get_dict():
        return None, "Inloggen bij Boekwinkeltjes is mislukt (verkeerde gebruikersnaam/wachtwoord?)."
    return session, None


@st.cache_data(ttl=600)
def lookup_boekwinkeltjes_market_info(isbn):
    """
    Haalt marktinfo (aantal aanbieders, prijsrange, laatste verkoop) op via een
    ONOFFICIEEL, niet-gedocumenteerd endpoint dat de website van Boekwinkeltjes
    zelf gebruikt bij het invoeren van een ISBN. Dit maakt GEEN deel uit van de
    officiële verkoper-API en kan zonder aankondiging veranderen of stoppen met
    werken. Logt bij elke opzoekactie opnieuw in (zie _boekwinkeltjes_login_session),
    dus er is geen verlopende sessie-cookie om bij te houden.

    Geeft (data, foutmelding) terug: data is None bij een fout (foutmelding legt
    uit wat er misging), of het opgezochte resultaat bij succes (foutmelding None).
    """
    isbn = (isbn or "").strip()
    if not isbn or isbn == "0":
        return None, None

    session, error = _boekwinkeltjes_login_session()
    if error:
        return None, error

    rand = random.randint(10_000_000, 99_999_999)
    url = f"https://www.boekwinkeltjes.nl/mbw/boeken/boekinfo/isbnean/{isbn}/rand/{rand}/"
    try:
        resp = session.get(url, timeout=10)
        if not resp.ok:
            return None, f"Boekwinkeltjes gaf een fout terug (status {resp.status_code})."
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        return None, f"Kon geen verbinding maken: {e}"

    if data == "AUTHENTICATION_FAIL" or (isinstance(data, dict) and data.get("error") == "AUTHENTICATION_FAIL"):
        return None, "Authenticatie mislukt ondanks inloggen — mogelijk is het inlogmechanisme veranderd."
    if not data.get("status"):
        return None, "Geen gegevens gevonden voor dit ISBN."
    return data, None


ISBNDB_BASE_URL = "https://api2.isbndb.com"


def _log_isbndb_usage(resp):
    """Leest de 'ratelimit'-header van een ISBNdb-antwoord en logt het resterende dagquotum."""
    header = resp.headers.get("ratelimit") or resp.headers.get("RateLimit")
    if not header:
        return
    match = re.search(r'"daily"\s*;\s*r=(\d+)', header)
    if not match:
        return
    remaining = int(match.group(1))
    try:
        conn = psycopg2.connect(get_db_url())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO isbndb_usage_log (daily_remaining) VALUES (%(r)s)",
                    {"r": remaining},
                )
            conn.commit()
        finally:
            conn.close()
    except psycopg2.Error:
        pass


def get_isbndb_usage_history(limit=200):
    """Geschiedenis van het resterende ISBNdb-dagquotum, oudste eerst (voor een grafiek)."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT checked_at, daily_remaining FROM isbndb_usage_log ORDER BY checked_at DESC LIMIT %(limit)s",
                {"limit": limit},
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    rows.reverse()  # oudste eerst voor de grafiek
    return [{"checked_at": r[0], "daily_remaining": r[1]} for r in rows]


def lookup_isbndb(isbn):
    """
    Haalt boekgegevens (en, indien je plan dat ondersteunt, actuele prijzen van
    diverse externe aanbieders) op bij ISBNdb.com — een betaalde, voor dit doel
    bedoelde bibliografische databank (geen scraping). Vereist ISBNDB_API_KEY
    in secrets/.env. Geeft None terug bij een fout of onbekend ISBN.
    """
    isbn = (isbn or "").strip()
    if not isbn or isbn == "0":
        return None

    api_key = _get_secret("ISBNDB_API_KEY")
    if not api_key:
        return None

    try:
        resp = requests.get(
            f"{ISBNDB_BASE_URL}/book/{isbn}",
            headers={"Authorization": api_key, "Accept": "application/json"},
            params={"with_prices": "1"},
            timeout=15,
        )
    except requests.RequestException:
        return None

    _log_isbndb_usage(resp)

    if not resp.ok:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    book = data.get("book") or {}
    if not book:
        return None

    result = {}
    if book.get("title"):
        result["title"] = book["title"]
    if book.get("authors"):
        result["authors"] = book["authors"]
    if book.get("publisher"):
        result["publisher"] = book["publisher"]
    if book.get("language"):
        result["language"] = book["language"]
    if book.get("pages"):
        result["pages"] = book["pages"]
    if book.get("binding"):
        result["binding"] = book["binding"]
    if book.get("dimensions"):
        result["dimensions_raw"] = book["dimensions"]
    date_published = book.get("date_published")
    if date_published:
        year_match = re.search(r"\d{4}", str(date_published))
        if year_match:
            result["year"] = year_match.group(0)
    synopsis = book.get("synopsis") or book.get("overview") or book.get("excerpt")
    if synopsis:
        result["description"] = synopsis
    if book.get("image"):
        result["cover_url"] = book["image"]
    if book.get("subjects"):
        result["subjects"] = book["subjects"]

    prices = []
    for p in book.get("prices") or []:
        condition = (p.get("condition") or "").strip()
        if condition.lower() == "rent":
            continue
        price_str = (p.get("price") or "").strip()
        if not price_str:
            continue
        currency_symbol = "$"
        for sym in ("$", "€", "£"):
            if price_str.startswith(sym):
                currency_symbol = sym
                price_str = price_str[len(sym):]
                break
        try:
            price_str = f"{currency_symbol}{float(price_str):.2f}".replace(".", ",")
        except ValueError:
            price_str = f"{currency_symbol}{price_str}"
        merchant = p.get("merchant") or "onbekende aanbieder"
        prices.append(f"{merchant}: {price_str}")
    if prices:
        result["prices"] = prices

    return result if result else None


def _map_language_to_boekwinkeltjes(lang):
    """Zet een taalcode/-naam om naar wat Boekwinkeltjes verwacht (bijv. NL, GB)."""
    if not lang:
        return None
    l = lang.strip().lower()
    if l in ("nl", "dut", "dutch", "nld"):
        return "NL"
    if l in ("en", "eng", "english"):
        return "GB"
    return lang.strip().upper()


_DIMENSION_UNIT_TO_CM = {
    "cm": 1.0, "centimeter": 1.0, "centimeters": 1.0,
    "mm": 0.1, "millimeter": 0.1, "millimeters": 0.1,
    "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54,
}


def _dimension_value_to_cm(value, unit):
    """Zet één maat + eenheid om naar centimeters, afgerond op 1 decimaal."""
    factor = _DIMENSION_UNIT_TO_CM.get((unit or "cm").strip().lower())
    if factor is None or value is None:
        return None
    return round(value * factor, 1)


def _parse_single_dimension_string(text):
    """Parst een losse maat als '24.00 cm' of '9.21 in' naar centimeters."""
    if not text:
        return None
    match = re.search(r"([\d]+[.,]?[\d]*)\s*(cm|mm|in|inch|inches)\b", str(text), re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return _dimension_value_to_cm(value, match.group(2))


def _parse_combined_dimensions_string(text):
    """
    Parst een gecombineerde maatvoering als '9.21 x 6.14 x 0.96 inches' (of met
    '×') naar een lijst van waarden in centimeters. Gebruikt bij bronnen (zoals
    ISBNdb) die lengte/breedte/dikte als één vrije-tekst-veld teruggeven, met
    maar één eenheid voor alle getallen samen.
    """
    if not text:
        return []
    text = str(text)
    unit_match = re.search(r"(cm|mm|inch|inches|in)\b", text, re.IGNORECASE)
    unit = unit_match.group(1) if unit_match else "cm"
    numbers = re.findall(r"\d+[.,]?\d*", text)
    values = []
    for n in numbers:
        try:
            cm_value = _dimension_value_to_cm(float(n.replace(",", ".")), unit)
        except ValueError:
            continue
        if cm_value:
            values.append(cm_value)
    return values


def _assign_length_width_thickness(values):
    """
    Sorteert 2 of 3 cm-waarden en wijst ze toe volgens een vaste regel (bronnen
    zijn niet altijd consistent in wat ze zelf 'hoogte'/'breedte'/'dikte'
    noemen): de hoogste waarde is de lengte, de laagste de dikte, en (bij 3
    waarden) wat overblijft is de breedte. Geeft (lengte, breedte, dikte) terug,
    met None voor wat niet te bepalen was.
    """
    clean_values = sorted((v for v in values if v and v > 0), reverse=True)
    if len(clean_values) < 2:
        return None, None, None
    if len(clean_values) == 2:
        return clean_values[0], None, clean_values[1]
    return clean_values[0], clean_values[1], clean_values[2]


BUSSTUK_LENGTH_LIMIT_CM = 37
BUSSTUK_THICKNESS_LIMIT_CM = 2.2
# Grenzen waarbinnen een gevonden afmeting nog geloofwaardig is voor een boek —
# daarbuiten vertrouwen we de bron niet (bronnen zoals ISBNdb geven afmetingen
# soms in een niet-standaard of dubbelzinnige vorm, wat tot absurde waarden kan
# leiden, bijv. een 'boek' van 2 meter lang).
PLAUSIBLE_LENGTH_RANGE_CM = (5, 45)
PLAUSIBLE_THICKNESS_RANGE_CM = (0.2, 8)


SHIPPING_DEFAULT_BRIEFPOST = 3.95
SHIPPING_DEFAULT_PAKKETPOST = 7.25


@st.cache_data(ttl=300)
def get_shipping_costs():
    """
    De verzendkosten voor briefpost (busstuk) en pakketpost, als (briefpost,
    pakketpost) in euro's. Instelbaar op 'Hulp en instellingen' (opgeslagen in
    app_settings), zodat een prijswijziging van de vervoerder geen codewijziging
    vraagt. Is er nog niets ingesteld, of is een waarde onleesbaar of niet groter
    dan 0, dan geldt het standaardbedrag (3,95 resp. 7,25).
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM app_settings WHERE key IN (%s, %s)",
                ("bw_shipping_briefpost", "bw_shipping_pakketpost"),
            )
            stored = dict(cur.fetchall())
    finally:
        conn.close()

    def _amount(key, default):
        try:
            value = float(str(stored.get(key)).replace(",", "."))
        except ValueError:
            return default
        return round(value, 2) if 0 < value < 1000 else default

    return (
        _amount("bw_shipping_briefpost", SHIPPING_DEFAULT_BRIEFPOST),
        _amount("bw_shipping_pakketpost", SHIPPING_DEFAULT_PAKKETPOST),
    )


def shipping_amount_label(amount):
    """3.75 -> '3,75': zoals een bedrag in de keuzelijst met verzendkosten staat."""
    return f"{amount:.2f}".replace(".", ",")


def shipping_options_labels(briefpost_eur, pakketpost_eur):
    """De keuzelijst voor de verzendkosten: 'Vrije invoer' plus de twee ingestelde bedragen, van laag naar hoog."""
    amounts = sorted({round(briefpost_eur, 2), round(pakketpost_eur, 2)})
    return ["Vrije invoer"] + [shipping_amount_label(amount) for amount in amounts]


def determine_busstuk(length_cm, thickness_cm, briefpost_eur, pakketpost_eur):
    """
    Bepaalt of een boek met deze afmetingen als 'busstuk' (briefpost) kan worden
    verstuurd of als pakket moet, en welke verzendkosten daarbij default zijn:
    de ingestelde briefpost-kosten bij een busstuk, de pakketpost-kosten bij een
    pakket. De bedragen komen uit de instellingen (zie get_shipping_costs).

    Zijn er geen afmetingen bekend, of vallen ze buiten wat fysiek geloofwaardig
    is voor een boek, dan wordt dat niet gegokt: het HOOGSTE van de twee
    bedragen wordt dan als veilige default gekozen (liever een keer iets te veel
    in rekening gebracht dan marge verliezen aan te lage verzendkosten).

    Geeft (is_busstuk, shipping_cost, toelichtende tekst) terug. is_busstuk is
    None als er niets geloofwaardigs bekend is; shipping_cost en de tekst zijn
    altijd gevuld.
    """
    plausible = (
        length_cm is not None and thickness_cm is not None
        and PLAUSIBLE_LENGTH_RANGE_CM[0] <= length_cm <= PLAUSIBLE_LENGTH_RANGE_CM[1]
        and PLAUSIBLE_THICKNESS_RANGE_CM[0] <= thickness_cm <= PLAUSIBLE_THICKNESS_RANGE_CM[1]
    )
    if not plausible:
        message = "⚠️ Het is onbekend hoe lang en dik het boek is, controleer de gekozen verzendkosten goed."
        if length_cm is not None and thickness_cm is not None:
            # Niet zomaar stilzwijgend verwerpen: de genegeerde waarde laten zien,
            # zodat jij een duidelijk foute waarde ook kunt zien en kunt melden.
            length_str = f"{length_cm:.1f}".replace(".", ",")
            thickness_str = f"{thickness_cm:.1f}".replace(".", ",")
            message += f" (Gevonden maar genegeerd, want niet geloofwaardig: {length_str} cm lang x {thickness_str} cm dik.)"
        return None, max(briefpost_eur, pakketpost_eur), message

    is_busstuk = not (length_cm > BUSSTUK_LENGTH_LIMIT_CM or thickness_cm > BUSSTUK_THICKNESS_LIMIT_CM)
    shipping_cost = briefpost_eur if is_busstuk else pakketpost_eur
    length_str = f"{length_cm:.1f}".replace(".", ",")
    thickness_str = f"{thickness_cm:.1f}".replace(".", ",")
    soort = "busstuk" if is_busstuk else "pakket"
    emoji = "✉️" if is_busstuk else "📦"
    message = f"{emoji} Het boek is waarschijnlijk {length_str} cm lang en {thickness_str} cm dik, dan is het een {soort}."
    return is_busstuk, shipping_cost, message


def _strip_html(text):
    """
    Haalt HTML-codes uit tekst (bijv. <p>, </p>, <em>, <br>) — van het hele stuk
    tussen < en > wordt alles vervangen door een spatie, waarna meerdere spaties
    achter elkaar worden teruggebracht tot één.
    """
    if not text:
        return text
    without_tags = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r" {2,}", " ", without_tags).strip()


@st.cache_data(ttl=3600)
def _download_cover(url):
    """Haalt een omslagplaatje op. Geeft (bytes, content_type), of None als het niet lukt."""
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException:
        return None
    if resp.ok and resp.content:
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")
    return None


def pick_cover(candidates, open_library_url=None):
    """
    Probeert de omslag-bronnen in volgorde (lijst van (naam, url)) en neemt de eerste die een echte omslag is.
    Een plaatshouder zoals "BOOK COVER NOT AVAILABLE", een onleesbaar of te klein plaatje wordt overgeslagen en
    de volgende bron is aan de beurt; zo komt zo'n plaatje nooit als foto bij een boek terecht en gaat het niet
    naar Boekwinkeltjes. Open Library komt als laatste terugval. Geeft
    {'bytes', 'content_type', 'source', 'rejected'} terug; 'rejected' is een lijst van (bron, reden).
    """
    rejected = []
    sources = list(candidates)
    if open_library_url:
        sources.append(("Open Library", open_library_url))
    for name, url in sources:
        downloaded = _download_cover(url)
        if not downloaded:
            continue
        verdict = check_cover_image(downloaded[0])
        if verdict == "ok":
            return {"bytes": downloaded[0], "content_type": downloaded[1], "source": name, "rejected": rejected}
        rejected.append((name, verdict))
    return {"bytes": None, "content_type": None, "source": None, "rejected": rejected}


def lookup_book_metadata_external(isbn):
    """
    Haalt boekgegevens (titel, auteur, uitgever, taal, omslagfoto, beschrijving,
    en eventueel externe prijzen) op — eerst via ISBNdb.com (betaald, voor dit
    doel bedoeld), aangevuld met Google Books en Open Library voor wat ISBNdb
    niet gaf. Geen scraping van commerciële platforms zoals Bol.com/Amazon/
    AbeBooks. Geeft None terug als er niets bruikbaars is gevonden.
    """
    isbn = (isbn or "").strip()
    if not isbn or isbn == "0":
        return None

    result = {}
    cover_candidates = []  # (bron, url) in volgorde van voorkeur; zie pick_cover

    # 1) ISBNdb eerst — vaak Nederlandstalige beschrijvingen en heeft ook prijzen
    isbndb_data = lookup_isbndb(isbn)
    if isbndb_data:
        if isbndb_data.get("title"):
            result["title"] = isbndb_data["title"]
        if isbndb_data.get("authors"):
            result["author"] = ", ".join(isbndb_data["authors"])
        if isbndb_data.get("publisher"):
            result["publisher"] = isbndb_data["publisher"]
        if isbndb_data.get("language"):
            result["language"] = _map_language_to_boekwinkeltjes(isbndb_data["language"])
        if isbndb_data.get("description"):
            result["description"] = isbndb_data["description"]
        if isbndb_data.get("cover_url"):
            cover_candidates.append(("ISBNdb", isbndb_data["cover_url"]))
        if isbndb_data.get("prices"):
            result["prices"] = isbndb_data["prices"]
        if isbndb_data.get("subjects"):
            result["subjects"] = isbndb_data["subjects"]
        if isbndb_data.get("dimensions_raw"):
            result["_isbndb_dimension_candidates"] = _parse_combined_dimensions_string(
                isbndb_data["dimensions_raw"]
            )
        bijz_parts = []
        if isbndb_data.get("year"):
            bijz_parts.append(str(isbndb_data["year"]))
        if isbndb_data.get("pages"):
            bijz_parts.append(f"{isbndb_data['pages']}pp")
        if isbndb_data.get("binding"):
            bijz_parts.append(isbndb_data["binding"])
        if bijz_parts:
            result["bijz"] = ", ".join(bijz_parts[:2]) + (f", {bijz_parts[2]}" if len(bijz_parts) > 2 else "")
        result["source"] = "ISBNdb"

    # 2) Google Books vult aan wat ISBNdb niet gaf
    try:
        resp = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn}"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            items = data.get("items") or []
            if items:
                info = items[0].get("volumeInfo", {})
                image_links = info.get("imageLinks") or {}
                cover_url = (
                    image_links.get("extraLarge")
                    or image_links.get("large")
                    or image_links.get("medium")
                    or image_links.get("small")
                    or image_links.get("thumbnail")
                    or image_links.get("smallThumbnail")
                )
                if cover_url:
                    cover_url = cover_url.replace("http://", "https://").replace("&edge=curl", "")
                if info.get("description") and "description" not in result:
                    result["description"] = info["description"]
                if cover_url:
                    cover_candidates.append(("Google Books", cover_url))
                gb_dimensions = info.get("dimensions") or {}
                google_dim_candidates = [
                    _parse_single_dimension_string(gb_dimensions.get("height")),
                    _parse_single_dimension_string(gb_dimensions.get("width")),
                    _parse_single_dimension_string(gb_dimensions.get("thickness")),
                ]
                if any(google_dim_candidates):
                    result["_google_dimension_candidates"] = google_dim_candidates
                result.setdefault("source", "Google Books")
    except (requests.RequestException, ValueError):
        pass

    # 3) De omslagfoto: de bronnen in volgorde proberen (ISBNdb, Google Books en als laatste Open Library) en de
    # eerste nemen die een echte omslag is. Een plaatshouder als "BOOK COVER NOT AVAILABLE" wordt overgeslagen
    # (zie cover_check); anders komt die als foto bij het boek en gaat hij naar Boekwinkeltjes.
    cover = pick_cover(cover_candidates, open_library_url=f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg")
    if cover["bytes"]:
        result["cover_bytes"] = cover["bytes"]
        result["cover_content_type"] = cover["content_type"]
        result["cover_source"] = cover["source"]
        if cover["source"] == "Open Library":
            result.setdefault("source", "Open Library")
    else:
        placeholders = [name for name, verdict in cover["rejected"] if verdict == "placeholder"]
        if placeholders:
            result["cover_rejected"] = placeholders

    if "description" not in result:
        try:
            resp = requests.get(
                "https://openlibrary.org/api/books",
                params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                book = data.get(f"ISBN:{isbn}")
                if book:
                    desc = book.get("notes") or book.get("excerpts")
                    if isinstance(desc, list) and desc:
                        desc = desc[0]
                    if isinstance(desc, dict):
                        desc = desc.get("value") or desc.get("text")
                    if desc:
                        result["description"] = desc
                        result.setdefault("source", "Open Library")
        except (requests.RequestException, ValueError):
            pass

    if not result:
        return None

    if result.get("description"):
        result["description"] = _strip_html(result["description"])

    # Afmetingen: Google Books heeft voorkeur (nette, al-gestructureerde cm-waarden),
    # anders de vrije-tekst maatvoering van ISBNdb als terugval.
    google_candidates = result.pop("_google_dimension_candidates", [])
    isbndb_candidates = result.pop("_isbndb_dimension_candidates", [])
    length_cm, width_cm, thickness_cm = _assign_length_width_thickness(google_candidates)
    if length_cm is None:
        length_cm, width_cm, thickness_cm = _assign_length_width_thickness(isbndb_candidates)
    if length_cm is not None:
        result["length_cm"] = length_cm
        result["width_cm"] = width_cm
        result["thickness_cm"] = thickness_cm

    has_content = any(
        result.get(k) for k in ("cover_bytes", "description", "title", "author", "publisher", "bijz", "prices")
    )
    return result if has_content else None


# ---------- Categoriebeheer: koppeling Boekwinkeltjes-categorieën <-> ISBNdb-subjects ----------

def search_isbndb_subjects(query):
    """
    Zoekt bij ISBNdb.com naar subjects die overeenkomen met 'query'. Dit is een
    zoekfunctie (geen volledige lijst-functie) — ISBNdb biedt geen eindpunt om
    alle subjects in één keer op te vragen. Geeft een lijst met subject-namen
    terug (kan leeg zijn), of een lege lijst bij een fout.
    """
    api_key = _get_secret("ISBNDB_API_KEY")
    if not api_key or not query:
        return []
    try:
        resp = requests.get(
            f"{ISBNDB_BASE_URL}/subjects/{query}",
            headers={"Authorization": api_key, "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException:
        return []
    _log_isbndb_usage(resp)
    if not resp.ok:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    subjects = data.get("subjects") or []
    names = []
    for s in subjects:
        if isinstance(s, str):
            names.append(s)
        elif isinstance(s, dict) and s.get("name"):
            names.append(s["name"])
    return names


def get_category_subject_mappings():
    """Alle rijen van de categorie<->subject-koppeltabel, als lijst van dicts."""
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, boekwinkeltjes_category1, boekwinkeltjes_category2, subjects "
                "FROM category_subject_mapping ORDER BY boekwinkeltjes_category1, boekwinkeltjes_category2"
            )
            return cur.fetchall()
    finally:
        conn.close()


def save_category_subject_mapping(category1, category2, subjects_text):
    """Slaat (of overschrijft) de subjects-lijst voor één categorie1(+2)-combinatie op."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO category_subject_mapping (boekwinkeltjes_category1, boekwinkeltjes_category2, subjects)
                VALUES (%(c1)s, %(c2)s, %(subjects)s)
                ON CONFLICT (boekwinkeltjes_category1, boekwinkeltjes_category2)
                DO UPDATE SET subjects = excluded.subjects
                """,
                {"c1": category1, "c2": category2 or "", "subjects": subjects_text},
            )
        conn.commit()
    finally:
        conn.close()


def match_subjects_to_category(book_subjects):
    """
    Vergelijkt de subjects van een opgezocht boek (bijv. via ISBNdb) met de
    categorie<->subject-koppeltabel. Zoekt EERST een Categorie 1-brede match;
    Categorie 2 (Hobby/Topografie/Studieboeken) wordt alleen als verfijning
    daarbinnen gebruikt — nooit als een op zichzelf staande match die een betere,
    bredere Categorie 1-treffer kan overstemmen. Geeft (None, None) terug als
    niets overeenkomt.
    """
    if not book_subjects:
        return None, None
    book_subjects_lower = {s.strip().lower() for s in book_subjects if s and s.strip()}
    if not book_subjects_lower:
        return None, None

    mappings = get_category_subject_mappings()

    matched_cat1 = None
    for row in mappings:
        if row["boekwinkeltjes_category2"]:
            continue
        mapped_subjects = {s.strip().lower() for s in (row["subjects"] or "").split("|") if s.strip()}
        if mapped_subjects & book_subjects_lower:
            matched_cat1 = row["boekwinkeltjes_category1"]
            break

    if not matched_cat1:
        return None, None

    # Verfijnen met een categorie2-match, maar alleen bínnen deze categorie1
    for row in mappings:
        if row["boekwinkeltjes_category1"] != matched_cat1 or not row["boekwinkeltjes_category2"]:
            continue
        mapped_subjects = {s.strip().lower() for s in (row["subjects"] or "").split("|") if s.strip()}
        if mapped_subjects & book_subjects_lower:
            return matched_cat1, row["boekwinkeltjes_category2"]

    return matched_cat1, None


# ---------- Instellingen (eenvoudige key-value-tabel) ----------

def get_setting(key, default=None):
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %(key)s", {"key": key})
            row = cur.fetchone()
            return row[0] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (%(key)s, %(value)s)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                {"key": key, "value": str(value)},
            )
        conn.commit()
    finally:
        conn.close()


# ---------- Einddatum van de GitHub-sleutel ----------
# Alle geplande taken (cron-job.org) en de knoppen in dit dashboard gebruiken één GitHub-sleutel. Heeft die een einddatum,
# en verloopt hij, dan stopt alles tegelijk. GitHub geeft die datum niet betrouwbaar aan een programma door, dus vul je
# hem één keer in (Hulp en instellingen). Dezelfde instelling gebruikt de waakhond om je te mailen.

GITHUB_TOKEN_EXPIRES_KEY = "github_token_expires"
GITHUB_TOKEN_NEVER = "never"  # de sleutel verloopt niet: er is geen einddatum om te bewaken
GITHUB_TOKEN_SOON_DAYS = 30
GITHUB_TOKEN_URGENT_DAYS = 7


def get_github_token_expiry():
    """
    De ingestelde einddatum van de GitHub-sleutel als datum, GITHUB_TOKEN_NEVER ('never') als de sleutel niet verloopt,
    of None (niet ingesteld of onleesbaar).
    """
    raw = get_setting(GITHUB_TOKEN_EXPIRES_KEY)
    text = str(raw).strip() if raw else ""
    if text.lower() == GITHUB_TOKEN_NEVER:
        return GITHUB_TOKEN_NEVER
    try:
        return dt.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def set_github_token_expiry(expiry):
    """Bewaart de einddatum (een datum), 'never' (verloopt niet), of wist de instelling (None)."""
    if expiry == GITHUB_TOKEN_NEVER:
        value = GITHUB_TOKEN_NEVER
    elif expiry:
        value = expiry.isoformat()
    else:
        value = ""
    set_setting(GITHUB_TOKEN_EXPIRES_KEY, value)


def github_token_status(expiry, today=None):
    """
    Hoe staat het met de GitHub-sleutel? Geeft {'state', 'days'} terug; 'state' is 'unset' (niets ingesteld), 'never'
    (verloopt niet), 'ok', 'soon' (binnen 30 dagen), 'urgent' (binnen 7 dagen) of 'expired'. De einddatum zelf telt al
    als verlopen: GitHub zet de sleutel op die dag uit, en liever een dag te vroeg gewaarschuwd dan te laat.
    """
    if expiry == GITHUB_TOKEN_NEVER:
        return {"state": "never", "days": None}
    if not expiry:
        return {"state": "unset", "days": None}
    today = today or dt.datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    days = (expiry - today).days
    if days <= 0:
        state = "expired"
    elif days <= GITHUB_TOKEN_URGENT_DAYS:
        state = "urgent"
    elif days <= GITHUB_TOKEN_SOON_DAYS:
        state = "soon"
    else:
        state = "ok"
    return {"state": state, "days": days}


def github_token_banner(expiry, today=None):
    """Wat Home laat zien: (soort, tekst) met soort 'error', 'warning' of 'caption'."""
    status = github_token_status(expiry, today)
    state, days = status["state"], status["days"]
    if state == "never":
        return "caption", "GitHub-sleutel: verloopt niet (zo ingesteld)."
    if state == "unset":
        return "warning", (
            "De einddatum van de GitHub-sleutel is niet ingesteld. Vul hem in bij Hulp en instellingen, dan waarschuwt de "
            "app je op tijd voordat de geplande taken stoppen."
        )
    date_text = expiry.strftime("%d-%m-%Y")
    if state == "expired":
        return "error", (
            f"🚨 De GitHub-sleutel is verlopen of verloopt vandaag ({date_text}). Daarna werken de geplande taken en de "
            f"knoppen in dit dashboard niet meer. Vernieuw hem direct (zie Hulp en instellingen)."
        )
    if state == "urgent":
        return "error", (
            f"🚨 De GitHub-sleutel verloopt over {days} {'dag' if days == 1 else 'dagen'}, op {date_text}. Daarna stoppen de "
            f"geplande taken. Vernieuw hem nu (zie Hulp en instellingen)."
        )
    if state == "soon":
        return "warning", (
            f"⚠️ De GitHub-sleutel verloopt over {days} dagen, op {date_text}. Vernieuw hem op tijd (zie Hulp en instellingen)."
        )
    return "caption", f"GitHub-sleutel verloopt op {date_text} (nog {days} dagen)."


_PUBLISHER_NOISE_WORDS = {
    "uitgeverij", "uitgevers", "uitgeverijen", "publishing", "publishers", "publisher",
    "bv", "b.v.", "nv", "n.v.", "inc", "inc.", "ltd", "ltd.", "the", "and", "&", "en",
}


def _normalize_publisher_name(name):
    """Zet een uitgeversnaam om naar kleine letters, zonder leestekens en zonder
    veelvoorkomende neutrale woorden ('uitgeverij', 'bv', 'publishing', ...), zodat
    de vergelijking niet verwatert door zulke toevoegingen."""
    name = (name or "").lower()
    name = re.sub(r"[^\w\s]", " ", name)
    words = [w for w in name.split() if w not in _PUBLISHER_NOISE_WORDS]
    return " ".join(words).strip()


def find_matching_publisher(candidate_name, threshold=None):
    """
    Vergelijkt 'candidate_name' (een uitgeversnaam uit Boekwinkeltjes-marktinfo of
    ISBNdb) met de naam vóór de eerste puntkomma van elke bekende uitgever, en
    geeft de volledige, bestaande uitgeversregel terug (naam ; adres ; contact)
    als de gelijkenis minstens 'threshold' procent is (standaard: de ingestelde
    'publisher_match_threshold', of 90 als die nog niet is ingesteld). Neutrale
    woorden als 'uitgeverij'/'bv'/'publishing' worden voor de vergelijking genegeerd,
    zodat die de gelijkenis niet onterecht verlagen.
    Geeft None terug als er geen voldoende goede match is.
    """
    candidate_name = (candidate_name or "").strip()
    if not candidate_name:
        return None
    if threshold is None:
        threshold = int(get_setting("publisher_match_threshold", "90"))

    candidate_norm = _normalize_publisher_name(candidate_name)
    if not candidate_norm:
        return None

    best_match = None
    best_ratio = 0.0
    for entry in get_known_publishers():
        existing_name = entry.split(";")[0].strip()
        existing_norm = _normalize_publisher_name(existing_name)
        if not existing_norm:
            continue
        ratio = SequenceMatcher(None, candidate_norm, existing_norm).ratio() * 100
        if candidate_norm in existing_norm or existing_norm in candidate_norm:
            ratio = max(ratio, 95.0)
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = entry

    if best_match is not None and best_ratio >= threshold:
        return best_match
    return None


# ---------- Zelflerende ISBN-uitgeverscode ----------
# Een Nederlands/Vlaams ISBN bestaat uit 978 + 90/94 (registratiegroep) + een
# uitgeverscode van wisselende lengte (2 t/m 7 cijfers) + een titelcode + een
# controlecijfer. Omdat er geen vrij beschikbare, betrouwbare lijst bestaat die
# dat cijferblok aan een uitgeversnaam koppelt, bouwt de app die koppeling zelf
# op: elke keer dat een uitgever via de normale weg wél gevonden wordt, wordt dat
# onthouden. Pas na MIN_OBSERVATIONS keer dezelfde, eenduidige combinatie wordt
# die koppeling ook echt gebruikt als een nieuwe uitgever niet te vinden is.

ISBN_PREFIX_MIN_OBSERVATIONS = 3


def _isbn_prefix_candidates(isbn):
    """
    Geeft, van lang naar kort, de mogelijke uitgeverscode-cijferblokken van een
    Nederlands/Vlaams ISBN terug (lengte 7 t/m 2) — of een lege lijst als dit geen
    Nederlands/Vlaams ISBN-13 is (begint niet met 97890 of 97894).
    """
    digits = re.sub(r"\D", "", isbn or "")
    if len(digits) != 13 or digits[:4] != "9789" or digits[4] not in ("0", "4"):
        return []
    middle_block = digits[5:12]  # uitgeverscode + titelcode samen, 7 cijfers
    return [middle_block[:length] for length in range(7, 1, -1)]


def record_isbn_prefix_observation(isbn, publisher):
    """
    Legt vast dat dit ISBN bij deze uitgever hoort, voor elk mogelijk
    uitgeverscode-cijferblok van dat ISBN (de juiste lengte wordt vanzelf
    duidelijk doordat die, in tegenstelling tot de verkeerde lengtes, telkens
    dezelfde combinatie oplevert bij meerdere boeken van dezelfde uitgever).
    Doet niets als dit geen Nederlands/Vlaams ISBN is, of als 'publisher' leeg is.
    """
    publisher = (publisher or "").strip()
    if not publisher:
        return
    candidates = _isbn_prefix_candidates(isbn)
    if not candidates:
        return

    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            for prefix in candidates:
                cur.execute(
                    """
                    INSERT INTO isbn_prefix_observations (prefix, publisher, times_seen, last_seen_at)
                    VALUES (%(prefix)s, %(publisher)s, 1, now())
                    ON CONFLICT (prefix, publisher) DO UPDATE SET
                        times_seen = isbn_prefix_observations.times_seen + 1,
                        last_seen_at = now()
                    """,
                    {"prefix": prefix, "publisher": publisher},
                )
        conn.commit()
    finally:
        conn.close()


def lookup_publisher_by_isbn_prefix(isbn, min_observations=ISBN_PREFIX_MIN_OBSERVATIONS):
    """
    Zoekt, als laatste redmiddel wanneer de normale uitgeverherkenning niets
    oplevert, of dit ISBN een uitgeverscode-cijferblok heeft dat we al vaak
    genoeg (en eenduidig — geen andere uitgever ooit onder datzelfde blok gezien)
    aan een uitgever hebben zien koppelen. Begint bij de langste (specifiekste)
    kandidaat en werkt af naar de kortste. Geeft de volledige uitgeversregel
    (naam ; adres ; contact) terug, of None.
    """
    candidates = _isbn_prefix_candidates(isbn)
    if not candidates:
        return None

    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            for prefix in candidates:
                cur.execute(
                    "SELECT publisher, times_seen FROM isbn_prefix_observations WHERE prefix = %(prefix)s",
                    {"prefix": prefix},
                )
                rows = cur.fetchall()
                if len(rows) == 1 and rows[0]["times_seen"] >= min_observations:
                    return rows[0]["publisher"]
                # Meerdere verschillende uitgevers onder hetzelfde blok: dit blok is
                # kennelijk te kort om betrouwbaar te zijn — niet gebruiken, ook niet
                # gedeeltelijk, en ook de kortere kandidaten niet meer proberen.
                if len(rows) > 1:
                    return None
    finally:
        conn.close()
    return None


# ---------- Bulk-import ----------

def suggest_bulk_price(lowest_current_price, floor=3.95):
    """
    Stelt een verkoopprijs voor die net onder 'lowest_current_price' ligt en
    eindigt op ,45 of ,95 (bijv. 6,00 -> 5,95; 5,80 -> 5,45; 5,40 -> 4,95),
    met 'floor' als bodem (standaard €3,95, de absolute minimumprijs voor een boek —
    kan hoger worden meegegeven, bijv. Prijs Boekwinkeltjes + Verzendkosten + 2,25).
    """
    if lowest_current_price is None:
        return None
    price = Decimal(str(lowest_current_price))
    candidate = Decimal(int(price)) + Decimal("0.95")
    while candidate >= price:
        candidate -= Decimal("0.50")
    return float(max(candidate, Decimal(str(floor))))


def autofill_book_fields_from_isbn(isbn):
    """
    Bouwt een dict met voorgestelde boekvelden voor één ISBN, voor gebruik bij
    bulk-import (waar geen mens live meekijkt zoals bij 'Nieuw boek'). Combineert
    Boekwinkeltjes-marktinfo en ISBNdb/Google Books/Open Library, inclusief
    fuzzy-matching van de uitgever en categorie-matching op basis van subjects.
    """
    fields = {"ean": isbn}
    publisher_candidate = None

    # Deze hebben niks met elkaar te maken, dus gelijktijdig ophalen in plaats van
    # na elkaar — dat scheelt flink tijd, aangezien dit bij elk ISBN in een
    # bulk-import wordt herhaald.
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_market = executor.submit(lookup_boekwinkeltjes_market_info, isbn)
        future_metadata = executor.submit(lookup_book_metadata_external, isbn)
        future_bol_offers = executor.submit(lookup_bol_competing_offers, isbn)
        market_info, _ = future_market.result()
        metadata = future_metadata.result()
        bol_count, bol_laagste, _ = future_bol_offers.result()

    bol_price_basis = None  # Bol's laagste prijs, als daar de prijssuggestie op gebaseerd moet worden
    if market_info:
        cleaned_titel, cleaned_bijz = clean_boekwinkeltjes_title_and_bijz(
            market_info.get("titel"), market_info.get("bijz")
        )
        if cleaned_titel:
            fields["title"] = cleaned_titel
        if market_info.get("schrijver"):
            fields["author"] = market_info["schrijver"]
        if market_info.get("taal"):
            fields["language"] = market_info["taal"]
        if cleaned_bijz:
            fields["short_description"] = cleaned_bijz
        if market_info.get("uitgever"):
            publisher_candidate = market_info["uitgever"]
        # Wordt het boek al door minstens één andere verkoper aangeboden? Prijs net
        # onder de huidige laagste prijs voorstellen, in plaats van de kale €0.
        active_amount = market_info.get("activeAmount") or 0
        if active_amount >= 1 and market_info.get("laagste_prijs"):
            try:
                suggested = suggest_bulk_price(float(market_info["laagste_prijs"]))
                if suggested is not None:
                    fields["price"] = suggested
            except (TypeError, ValueError):
                pass
        elif not market_info.get("lastOrder") and bol_count and bol_laagste:
            # Boekwinkeltjes heeft niets, maar Bol wel: de prijs wordt voorgesteld op
            # basis van Bol's laagste prijs. Dat rekenen we pas helemaal onderaan uit,
            # want we moeten daarvoor eerst weten welke verzendkosten bij dit boek horen
            # (briefpost of pakketpost, afhankelijk van de afmetingen).
            try:
                bol_price_basis = float(bol_laagste)
            except (TypeError, ValueError):
                pass

    if metadata:
        if metadata.get("title") and "title" not in fields:
            fields["title"] = metadata["title"]
        if metadata.get("author") and "author" not in fields:
            fields["author"] = metadata["author"]
        if metadata.get("language") and "language" not in fields:
            fields["language"] = metadata["language"]
        if metadata.get("bijz") and "short_description" not in fields:
            fields["short_description"] = metadata["bijz"]
        if metadata.get("description"):
            fields["long_description"] = metadata["description"]
        if metadata.get("publisher") and not publisher_candidate:
            publisher_candidate = metadata["publisher"]
        if metadata.get("subjects"):
            cat1, cat2 = match_subjects_to_category(metadata["subjects"])
            if cat1:
                fields["category1"] = cat1
            if cat2:
                fields["category2"] = cat2

    # Bol als laatste aanvulling voor wat nog steeds ontbreekt (zie kanttekening
    # in lookup_bol_catalog_product: dit is best-effort, geen gegarandeerde koppeling)
    bol_product = None
    metadata_has_dimensions = bool(metadata and metadata.get("length_cm"))
    if (
        not fields.get("title") or not fields.get("author") or not publisher_candidate
        or not fields.get("long_description") or not fields.get("short_description")
        or not metadata_has_dimensions
    ):
        bol_product = lookup_bol_catalog_product(isbn)
        if bol_product:
            if bol_product.get("title") and not fields.get("title"):
                fields["title"] = bol_product["title"]
            if bol_product.get("author") and not fields.get("author"):
                fields["author"] = bol_product["author"]
            if bol_product.get("description") and not fields.get("long_description"):
                fields["long_description"] = _strip_html(bol_product["description"])
            if not fields.get("short_description"):
                bijz_parts = []
                if bol_product.get("year"):
                    bijz_parts.append(bol_product["year"])
                if bol_product.get("pages"):
                    bijz_parts.append(f"{bol_product['pages']}pp")
                if bol_product.get("binding"):
                    bijz_parts.append(bol_product["binding"])
                if bijz_parts:
                    fields["short_description"] = ", ".join(bijz_parts[:2]) + (
                        f", {bijz_parts[2]}" if len(bijz_parts) > 2 else ""
                    )
            if bol_product.get("manufacturer_name") and not publisher_candidate:
                publisher_candidate = " ; ".join(
                    [
                        bol_product.get("manufacturer_name") or "",
                        bol_product.get("manufacturer_address") or "",
                        bol_product.get("manufacturer_contact") or "",
                    ]
                )

    if not fields.get("category1"):
        bol_category_names = lookup_bol_category_names(isbn)
        if bol_category_names:
            cat1, cat2 = find_matching_category(bol_category_names)
            if cat1:
                fields["category1"] = cat1
            if cat2:
                fields["category2"] = cat2

    if publisher_candidate:
        known = get_known_publishers()
        if publisher_candidate in known:
            fields["publisher"] = publisher_candidate
            record_isbn_prefix_observation(isbn, publisher_candidate)
        else:
            match = find_matching_publisher(publisher_candidate)
            if match:
                fields["publisher"] = match
                record_isbn_prefix_observation(isbn, match)
            else:
                fields["publisher"] = publisher_candidate
    else:
        # Geen enkele bron kon een uitgever vinden: als laatste redmiddel kijken of
        # het ISBN-uitgeverscijferblok al vaak genoeg aan een uitgever is gekoppeld.
        learned_publisher = lookup_publisher_by_isbn_prefix(isbn)
        if learned_publisher:
            fields["publisher"] = learned_publisher

    if metadata and metadata.get("cover_bytes"):
        # Geen echt boekveld — de aanroeper haalt dit eruit en gebruikt het apart
        # via save_uploaded_images(), zodat de gevonden omslagfoto ook meekomt.
        fields["_cover_bytes"] = metadata["cover_bytes"]
        fields["_cover_content_type"] = metadata.get("cover_content_type", "image/jpeg")

    # Afmetingen: Google Books/ISBNdb (via metadata) hebben voorkeur, anders Bol
    # als laatste terugval. Op basis daarvan de verzendkosten bepalen (briefpost
    # bij een busstuk, pakketpost bij een pakket; onbekend = het hoogste van de
    # twee, zie determine_busstuk). De bedragen komen uit de instellingen.
    length_cm = width_cm = thickness_cm = None
    if metadata and metadata.get("length_cm"):
        length_cm, width_cm, thickness_cm = metadata["length_cm"], metadata.get("width_cm"), metadata["thickness_cm"]
    elif bol_product and bol_product.get("length_cm"):
        length_cm, width_cm, thickness_cm = bol_product["length_cm"], bol_product.get("width_cm"), bol_product["thickness_cm"]

    if length_cm is not None:
        fields["length_cm"] = length_cm
        fields["width_cm"] = width_cm
        fields["thickness_cm"] = thickness_cm
    briefpost_eur, pakketpost_eur = get_shipping_costs()
    _, shipping_cost, _ = determine_busstuk(length_cm, thickness_cm, briefpost_eur, pakketpost_eur)
    fields["shipping_cost"] = shipping_cost

    # De prijssuggestie op basis van Bol (zie hierboven): Bol's laagste prijs, min
    # €2,25 marge, min de verzendkosten die hierboven zijn bepaald, afgerond naar
    # beneden op ,45 of ,95 en met €3,95 als bodem — in plaats van de kale €0.
    if bol_price_basis is not None:
        suggested = suggest_bulk_price(bol_price_basis - 2.25 - shipping_cost)
        if suggested is not None:
            fields["price"] = suggested

    return fields


def get_queued_books():
    """
    Boeken die in de wachtrij staan (na bulk-import), meest recent eerst. 'added_by' is de korte naam van wie
    het boek heeft toegevoegd (uit het activiteitenlog); None bij oudere boeken waarvan dat niet is bijgehouden.
    """
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.title, b.author, b.ean, b.location,
                       (SELECT l.user_short_name FROM book_activity_log l
                        WHERE l.book_id = b.id AND l.action = 'created'
                        ORDER BY l.occurred_at LIMIT 1) AS added_by
                FROM books b
                WHERE b.queued = TRUE
                ORDER BY b.id DESC
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


QUEUE_VIEW_MINE = "Boeken van mij"
QUEUE_VIEW_ALL = "Alle boeken"


def queued_counts(queued_books, user_short_name):
    """(aantal boeken in de bulkwachtrij dat deze gebruiker heeft toegevoegd, aantal in totaal)."""
    mine = sum(1 for book in queued_books if book.get("added_by") == user_short_name)
    return mine, len(queued_books)


def filter_queued_books(queued_books, user_short_name, view):
    """De boeken die bij de gekozen weergave horen: alleen die van deze gebruiker, of alles."""
    if view == QUEUE_VIEW_MINE:
        return [book for book in queued_books if book.get("added_by") == user_short_name]
    return list(queued_books)


def queued_caption(mine, total):
    """'20 boeken die ik heb toegevoegd in bulk wachten op handmatige controle, 44 in totaal.' (met 'boek ... wacht' bij 1)."""
    woord = "boek dat" if mine == 1 else "boeken die"
    werkwoord = "wacht" if mine == 1 else "wachten"
    return f"{mine} {woord} ik heb toegevoegd in bulk {werkwoord} op handmatige controle, {total} in totaal."


def queued_row_text(book):
    """
    'Titel — Auteur — ISBN 978… (Bart, 0413R)': tussen haakjes wie het boek heeft toegevoegd en waar het ligt.
    Is een van beide niet bekend, dan staat alleen de andere er; zijn ze allebei onbekend, dan geen haakjes.
    """
    text = f"**{book['title'] or '(geen titel)'}** — {book['author'] or '–'} — ISBN {book['ean'] or '–'}"
    location = book.get("location")
    location = str(location).strip() if location is not None else ""
    details = [part for part in (book.get("added_by"), location) if part]
    if details:
        text += f" ({', '.join(details)})"
    return text


def info_box(message):
    """Info-kadertje met leesbare tekstkleur (volgt het thema) en een blauwe rand als accent —
    i.p.v. st.info(), waarvan de standaard blauwe tekst in donkere modus slecht leesbaar is."""
    st.markdown(
        f'<div style="border-left: 4px solid #1c83e1; background-color: rgba(28,131,225,0.12); '
        f'padding: 0.75em 1em; border-radius: 4px; color: inherit;">{message}</div>',
        unsafe_allow_html=True,
    )


def delete_book(book_id):
    """
    Verwijdert een boek volledig lokaal (en de bijbehorende afbeeldingen) —
    bedoeld voor onterecht aangemaakte/verkeerd gescande boeken, met name uit
    de bulk-wachtrij. Raakt niets aan bij Boekwinkeltjes zelf.
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM book_images WHERE book_id = %(id)s", {"id": int(book_id)})
            cur.execute("DELETE FROM book_uploaded_images WHERE book_id = %(id)s", {"id": int(book_id)})
            cur.execute("DELETE FROM books WHERE id = %(id)s", {"id": int(book_id)})
        conn.commit()
    finally:
        conn.close()


def normalize_isbn(raw):
    """
    Zet een ISBN-10 om naar ISBN-13 (met het standaard 978-prefix en herberekende
    controlecijfer); laat een ISBN-13 of iets onherkenbaars ongewijzigd. Spaties
    en streepjes worden genegeerd bij het herkennen.
    """
    if not raw:
        return raw
    cleaned = re.sub(r"[\s-]", "", raw).upper()
    digits_only = re.sub(r"[^0-9X]", "", cleaned)

    if len(digits_only) == 13 and digits_only.isdigit():
        return digits_only

    if len(digits_only) == 10 and digits_only[:9].isdigit():
        base = "978" + digits_only[:9]
        total = sum((1 if i % 2 == 0 else 3) * int(d) for i, d in enumerate(base))
        check = (10 - (total % 10)) % 10
        return base + str(check)

    return raw.strip()


# ---------- Dubbele ISBN, opslagmeting, winkeldochters ----------

def find_existing_book_by_isbn(isbn, exclude_id=None):
    """
    Zoekt een al bestaand boek (met een echt, positief id) met dit ISBN — voor de
    dubbele-ISBN-waarschuwing bij het toevoegen van een nieuw boek. Geeft een dict
    terug ({'id', 'title', 'amount', 'location'}) of None.
    """
    isbn = (isbn or "").strip()
    if not isbn or isbn == "0":
        return None
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            if exclude_id is not None:
                cur.execute(
                    "SELECT id, title, amount, location FROM books WHERE ean = %(isbn)s AND id > 0 AND id != %(exclude)s LIMIT 1",
                    {"isbn": isbn, "exclude": exclude_id},
                )
            else:
                cur.execute(
                    "SELECT id, title, amount, location FROM books WHERE ean = %(isbn)s AND id > 0 LIMIT 1",
                    {"isbn": isbn},
                )
            return cur.fetchone()
    finally:
        conn.close()


# Conditie van het Bol-aanbod, in de volgorde van de taartgrafiek op Home (van best naar slechtst).
BOL_CONDITION_ORDER = ["Nieuw", "Als nieuw", "Goed", "Redelijk", "Matig", "Onbekend"]
_BOL_CONDITION_STATES = {"AS_NEW": "Als nieuw", "GOOD": "Goed", "REASONABLE": "Redelijk", "MODERATE": "Matig"}


def bol_condition_label(category, state):
    """De Nederlandse naam van een Bol-conditie ('Nieuw', 'Als nieuw', 'Goed', 'Redelijk', 'Matig'), anders 'Onbekend'."""
    category = str(category or "").strip().upper()
    state = str(state or "").strip().upper()
    if category == "NEW":
        return "Nieuw"
    if category == "SECONDHAND":
        return _BOL_CONDITION_STATES.get(state, "Onbekend")
    # Staat er alleen een staat (zonder categorie), dan telt die ook.
    return _BOL_CONDITION_STATES.get(state, "Onbekend")


def bol_condition_counts(rows):
    """
    'rows' is een lijst met (categorie, staat). Geeft [(naam, aantal), ...] in vaste volgorde (BOL_CONDITION_ORDER),
    alleen de condities die er daadwerkelijk zijn.
    """
    counts = {}
    for category, state in rows:
        label = bol_condition_label(category, state)
        counts[label] = counts.get(label, 0) + 1
    return [(label, counts[label]) for label in BOL_CONDITION_ORDER if counts.get(label)]


@st.cache_data(ttl=300)
def get_bol_condition_rows():
    """
    (categorie, staat) van elk Bol-aanbod dat nu voorraad heeft, zoals de voorraadsync het laatst bij Bol zag.
    Geeft None als de kolommen er nog niet zijn (de voorraadsync heeft dan nog niet gedraaid met deze versie).
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'bol_offer_mapping' AND column_name = 'condition_category'"
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                "SELECT condition_category, condition_state FROM bol_offer_mapping WHERE COALESCE(bol_stock, 0) > 0"
            )
            return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        conn.close()


def get_database_size_mb():
    """
    Huidige grootte van de database in MB, zoals Supabase de limiet toepast: de som over alle databases
    (niet alleen die van de app). Zo geeft de grafiek op Home hetzelfde getal als de opslagmelding per mail.
    """
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database")
            size_bytes = cur.fetchone()[0]
            # De som van Postgres komt als Decimal binnen; dat laat zich niet delen door een kommagetal (zoals de
            # limiet op Home), dus eerst naar een gewoon getal.
            return float(size_bytes) / (1024 * 1024)
    finally:
        conn.close()


def get_slow_movers(limit=100):
    """
    Boeken die het langst onafgebroken in voorraad staan (op basis van listing_date —
    let op: dat veld kan meebewegen bij een latere bewerking van het boek zelf).
    Alleen echte, actieve boeken (geen wachtrij, voorraad > 0).
    """
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, ean, price, listing_date
                FROM books
                WHERE id > 0 AND COALESCE(queued, FALSE) = FALSE AND COALESCE(amount, 0) > 0
                ORDER BY listing_date ASC NULLS LAST
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------- Bol.com Retailer API: concurrerende aanbiedingen ----------

@st.cache_data(ttl=250)
def _get_bol_access_token():
    """
    Haalt een toegangstoken op bij Bol.com's Retailer API (client_credentials-flow).
    Vereist BOL_CLIENT_ID en BOL_CLIENT_SECRET in secrets/.env (aan te maken in het
    Bol Seller Dashboard onder Instellingen > API Instellingen). Het token is 5
    minuten geldig en wordt daarom hooguit eens per 250 seconden opnieuw opgehaald,
    zoals Bol.com zelf voorschrijft (niet bij elk verzoek een nieuw token vragen).
    """
    client_id = _get_secret("BOL_CLIENT_ID")
    client_secret = _get_secret("BOL_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            "https://login.bol.com/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": f"Basic {credentials}",
            },
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("access_token")
    except requests.RequestException:
        pass
    return None


@st.cache_data(ttl=600)
def lookup_bol_competing_offers(ean):
    """
    Haalt actuele concurrerende aanbiedingen op bij Bol.com voor dit EAN, via de
    Retailer API (bedoeld voor eigen verkopers — geen scraping). Geeft
    (aantal_aanbieders, laagste_prijs, hoogste_prijs) terug — aantal_aanbieders is
    0 als het product bij Bol bekend is maar niemand het aanbiedt, en (None, None,
    None) bij een fout of ontbrekende sleutels.
    """
    ean = (ean or "").strip()
    if not ean or ean == "0":
        return None, None, None

    token = _get_bol_access_token()
    if not token:
        return None, None, None

    try:
        resp = requests.get(
            f"https://api.bol.com/retailer/products/{ean}/offers",
            headers={
                "Accept": "application/vnd.retailer.v10+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=15,
        )
    except requests.RequestException:
        return None, None, None

    if not resp.ok:
        return None, None, None
    try:
        data = resp.json()
    except ValueError:
        return None, None, None

    offers = data.get("offers") or []
    prices = [o["price"] for o in offers if o.get("price") is not None]
    laagste = min(prices) if prices else None
    hoogste = max(prices) if prices else None
    return len(offers), laagste, hoogste


def fmt2(value):
    """Geeft een bedrag terug met altijd exact twee decimalen en een komma als
    decimaalteken (bijv. 9.5 -> '9,50')."""
    try:
        return f"{float(value):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return value


def round_down_to_45_95(price):
    """Rondt af naar het eerstvolgende bedrag eindigend op ,45 of ,95, net onder 'price'."""
    if price is None:
        return None
    p = Decimal(str(price))
    candidate = Decimal(int(p)) + Decimal("0.95")
    while candidate >= p:
        candidate -= Decimal("0.50")
    return float(candidate)


def format_price_dot(value):
    """Formatteert een bedrag met precies 2 decimalen en een komma als decimaalteken
    (voor externe bronnen zoals Boekwinkeltjes' marktinfo en Bol, die zelf vaak een
    punt gebruiken) — voor consistentie gebruikt de hele app overal een komma.
    Geeft de waarde ongewijzigd terug als hij niet om te zetten is naar een getal."""
    try:
        return f"{float(value):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return value


@st.cache_data(ttl=3600)
def lookup_bol_catalog_product(ean):
    """
    Haalt catalogusgegevens op bij Bol.com voor dit EAN (titel, auteur, fabrikant,
    beschrijving, pagina's, jaar, uitvoering) — als aanvulling wanneer
    Boekwinkeltjes/ISBNdb/Google Books deze niet vonden.

    ONZEKER: dit endpoint gebruikt een generieke 'attributes'/'parties'-structuur
    waarvan Bol de exacte veldnamen niet documenteert. Deze functie zoekt op
    trefwoorden in de veldnamen, wat minder betrouwbaar is dan een exacte
    koppeling en mogelijk bijgesteld moet worden na een eerste test.

    Geeft een dict terug met evt. 'title', 'author', 'manufacturer_name',
    'manufacturer_address', 'pages', 'year', 'binding', 'description',
    'length_cm', 'width_cm', 'thickness_cm' — of None bij een fout, ontbrekende
    sleutels, of onbekend ISBN.
    """
    ean = (ean or "").strip()
    if not ean or ean == "0":
        return None

    token = _get_bol_access_token()
    if not token:
        return None

    try:
        resp = requests.get(
            f"https://api.bol.com/retailer/content/catalog-products/{ean}",
            headers={
                "Accept": "application/vnd.retailer.v10+json",
                "Accept-Language": "nl",
                "Authorization": f"Bearer {token}",
            },
            timeout=15,
        )
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    # De 'products'-wrapper is bij sommige responsvormen aanwezig, bij andere niet
    if isinstance(data, dict) and "products" in data:
        products = data.get("products") or []
        product = products[0] if products else None
    else:
        product = data
    if not product:
        return None

    result = {}

    # Auteur/fabrikant zitten waarschijnlijk in 'parties' (naam + rol)
    for party in product.get("parties") or []:
        role = (party.get("role") or party.get("type") or "").lower()
        name = party.get("name")
        if not name:
            continue
        if "auteur" in role or "author" in role or "schrijver" in role:
            result.setdefault("author", name)
        elif "fabrikant" in role or "manufacturer" in role:
            result.setdefault("manufacturer_name", name)
        elif "uitgever" in role or "publisher" in role:
            result.setdefault("manufacturer_name", result.get("manufacturer_name") or name)

    # Overige velden zitten waarschijnlijk in 'attributes' (id + waarde), op trefwoord gezocht
    bol_dimension_candidates = []
    for attr in product.get("attributes") or []:
        attr_id = (attr.get("id") or "").lower()
        values = attr.get("values") or []
        if not values:
            continue
        value = values[0].get("value")
        if not value:
            continue
        if "titel" in attr_id or "title" in attr_id:
            result.setdefault("title", value)
        elif "adres" in attr_id and ("fabrikant" in attr_id or "manufacturer" in attr_id):
            result.setdefault("manufacturer_address", value)
        elif "contact" in attr_id or "e-mail" in attr_id or "email" in attr_id:
            result.setdefault("manufacturer_contact", value)
        elif "pagina" in attr_id or "pages" in attr_id:
            result.setdefault("pages", value)
        elif "jaar" in attr_id or "verschijning" in attr_id or "publicatiedatum" in attr_id:
            year_match = re.search(r"\d{4}", str(value))
            if year_match:
                result.setdefault("year", year_match.group(0))
        elif "uitvoering" in attr_id or "bindwijze" in attr_id or "binding" in attr_id:
            result.setdefault("binding", value)
        elif "samenvatting" in attr_id or "omschrijving" in attr_id or "beschrijving" in attr_id or "description" in attr_id:
            result.setdefault("description", value)
        elif "lengte" in attr_id or "length" in attr_id or "hoogte" in attr_id or "height" in attr_id:
            parsed = _parse_single_dimension_string(value) or _parse_single_dimension_string(f"{value} cm")
            if parsed:
                bol_dimension_candidates.append(parsed)
        elif "breedte" in attr_id or "width" in attr_id:
            parsed = _parse_single_dimension_string(value) or _parse_single_dimension_string(f"{value} cm")
            if parsed:
                bol_dimension_candidates.append(parsed)
        elif "dikte" in attr_id or "thickness" in attr_id or "diepte" in attr_id or "depth" in attr_id:
            parsed = _parse_single_dimension_string(value) or _parse_single_dimension_string(f"{value} cm")
            if parsed:
                bol_dimension_candidates.append(parsed)

    if bol_dimension_candidates:
        length_cm, width_cm, thickness_cm = _assign_length_width_thickness(bol_dimension_candidates)
        if length_cm is not None:
            result["length_cm"] = length_cm
            result["width_cm"] = width_cm
            result["thickness_cm"] = thickness_cm

    return result if result else None


@st.cache_data(ttl=3600)
def lookup_bol_category_names(ean):
    """
    Haalt bij Bol.com de categorie-hiërarchie (met leesbare namen) op waarin dit
    EAN is geplaatst, via de Product Placement-endpoint. Geeft een platte lijst
    van alle categorienamen in die hiërarchie terug (van breed naar specifiek),
    of een lege lijst bij een fout/ontbrekende sleutels.
    """
    ean = (ean or "").strip()
    if not ean or ean == "0":
        return []

    token = _get_bol_access_token()
    if not token:
        return []

    try:
        resp = requests.get(
            f"https://api.bol.com/retailer/products/{ean}/placement",
            headers={
                "Accept": "application/vnd.retailer.v10+json",
                "Authorization": f"Bearer {token}",
            },
            params={"country-code": "NL"},
            timeout=15,
        )
    except requests.RequestException:
        return []
    if not resp.ok:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []

    names = []

    def _walk(node_list):
        for node in node_list or []:
            name = node.get("categoryName") or node.get("name")
            if name:
                names.append(name)
            _walk(node.get("subcategories"))

    _walk(data.get("categories"))
    return names


def find_matching_category(bol_category_names, threshold=None):
    """
    Vergelijkt de Bol-categorienamen (breed naar specifiek) met de Boekwinkeltjes-
    categorieën, en geeft de best passende (category1, category2) terug —
    category2 alleen gevuld als er ook een specifiekere subcategorie-match is
    binnen dezelfde category1 (Hobby/Topografie/Studieboeken).
    Geeft (None, None) terug als er geen voldoende goede match is.
    """
    if not bol_category_names:
        return None, None
    if threshold is None:
        threshold = int(get_setting("category_match_threshold", "90"))

    best_cat1 = None
    best_cat1_ratio = 0.0
    for bol_name in bol_category_names:
        for value, label in CATEGORY1_OPTIONS:
            ratio = SequenceMatcher(None, bol_name.lower(), label.lower()).ratio() * 100
            if ratio > best_cat1_ratio:
                best_cat1_ratio = ratio
                best_cat1 = value

    if best_cat1 is None or best_cat1_ratio < threshold:
        return None, None

    if best_cat1 in CATEGORY2_OPTIONS:
        best_cat2 = None
        best_cat2_ratio = 0.0
        for bol_name in bol_category_names:
            for value, label in CATEGORY2_OPTIONS[best_cat1]:
                if not value:
                    continue
                ratio = SequenceMatcher(None, bol_name.lower(), label.lower()).ratio() * 100
                if ratio > best_cat2_ratio:
                    best_cat2_ratio = ratio
                    best_cat2 = value
        if best_cat2 is not None and best_cat2_ratio >= threshold:
            return best_cat1, best_cat2

    return best_cat1, None


def clean_boekwinkeltjes_title_and_bijz(titel, bijz):
    """
    Boekwinkeltjes vermeldt soms een druk in de titel (bijv. 'Titel / druk1') —
    die hoort er nooit bij en wordt eraf gehaald. Gebeurt dat, dan is het
    jaartal vooraan in Bijzonderheden ook niet meer betrouwbaar en wordt dat
    ook verwijderd. Daarnaast wordt 'Gebonden' met een hoofdletter altijd naar
    kleine letters omgezet, voor consistentie.
    Geeft (titel, bijz) terug, ongewijzigd als er niets te doen was.
    """
    cleaned_titel = titel
    cleaned_bijz = bijz

    if titel:
        new_titel = re.sub(r"\s*/\s*druk\s*\d*\s*$", "", titel, flags=re.IGNORECASE).strip()
        if new_titel and new_titel != titel.strip():
            cleaned_titel = new_titel
            if bijz:
                cleaned_bijz = re.sub(r"^\s*\d{4}\s*,\s*", "", bijz)

    if cleaned_bijz:
        cleaned_bijz = cleaned_bijz.replace("Gebonden", "gebonden")

    return cleaned_titel, cleaned_bijz


# ---------- Abebooks: laagste prijs, voor boeken die elders niet worden aangeboden ----------
# LET OP: dit gebruikt een onofficieel adres van de AbeBooks-website (geen
# gedocumenteerde API) en de robots.txt van die site staat geautomatiseerde
# toegang niet toe. Het kan dus zonder waarschuwing veranderen of geblokkeerd
# worden. Daarom is alles hieronder zo gemaakt dat een mislukking nooit de pagina
# breekt, en dat we het alleen aanroepen als een boek bij Boekwinkeltjes én Bol
# nergens te koop is (dus zelden), met maximaal één verzoek per ISBN per uur.
# Er is bewust geen enkele omweg ingebouwd als het geblokkeerd wordt.
# Het antwoord van Abebooks is niet gedocumenteerd: het lezen ervan (hieronder) is
# voorzichtig en toont bij twijfel liever niets dan een verkeerd bedrag.

ABEBOOKS_PRICING_URL = "https://www.abebooks.com/servlet/DWRestService/pricingservice"


class AbebooksError(Exception):
    """De opzoeking bij Abebooks is mislukt. 'raw' bevat (een stuk van) wat Abebooks teruggaf, voor diagnose."""

    def __init__(self, message, raw=""):
        super().__init__(message)
        self.raw = raw


@st.cache_data(ttl=3600)
def _fetch_abebooks_pricing(isbn13):
    """
    Haalt het ruwe prijsantwoord van Abebooks op: één verzoek, met een korte
    time-out (het bestand waar dit op gebaseerd is heeft er geen, waardoor de
    pagina kon blijven hangen). Een mislukking geeft een AbebooksError, en die
    wordt door st.cache_data niet onthouden — een tijdelijke storing blijft dus
    niet een uur hangen. Een geslaagd antwoord (ook 'geen aanbod') wordt wel een
    uur onthouden.
    """
    payload = {
        "action": "getPricingDataByISBN",
        "isbn": isbn13,
        "container": f"pricingService-{isbn13}",
    }
    try:
        resp = requests.post(
            ABEBOOKS_PRICING_URL, data=payload, timeout=8,
            headers={"User-Agent": "Boekbeheer-prijscontrole/1.0"},
        )
    except requests.RequestException as e:
        raise AbebooksError(f"geen verbinding met Abebooks ({type(e).__name__})")
    if not resp.ok:
        raise AbebooksError(f"Abebooks antwoordde met HTTP {resp.status_code}", resp.text[:1500])
    try:
        return resp.json()
    except ValueError:
        raise AbebooksError("Abebooks gaf geen leesbaar (JSON-)antwoord", resp.text[:1500])


_ABE_CURRENCY_NAMES = {
    "USD": "US$", "US$": "US$", "EUR": "€", "€": "€", "GBP": "£", "£": "£", "CAD": "CA$", "AUD": "AU$",
}
# Sleutels waar 'price' in zit maar die niet de prijs van een aanbod zijn.
_ABE_PRICE_EXCLUDE = ("max", "high", "avg", "average", "ship", "post", "tax", "list", "retail", "msrp", "original", "discount", "saving")
# Sleutels die zelf al zeggen dat het om de laagste prijs gaat.
_ABE_LOWEST_HINTS = ("min", "low", "from", "best", "cheap", "start")
# Sleutels die een aantal gevonden boeken aangeven.
_ABE_COUNT_KEYS = ("count", "bookcount", "totalcount", "totalresults", "numresults", "resultcount", "numberofresults", "itemcount", "numbooks", "totalbooks")


def _abebooks_currency(text):
    """Maakt van een gevonden valuta-aanduiding iets veiligs om te tonen ('US$', '€', ...), of None."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.upper() in _ABE_CURRENCY_NAMES:
        return _ABE_CURRENCY_NAMES[text.upper()]
    if re.fullmatch(r"[A-Za-z$€£]{1,4}", text):  # bv. een onbekende code als 'SEK'; geen vrije tekst doorlaten
        return text
    return None


_ABE_AMOUNT_RE = re.compile(
    r"\s*(?P<pre>US\$|CA\$|AU\$|[$€£]|[A-Z]{3})?\s*(?P<num>\d[\d.,]*)\s*(?P<post>US\$|CA\$|AU\$|[$€£]|[A-Z]{3})?\s*"
)


def _abebooks_parse_amount(value):
    """
    Geeft (bedrag of None, valuta of None). Begrijpt getallen en teksten die ALLEEN
    uit een bedrag bestaan, eventueel met een valuta ervoor of erachter: 'US$ 2.40',
    '2,40', '$2.40', 'EUR 3.10' of '1.234,56'. Staat er nog iets anders in de tekst
    (andere woorden of cijfers, bijv. 'vanaf 3 verkopers: 2,40'), dan wordt er
    bewust niets gelezen: liever geen bedrag dan het verkeerde cijfer pakken.
    Een bedrag van 0 of lager telt niet.
    """
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return (float(value), None) if value > 0 else (None, None)
    if not isinstance(value, str):
        return None, None
    match = _ABE_AMOUNT_RE.fullmatch(value)
    if not match:
        return None, None
    number = match.group("num").rstrip(".,")
    if "." in number and "," in number:
        decimal_sep = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        number = number.replace(thousands_sep, "").replace(decimal_sep, ".")
    else:
        sep = "." if "." in number else ("," if "," in number else None)
        if sep:
            parts = number.split(sep)
            if len(parts) > 2 or len(parts[-1]) == 3:  # meerdere scheidingstekens, of 3 cijfers erachter: duizendtallen
                number = "".join(parts)
            else:
                number = parts[0] + "." + parts[1]
    try:
        amount = float(number)
    except ValueError:
        return None, None
    if not 0 < amount < 100000:
        return None, None
    return amount, _abebooks_currency(match.group("pre") or match.group("post"))


def _abebooks_walk(node, key="", path=""):
    """Geeft van alle 'bladeren' in het antwoord (pad, laatste sleutel, waarde)."""
    if isinstance(node, dict):
        for child_key, child in node.items():
            yield from _abebooks_walk(child, str(child_key), f"{path}.{child_key}" if path else str(child_key))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _abebooks_walk(child, key, f"{path}[{index}]")
    else:
        yield path, key, node


def parse_abebooks_pricing(data):
    """
    Leest het ruwe antwoord van Abebooks. Geeft (status, bedrag, valuta, bron):
      'price'      - laagste bedrag gevonden; 'bron' is het veld waar het uit kwam
      'none'       - er is duidelijk geen aanbod (een aantal van 0, of een lege prijs)
      'unreadable' - niets herkenbaars; liever niets tonen dan raden
    Eerst telt wat al in de sleutel zegt dat het de laagste prijs is (minPrice,
    lowestPrice, ...); anders de laagste van de overige prijsvelden. Maximum-,
    verzend-, gemiddelde- en adviesprijzen tellen niet mee.
    """
    leaves = list(_abebooks_walk(data))

    currency_hint = None
    for _, key, value in leaves:
        if "currency" in key.lower() and isinstance(value, str) and currency_hint is None:
            currency_hint = _abebooks_currency(value)

    candidates = []  # (bedrag, valuta, pad, sleutel)
    empty_price_seen = False
    for path, key, value in leaves:
        lowered = key.lower()
        if "price" not in lowered or any(word in lowered for word in _ABE_PRICE_EXCLUDE):
            continue
        amount, currency = _abebooks_parse_amount(value)
        if amount is not None:
            candidates.append((amount, currency, path, lowered))
        elif value is None or (isinstance(value, str) and value.strip() in ("", "0", "0.00", "0,00")) or value == 0:
            empty_price_seen = True

    if candidates:
        hinted = [c for c in candidates if any(hint in c[3] for hint in _ABE_LOWEST_HINTS)]
        amount, currency, path, _ = min(hinted or candidates, key=lambda c: c[0])
        return "price", amount, currency or currency_hint, path

    zero_count_seen = any(
        key.lower() in _ABE_COUNT_KEYS and str(value).strip() == "0" for _, key, value in leaves
    )
    if zero_count_seen or empty_price_seen:
        return "none", None, None, None
    return "unreadable", None, None, None


# --- Omrekenen naar euro: de dagelijkse referentiekoersen van de Europese Centrale Bank ---
# Officiële bron, gratis en zonder sleutel, bedoeld voor machines. Gepubliceerd rond
# 16:00 op elke werkdag, voor circa 30 valuta (USD, GBP, CAD, AUD, ...), als het aantal
# eenheden vreemde valuta per 1 euro. Het is een referentiekoers: je werkelijke
# omrekenkosten (bank, creditcard) liggen er altijd net even anders — vandaar '±'.

ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
# Hoe een valuta in ons vakje getoond wordt -> ISO-code bij de ECB. Een kaal '$' is
# bewust niet opgenomen: dat kan een Amerikaanse, Canadese of Australische dollar
# zijn, en dan rekenen we liever niet om dan met de verkeerde.
_CURRENCY_DISPLAY_TO_ISO = {"US$": "USD", "£": "GBP", "CA$": "CAD", "AU$": "AUD", "€": "EUR"}


class ExchangeRateError(Exception):
    """De wisselkoersen konden niet worden opgehaald of gelezen."""


def parse_ecb_rates(xml_bytes):
    """
    Leest de ECB-feed. Geeft ({ISO-code: aantal per 1 euro}, datum als 'jjjj-mm-dd').
    Weigert alles wat niet klopt (geen koersen, onleesbaar, of een bestand met
    DTD/entiteiten, dat hoort er niet in en is een bekende manier om een
    XML-lezer te misbruiken) in plaats van door te gaan met half werk.
    """
    if b"<!DOCTYPE" in xml_bytes.upper() or b"<!ENTITY" in xml_bytes.upper():
        raise ExchangeRateError("onverwachte inhoud in het koersbestand")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ExchangeRateError(f"koersbestand niet te lezen ({e})")
    rates, rate_date = {}, None
    for cube in root.iter(_ECB_NS + "Cube"):
        if "time" in cube.attrib:
            rate_date = cube.attrib["time"]
        if "currency" in cube.attrib and "rate" in cube.attrib:
            try:
                rate = float(cube.attrib["rate"])
            except ValueError:
                continue
            if rate > 0:
                rates[cube.attrib["currency"]] = rate
    if not rates:
        raise ExchangeRateError("geen koersen in het bestand")
    if not (isinstance(rate_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rate_date)):
        rate_date = "onbekende datum"
    return rates, rate_date


@st.cache_data(ttl=21600)
def _fetch_ecb_rates():
    """Haalt de koersen op (maximaal eens per 6 uur). Een mislukking wordt niet onthouden."""
    try:
        resp = requests.get(ECB_RATES_URL, timeout=8)
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "onbekend"
        raise ExchangeRateError(f"de ECB antwoordde met een foutmelding (HTTP {status})")
    except requests.RequestException as e:
        raise ExchangeRateError(f"geen verbinding met de ECB ({type(e).__name__})")
    return parse_ecb_rates(resp.content)


def convert_to_eur(amount, display_currency):
    """
    Rekent een bedrag in 'display_currency' (zoals getoond: 'US$', '£', 'CA$', 'SEK', ...)
    om naar euro. Geeft (bedrag in euro of None, toelichting). De toelichting zegt welke
    koers is gebruikt, of waarom er niet is omgerekend; die is bedoeld voor de
    technische details, niet voor het vakje zelf.
    """
    iso = _CURRENCY_DISPLAY_TO_ISO.get(display_currency)
    if iso is None and isinstance(display_currency, str) and re.fullmatch(r"[A-Z]{3}", display_currency):
        iso = display_currency  # een ISO-code als 'SEK'; de ECB publiceert er circa 30
    if iso is None:
        return None, f"'{display_currency}' is niet eenduidig genoeg om om te rekenen"
    if iso == "EUR":
        return None, "het bedrag staat al in euro"
    try:
        rates, rate_date = _fetch_ecb_rates()
    except ExchangeRateError as e:
        return None, f"omrekenen naar euro lukte niet: {e}"
    rate = rates.get(iso)
    if rate is None:
        return None, f"de ECB publiceert geen koers voor {iso}"
    rate_text = f"{rate:.4f}".rstrip("0").rstrip(".").replace(".", ",")
    return round(amount / rate, 2), f"1 euro = {rate_text} {iso} (referentiekoers van de ECB, {rate_date})"


def lookup_abebooks_lowest_price(isbn):
    """
    Zoekt het laagste bedrag voor dit ISBN bij Abebooks. Geeft altijd een dict
    terug (nooit een uitzondering), met 'status' ('price', 'none', 'unreadable' of
    'error'), 'amount', 'currency', 'source' (uit welk veld het bedrag kwam),
    'message' (bij 'error') en 'raw' (wat Abebooks teruggaf, voor controle), plus
    'amount_eur' (het omgerekende bedrag in euro, of None) en 'rate_note' (welke
    koers is gebruikt, of waarom er niet is omgerekend). Alle velden zijn altijd aanwezig.
    """
    result = {
        "status": "error", "amount": None, "currency": None, "source": None, "message": None, "raw": "",
        "amount_eur": None, "rate_note": None,
    }
    isbn13 = re.sub(r"\D", "", normalize_isbn(isbn) or "")
    if len(isbn13) != 13:
        result["message"] = "geen geldig ISBN om op te zoeken"
        return result
    try:
        data = _fetch_abebooks_pricing(isbn13)
    except AbebooksError as e:
        result["message"] = str(e)
        result["raw"] = e.raw
        return result
    result["raw"] = json.dumps(data, ensure_ascii=False, indent=1)[:2000]
    status, amount, currency, source = parse_abebooks_pricing(data)
    result.update(status=status, amount=amount, currency=currency, source=source)
    if status == "unreadable":
        result["message"] = "het antwoord van Abebooks bevat niets wat de app herkent"
    if status == "price" and currency:
        result["amount_eur"], result["rate_note"] = convert_to_eur(amount, currency)
    return result


def abebooks_box_line(result):
    """De regel voor het blauwe vakje, bijv. 'Op Abebooks is het laagste bedrag US$ 2,40'."""
    if result["status"] == "price":
        amount_text = f"{result['amount']:.2f}".replace(".", ",")
        if result["currency"]:
            line = f"Op Abebooks is het laagste bedrag {result['currency']} {amount_text}"
            if result.get("amount_eur") is not None:
                line += f" (± € {result['amount_eur']:.2f})".replace(".", ",")
            return line
        return f"Op Abebooks is het laagste bedrag {amount_text} (valuta onbekend)"
    if result["status"] == "none":
        return "Ook op Abebooks wordt dit boek momenteel niet aangeboden."
    return "Abebooks kon niet worden geraadpleegd (zie de technische details hieronder)."


# ---------- cron-job.org (overzicht van de 5 geplande taken op Home) ----------
# cron-job.org hanteert een DAGLIMIET van standaard 100 API-verzoeken per dag
# (niet per minuut) — elke ververing kost hier 6 verzoeken (1 voor de lijst + 5
# voor de geschiedenis per taak). Vandaar de relatief lange cache-tijd (6 uur —
# dit hoeft niet super actueel te zijn), zodat regelmatig bezoek aan Home
# gedurende de dag niet alsnog tegen die daglimiet aanloopt.

CRON_JOB_API_BASE = "https://api.cron-job.org"


# De statuscodes van cron-job.org voor de laatste uitvoering van een taak (zie de REST API-documentatie, 'JobStatus').
CRON_JOB_STATUS_LABELS = {
    0: "Nog niet gedraaid",
    1: "✅ Geslaagd",
    2: "⚠️ Mislukt (DNS-fout)",
    3: "⚠️ Mislukt (geen verbinding)",
    4: "⚠️ Mislukt (HTTP-fout)",
    5: "⚠️ Mislukt (time-out)",
    6: "⚠️ Mislukt (te veel antwoord)",
    7: "⚠️ Mislukt (ongeldige URL)",
    8: "⚠️ Mislukt (interne fout bij cron-job.org)",
    9: "⚠️ Mislukt (onbekende reden)",
    10: "⚠️ Mislukt (controlepagina)",
}


def cron_status_label(code):
    """De tekst voor een statuscode. Een code die we niet kennen toont de code zelf, nooit een streepje: dat zou een mislukking verbergen."""
    if code is None:
        return "–"
    return CRON_JOB_STATUS_LABELS.get(code, f"⚠️ Onbekende status ({code})")


def failed_cron_jobs(jobs):
    """De taken waarvan de laatste uitvoering mislukte: elke code vanaf 2 (ook een code die we nog niet kennen)."""
    return [job for job in jobs if isinstance(job.get("lastStatus"), int) and job["lastStatus"] >= 2]


@st.cache_data(ttl=21600)
def get_cron_job_status():
    """
    Haalt de lijst van alle cron-job.org-taken op, met per taak: titel, laatste
    uitvoering (tijdstip, status, duur in ms) en eerstvolgende uitvoering.
    Geeft (taken, foutmelding) terug — foutmelding is None bij succes, zodat
    Home kan tonen wat er precies misging in plaats van alleen 'niets gevonden'.
    """
    api_key = _get_secret("CRON_JOB_API_KEY")
    if not api_key:
        return [], "CRON_JOB_API_KEY is niet ingesteld."
    try:
        resp = requests.get(
            f"{CRON_JOB_API_BASE}/jobs",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if not resp.ok:
            return [], f"cron-job.org gaf {resp.status_code} terug: {resp.text[:300]}"
        return resp.json().get("jobs", []), None
    except requests.RequestException as e:
        return [], f"Netwerkfout: {e}"


@st.cache_data(ttl=21600)
def get_cron_job_history(job_id, limit=20):
    """
    Haalt de laatste uitvoeringen van één cron-job.org-taak op (meest recente
    eerst), voor het duur-grafiekje. Geeft een lege lijst terug bij een
    ontbrekende sleutel of een mislukte opzoeking.

    cron-job.org hanteert vooral een daglimiet (zie hierboven), maar noemt
    daarnaast dat er ook losse, per-eindpunt snelheidslimieten kunnen gelden.
    Deze functie wordt per taak apart aangeroepen (dus meerdere keren vlak na
    elkaar bij het laden van Home) — de korte wachttijd hieronder spreidt die
    verzoeken voor de zekerheid over de tijd uit. Geldt alleen bij een echte
    (nog niet gecachete) opzoeking.
    """
    api_key = _get_secret("CRON_JOB_API_KEY")
    if not api_key:
        return []
    time.sleep(13)
    try:
        resp = requests.get(
            f"{CRON_JOB_API_BASE}/jobs/{job_id}/history",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if not resp.ok:
            return []
        return resp.json().get("history", [])[:limit]
    except requests.RequestException:
        return []

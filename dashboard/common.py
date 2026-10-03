"""
common.py — Gedeelde functies voor alle dashboard-pagina's (app.py + pages/*.py).
"""

import os
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # leest .env, ook als die in de bovenliggende (project)map staat

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")


import base64
import hashlib
import os
import random
import re
import secrets
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

    if resp.status_code == 204:
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
    set_clause = ", ".join(f"{col} = %({col})s" for col in fields)
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
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

            cur.execute(
                f"""
                UPDATE books
                SET {set_clause}, pending_push = TRUE, local_updated_at = %(now)s
                WHERE id = %(id)s
                """,
                {**fields, "now": pd.Timestamp.utcnow(), "id": int(book_id)},
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
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
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
                SELECT id, image_data, content_type FROM book_uploaded_images
                WHERE book_id = %(book_id)s
                ORDER BY position ASC
                """,
                {"book_id": book_id},
            )
            for row_id, image_data, content_type in cur.fetchall():
                data_uri = f"data:{content_type};base64,{base64.b64encode(bytes(image_data)).decode('ascii')}"
                results.append(
                    {
                        "url_large": data_uri,
                        "url_medium": data_uri,
                        "url_small": data_uri,
                        "source": "uploaded",
                        "ref_id": row_id,
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
                            "source": "confirmed",
                            "ref_id": image_id,
                        }
                    )
    finally:
        conn.close()
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
            for position, img in enumerate(images):
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


def determine_busstuk(length_cm, thickness_cm, shipping_options_eur):
    """
    Bepaalt of een boek met deze afmetingen als 'busstuk' kan worden verstuurd
    (in plaats van een pakje), en welke verzendkosten daarbij default zouden
    moeten zijn. 'shipping_options_eur' is de lijst met numerieke
    verzendkosten-keuzes waartussen gekozen wordt (bijv. [3.75, 7.25]) — bij
    'Nee' wordt de hoogste daarvan gekozen, bij 'Ja' de laagste, zodat dit blijft
    kloppen als die keuzes ooit wijzigen. Geeft (is_busstuk, shipping_cost,
    toelichtende tekst) terug — alle None als er geen afmetingen bekend zijn.
    """
    if length_cm is None or thickness_cm is None or not shipping_options_eur:
        return None, None, None
    is_too_groot = length_cm > BUSSTUK_LENGTH_LIMIT_CM and thickness_cm > BUSSTUK_THICKNESS_LIMIT_CM
    is_busstuk = not is_too_groot
    shipping_cost = min(shipping_options_eur) if is_busstuk else max(shipping_options_eur)
    length_str = f"{length_cm:.1f}".replace(".", ",")
    thickness_str = f"{thickness_cm:.1f}".replace(".", ",")
    if is_busstuk:
        message = f"Ja, het is {length_str} cm lang en {thickness_str} cm dik. Dit is een busstuk."
    else:
        message = f"Nee, het is {length_str} cm lang en {thickness_str} cm dik. Dit is een pakje."
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
            result["cover_url"] = isbndb_data["cover_url"]
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
                if cover_url and "cover_url" not in result:
                    result["cover_url"] = cover_url
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

    # 3) Open Library als laatste terugval, vooral voor de omslagfoto
    if "cover_url" not in result:
        ol_cover = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
        try:
            head = requests.head(ol_cover, timeout=10, allow_redirects=True)
            if head.ok and int(head.headers.get("Content-Length", "0")) > 1000:
                result["cover_url"] = ol_cover
                result.setdefault("source", "Open Library")
        except requests.RequestException:
            pass

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

    # De omslagfoto direct als bytes ophalen, zodat de pagina zelf geen netwerkcode nodig heeft
    if "cover_url" in result:
        try:
            img_resp = requests.get(result["cover_url"], timeout=10)
            if img_resp.ok and img_resp.content:
                result["cover_bytes"] = img_resp.content
                result["cover_content_type"] = img_resp.headers.get("Content-Type", "image/jpeg")
        except requests.RequestException:
            pass
        del result["cover_url"]

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
            # Boekwinkeltjes heeft niets, maar Bol wel: prijs voorstellen op basis
            # van Bol's laagste prijs (min €2,25 marge, min de verzendkosten —
            # standaard €3,75 hier — afgerond naar beneden op ,45 of ,95, met
            # €3,95 als bodem), in plaats van de kale €0.
            try:
                suggested = suggest_bulk_price(float(bol_laagste) - 2.25 - 3.75)
                if suggested is not None:
                    fields["price"] = suggested
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
    # als laatste terugval. Op basis daarvan de verzendkosten bepalen (busstuk
    # vs. pakje) — alleen als er daadwerkelijk afmetingen gevonden zijn; anders
    # blijft de aanroeper zijn eigen standaardbedrag (3,75) gebruiken.
    length_cm = width_cm = thickness_cm = None
    if metadata and metadata.get("length_cm"):
        length_cm, width_cm, thickness_cm = metadata["length_cm"], metadata.get("width_cm"), metadata["thickness_cm"]
    elif bol_product and bol_product.get("length_cm"):
        length_cm, width_cm, thickness_cm = bol_product["length_cm"], bol_product.get("width_cm"), bol_product["thickness_cm"]

    if length_cm is not None:
        fields["length_cm"] = length_cm
        fields["width_cm"] = width_cm
        fields["thickness_cm"] = thickness_cm
        _, shipping_cost, _ = determine_busstuk(length_cm, thickness_cm, [3.75, 7.25])
        if shipping_cost is not None:
            fields["shipping_cost"] = shipping_cost

    return fields


def get_queued_books():
    """Boeken die in de wachtrij staan (na bulk-import), meest recent eerst."""
    conn = _dict_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, author, ean FROM books WHERE queued = TRUE ORDER BY id DESC")
            return cur.fetchall()
    finally:
        conn.close()


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


def get_database_size_mb():
    """Huidige grootte van de Postgres-database in MB (via Postgres' eigen pg_database_size)."""
    conn = psycopg2.connect(get_db_url())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            size_bytes = cur.fetchone()[0]
            return size_bytes / (1024 * 1024)
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


# ---------- cron-job.org (overzicht van de 5 geplande taken op Home) ----------
# Rate limit bij cron-job.org: 1 verzoek/seconde, 5 verzoeken/minuut — vandaar de
# relatief lange cache-tijd (5 minuten), zodat herhaaldelijk verversen van Home
# niet per ongeluk tegen die limiet aanloopt.

CRON_JOB_API_BASE = "https://api.cron-job.org"


@st.cache_data(ttl=300)
def get_cron_job_status():
    """
    Haalt de lijst van alle cron-job.org-taken op, met per taak: titel, laatste
    uitvoering (tijdstip, status, duur in ms) en eerstvolgende uitvoering. Geeft
    een lege lijst terug als CRON_JOB_API_KEY niet is ingesteld of de opzoeking
    om wat voor reden dan ook mislukt (zodat Home nooit stukloopt hierop).
    """
    api_key = _get_secret("CRON_JOB_API_KEY")
    if not api_key:
        return []
    try:
        resp = requests.get(
            f"{CRON_JOB_API_BASE}/jobs",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if not resp.ok:
            return []
        return resp.json().get("jobs", [])
    except requests.RequestException:
        return []


@st.cache_data(ttl=300)
def get_cron_job_history(job_id, limit=20):
    """
    Haalt de laatste uitvoeringen van één cron-job.org-taak op (meest recente
    eerst), voor het duur-grafiekje. Geeft een lege lijst terug bij een
    ontbrekende sleutel of een mislukte opzoeking.
    """
    api_key = _get_secret("CRON_JOB_API_KEY")
    if not api_key:
        return []
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

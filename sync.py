"""
sync.py — Synchroniseert boeken en orders tussen Boekwinkeltjes en Supabase (Postgres).

Volgorde per sync-run: eerst PULL (ophalen wat er op de site is veranderd),
dan PUSH (lokale wijzigingen wegschrijven). Zo overschrijf je niet per ongeluk
een wijziging die je net op de site zelf hebt gemaakt.
"""

import datetime as dt
import csv
import hashlib
import io
import json
import os
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import psycopg2
import requests

import api_client
import bol_client
import notifications
from db import get_connection, init_db


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Hoeveel rijen een pull per keer opslaat (commit). Een transactie houdt alle rijen die ze heeft aangepast vast tot ze
# klaar is; bij één grote transactie over alle ~3500 boeken kan een gelijktijdige wijziging (in het dashboard of door
# een andere job) daardoor lang blijven wachten en op een 'statement timeout' stuklopen. Elke 200 rijen opslaan
# houdt dat kort. Een pull is herhaalbaar (alles is een 'upsert'), dus tussentijds opgeslagen werk is nooit een probleem.
PULL_COMMIT_EVERY = 200


def _n(count, singular, plural):
    """Enkelvoud bij precies 1, anders meervoud (dus ook bij 0)."""
    return singular if count == 1 else plural


# ---------- Verzendformaat (Boekwinkeltjes-veld 'shippingFormat') ----------

# Waarden zoals in Boekwinkeltjes' eigen keuzelijst (de API-documentatie noemt alleen 0-4, zonder uitleg).
SHIPPING_FORMAT_PICKUP = 0
SHIPPING_FORMAT_MAILBOX = 1       # brievenbuspakje
SHIPPING_FORMAT_SMALL_PARCEL = 2
SHIPPING_FORMAT_PARCEL = 3        # normaal pakket
SHIPPING_FORMAT_LARGE_PARCEL = 4
SHIPPING_FORMAT_LABELS = {
    0: "Alleen afhalen mogelijk",
    1: "Brievenbuspakje",
    2: "Klein pakket",
    3: "Normaal pakket",
    4: "Groot of zwaar pakket",
}
SHIPPING_DEFAULT_BRIEFPOST = 3.95
# Oude briefpost-bedragen: 3,75 was het vorige tarief en 1,40 de oude standaardwaarde (die bleef staan als je niets
# aanpaste, bijvoorbeeld bij het wijzigen van een afbeelding). Boeken met zo'n bedrag zijn briefpost.
SHIPPING_LEGACY_BRIEFPOST_AMOUNTS = (3.75, 1.40)
SHIPPING_DEFAULT_PAKKETPOST = 7.25


def shipping_format_for_cost(cost, briefpost, pakketpost):
    """
    Het verzendformaat dat bij de verzendkosten van een boek hoort: precies de briefpost-kosten (of een oud
    briefpost-bedrag, zie SHIPPING_LEGACY_BRIEFPOST_AMOUNTS) -> brievenbuspakje (1), precies de pakketpost-kosten ->
    normaal pakket (3). Elk ander bedrag (of geen
    bedrag) -> None: daar gokken we niet op.
    """
    if cost is None:
        return None
    try:
        cost = round(float(cost), 2)
    except (TypeError, ValueError):
        return None
    if cost == round(float(briefpost), 2):
        return SHIPPING_FORMAT_MAILBOX
    if cost == round(float(pakketpost), 2):
        return SHIPPING_FORMAT_PARCEL
    if cost in SHIPPING_LEGACY_BRIEFPOST_AMOUNTS:
        return SHIPPING_FORMAT_MAILBOX
    return None


def _format_for_row(row, costs):
    """Het verzendformaat van een boekrij: de opgeslagen waarde, anders afgeleid uit de verzendkosten (zie shipping_format_for_cost)."""
    fmt = row.get("shipping_format")
    if fmt is None:
        fmt = shipping_format_for_cost(row.get("shipping_cost"), costs[0], costs[1])
    return fmt


_SHIPPING_FORMAT_COLUMN_CHECKED = False


def ensure_shipping_format_column(conn):
    """
    Zorgt dat books.shipping_format bestaat. Eerst wordt gekeken OF de kolom er al is: een ALTER TABLE vraagt
    namelijk altijd een exclusief slot op de tabel, ook als er niets te wijzigen valt, en dat kan blijven
    wachten achter een lopende sync (en dan loopt het af op 'statement timeout'). Alleen als de kolom echt
    ontbreekt wordt hij aangemaakt. Per programma-run wordt het maar één keer gecontroleerd. De aanroeper commit.
    """
    global _SHIPPING_FORMAT_COLUMN_CHECKED
    if _SHIPPING_FORMAT_COLUMN_CHECKED:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'books' AND column_name = 'shipping_format'"
        )
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute("ALTER TABLE books ADD COLUMN IF NOT EXISTS shipping_format INTEGER")
    _SHIPPING_FORMAT_COLUMN_CHECKED = True


def get_shipping_costs_setting(conn):
    """(briefpost, pakketpost) zoals ingesteld op 'Hulp en instellingen'; onleesbaar of niet ingesteld = standaardbedrag."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('bw_shipping_briefpost', 'bw_shipping_pakketpost')"
        )
        stored = {row["key"] if isinstance(row, dict) else row[0]: row["value"] if isinstance(row, dict) else row[1]
                  for row in cur.fetchall()}

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


def _log(conn, direction, resource, status, detail="", platform="BW"):
    # Timing loopt niet meer via GitHub's eigen 'schedule'-trigger (onbetrouwbaar
    # gebleken) maar via cron-job.org, dat de workflow aanroept als 'workflow_dispatch'
    # — hetzelfde event-type als een handmatige klik in de Actions-tab. Om toch
    # onderscheid te houden, stuurt cron-job.org een extra input 'trigger_source' mee,
    # die de workflow doorzet als de TRIGGER_SOURCE-omgevingsvariabele.
    trigger = "Gepland" if os.environ.get("TRIGGER_SOURCE") == "cron-job.org" else "Handmatig"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_log (run_at, direction, resource, status, detail, trigger, platform)
            VALUES (%(run_at)s, %(direction)s, %(resource)s, %(status)s, %(detail)s, %(trigger)s, %(platform)s)
            """,
            {
                "run_at": _now(),
                "direction": direction,
                "resource": resource,
                "status": status,
                "detail": detail,
                "trigger": trigger,
                "platform": platform,
            },
        )
    if status == "error":
        try:
            notifications.send_email(
                subject=f"⚠️ Boekbeheersysteem: sync-fout ({platform} — {resource})",
                body=(
                    f"Er ging iets mis bij een synchronisatie.\n\n"
                    f"Richting: {direction}\n"
                    f"Onderdeel: {resource}\n"
                    f"Platform: {platform}\n"
                    f"Moment: {_now()}\n\n"
                    f"Details:\n{detail}\n\n"
                    f"Bekijk 'Laatste sync-runs' op Home voor meer context."
                ),
            )
        except Exception:
            pass  # een mislukte e-mail mag de sync zelf nooit laten crashen


# ---------- PULL ----------

def _normalize_language(raw):
    """
    Normaliseert het 'language'-veld: altijd lowercase, met twee uitzonderingen
    die allebei op 'en' uitkomen: GB en MET.
    """
    if not raw:
        return raw
    upper = raw.strip().upper()
    special = {"GB": "en", "MET": "en"}
    if upper in special:
        return special[upper]
    return raw.strip().lower()


def _split_publisher(raw):
    """
    Splitst het 'publisher'-veld op ';' in (naam, adres, contact).
    Geen ';' aanwezig -> alles in naam, adres/contact = "Onbekend".
    Wel ';' aanwezig maar een deel ontbreekt/is leeg -> dat deel = "Onbekend".
    """
    if not raw or ";" not in raw:
        return raw, "Onbekend", "Onbekend"

    parts = [p.strip() for p in raw.split(";")]
    name = parts[0] if len(parts) > 0 and parts[0] else "Onbekend"
    address = parts[1] if len(parts) > 1 and parts[1] else "Onbekend"
    contact = parts[2] if len(parts) > 2 and parts[2] else "Onbekend"
    return name, address, contact


def pull_books():
    conn = get_connection()
    count = 0
    seen_ids = []
    try:
        ensure_shipping_format_column(conn)
        conn.commit()
        with conn.cursor() as cur:
            for book in api_client.iter_all_books():
                publisher_name, publisher_address, publisher_contact = _split_publisher(
                    book.get("publisher")
                )
                params = {
                    "id": book.get("id"),
                    "bookNumber": book.get("bookNumber"),
                    "location": book.get("location"),
                    "amount": book.get("amount"),
                    "category1": book.get("category1"),
                    "category2": book.get("category2"),
                    "category3": book.get("category3"),
                    "language": _normalize_language(book.get("language")),
                    "author": book.get("author"),
                    "title": book.get("title"),
                    "publisher": book.get("publisher"),
                    "publisher_name": publisher_name,
                    "publisher_address": publisher_address,
                    "publisher_contact": publisher_contact,
                    "ean": book.get("ean"),
                    "shortDescription": book.get("shortDescription"),
                    "longDescription": book.get("longDescription"),
                    "price": book.get("price"),
                    "shippingCost": book.get("shippingCost"),
                    "shippingCategory": book.get("shippingCategory"),
                    # Geeft de API het veld niet mee (sleutel ontbreekt), dan laten we de lokale waarde ongemoeid.
                    "shippingFormat": book.get("shippingFormat"),
                    "sf_known": "shippingFormat" in book,
                    "date": book.get("date"),
                    "weblink": book.get("weblink"),
                    "last_synced_at": _now(),
                }
                cur.execute(
                    """
                    INSERT INTO books (
                        id, book_number, location, amount, category1, category2, category3,
                        language, author, title, publisher, publisher_name, publisher_address, publisher_contact,
                        ean, short_description, long_description,
                        price, shipping_cost, shipping_category, shipping_format, listing_date, weblink, last_synced_at
                    ) VALUES (%(id)s, %(bookNumber)s, %(location)s, %(amount)s, %(category1)s, %(category2)s, %(category3)s,
                        %(language)s, %(author)s, %(title)s, %(publisher)s, %(publisher_name)s, %(publisher_address)s, %(publisher_contact)s,
                        %(ean)s, %(shortDescription)s, %(longDescription)s,
                        %(price)s, %(shippingCost)s, %(shippingCategory)s, %(shippingFormat)s, %(date)s, %(weblink)s, %(last_synced_at)s)
                    ON CONFLICT (id) DO UPDATE SET
                        book_number=excluded.book_number, location=excluded.location, amount=excluded.amount,
                        category1=excluded.category1, category2=excluded.category2, category3=excluded.category3,
                        language=excluded.language, author=excluded.author, title=excluded.title,
                        publisher=excluded.publisher, publisher_name=excluded.publisher_name,
                        publisher_address=excluded.publisher_address, publisher_contact=excluded.publisher_contact,
                        ean=excluded.ean,
                        short_description=excluded.short_description, long_description=excluded.long_description,
                        price=excluded.price, shipping_cost=excluded.shipping_cost,
                        shipping_category=excluded.shipping_category,
                        shipping_format=CASE WHEN %(sf_known)s THEN excluded.shipping_format ELSE books.shipping_format END,
                        listing_date=excluded.listing_date,
                        weblink=excluded.weblink, last_synced_at=excluded.last_synced_at
                    WHERE books.pending_push = FALSE
                    """,
                    params,
                )
                count += 1
                seen_ids.append(book.get("id"))
                if count % PULL_COMMIT_EVERY == 0:
                    conn.commit()

            # Boeken die lokaal nog bestaan (met een echt, positief id) maar niet meer
            # in deze volledige pull voorkwamen, zijn kennelijk uitverkocht of bij
            # Boekwinkeltjes verwijderd. Die halen we NIET lokaal weg — voorraad
            # gaat gewoon op 0, zodat de overzichten (die op 'amount' filteren)
            # dit boek meteen als niet-op-voorraad behandelen.
            marked_not_in_stock = 0
            if seen_ids:
                cur.execute(
                    """
                    UPDATE books
                    SET amount = 0
                    WHERE id > 0 AND pending_push = FALSE AND COALESCE(amount, 0) != 0
                      AND NOT (id = ANY(%(seen_ids)s))
                    RETURNING id
                    """,
                    {"seen_ids": seen_ids},
                )
                marked_not_in_stock = len(cur.fetchall())
            cur.execute(
                "SELECT COUNT(*) AS n FROM books WHERE id > 0 AND push_enabled = TRUE AND shipping_format IS NULL"
            )
            no_format = cur.fetchone()["n"]
        conn.commit()
        detail = f"{count} {_n(count, 'boek', 'boeken')} verwerkt, {marked_not_in_stock} op voorraad 0 gezet (niet meer bij Boekwinkeltjes)"
        if no_format:
            detail += f"; {no_format} {_n(no_format, 'boek heeft', 'boeken hebben')} nog geen verzendformaat"
        _log(conn, "pull", "books", "ok", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "books", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return count


def pull_orders():
    conn = get_connection()
    count = 0
    try:
        with conn.cursor() as cur:
            for order in api_client.iter_all_orders():
                book = order.get("book") or {}
                buyer = order.get("buyer") or {}
                address = buyer.get("address") or {}

                cur.execute(
                    """
                    INSERT INTO orders (
                        id, order_date, status, online_payment_status, book_id,
                        book_title, book_author, book_price, book_shipping_cost, book_ean,
                        buyer_first_name, buyer_last_name, buyer_name, buyer_phone, buyer_email,
                        buyer_language, buyer_street, buyer_number, buyer_number_extra,
                        buyer_zip_code, buyer_city, buyer_country, buyer_company, note, last_synced_at
                    ) VALUES (%(id)s, %(date)s, %(status)s, %(onlinePaymentStatus)s, %(book_id)s,
                        %(book_title)s, %(book_author)s, %(book_price)s, %(book_shipping_cost)s, %(book_ean)s,
                        %(first_name)s, %(last_name)s, %(name)s, %(phone)s, %(email)s,
                        %(language)s, %(street)s, %(number)s, %(number_extra)s,
                        %(zip_code)s, %(city)s, %(country)s, %(company)s, %(note)s, %(last_synced_at)s)
                    ON CONFLICT (id) DO UPDATE SET
                        order_date=excluded.order_date, status=excluded.status,
                        online_payment_status=excluded.online_payment_status, book_id=excluded.book_id,
                        book_title=excluded.book_title, book_author=excluded.book_author,
                        book_price=excluded.book_price, book_shipping_cost=excluded.book_shipping_cost,
                        book_ean=excluded.book_ean,
                        buyer_first_name=excluded.buyer_first_name, buyer_last_name=excluded.buyer_last_name,
                        buyer_name=excluded.buyer_name, buyer_phone=excluded.buyer_phone,
                        buyer_email=excluded.buyer_email, buyer_language=excluded.buyer_language,
                        buyer_street=excluded.buyer_street, buyer_number=excluded.buyer_number,
                        buyer_number_extra=excluded.buyer_number_extra, buyer_zip_code=excluded.buyer_zip_code,
                        buyer_city=excluded.buyer_city, buyer_country=excluded.buyer_country,
                        buyer_company=excluded.buyer_company, note=excluded.note,
                        last_synced_at=excluded.last_synced_at
                    """,
                    {
                        "id": order.get("id"),
                        "date": order.get("date"),
                        "status": order.get("status"),
                        "onlinePaymentStatus": order.get("onlinePaymentStatus"),
                        "book_id": book.get("id"),
                        "book_title": book.get("title"),
                        "book_author": book.get("author"),
                        "book_price": book.get("price"),
                        "book_shipping_cost": book.get("shippingCost"),
                        "book_ean": book.get("ean"),
                        "first_name": buyer.get("firstName"),
                        "last_name": buyer.get("lastName"),
                        "name": buyer.get("name"),
                        "phone": buyer.get("phone") or buyer.get("phoneNumber"),
                        "email": buyer.get("email"),
                        "language": buyer.get("language"),
                        "street": address.get("street"),
                        "number": address.get("number"),
                        "number_extra": address.get("numberExtra"),
                        "zip_code": address.get("zipCode"),
                        "city": address.get("city"),
                        "country": address.get("country"),
                        "company": address.get("company"),
                        "note": buyer.get("note"),
                        "last_synced_at": _now(),
                    },
                )
                count += 1
                if count % PULL_COMMIT_EVERY == 0:
                    conn.commit()
        conn.commit()
        _log(conn, "pull", "orders", "ok", f"{count} {_n(count, 'order', 'orders')} verwerkt")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "orders", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return count


# ---------- PUSH ----------

def push_pending_books():
    """Stuurt lokaal gewijzigde boeken (pending_push = TRUE) naar Boekwinkeltjes."""
    conn = get_connection()
    count = 0
    skipped_gone = 0
    errors = []
    try:
        ensure_shipping_format_column(conn)
        conn.commit()
        costs = get_shipping_costs_setting(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM books WHERE pending_push = TRUE AND pending_create = FALSE AND push_enabled = TRUE"
            )
            rows = cur.fetchall()
            for row in rows:
                shipping_format = _format_for_row(row, costs)
                payload = {
                    "bookNumber": row["book_number"],
                    "location": row["location"],
                    "amount": row["amount"],
                    "category1": row["category1"],
                    "category2": row["category2"],
                    "category3": row["category3"],
                    "language": row["language"],
                    "author": row["author"],
                    "title": row["title"],
                    "publisher": row["publisher"],
                    "ean": row["ean"],
                    "shortDescription": row["short_description"],
                    "longDescription": row["long_description"],
                    "price": float(row["price"]) if row["price"] is not None else None,
                    "shippingCost": float(row["shipping_cost"]) if row["shipping_cost"] is not None else None,
                    "shippingCategory": row["shipping_category"],
                    "shippingFormat": shipping_format,
                    "weblink": row["weblink"],
                }
                # Geen lege/None velden meesturen die de API mogelijk niet accepteert
                payload = {k: v for k, v in payload.items() if v is not None}
                try:
                    api_client.update_book(row["id"], payload)
                    cur.execute(
                        "UPDATE books SET pending_push = FALSE, last_synced_at = %(now)s, "
                        "shipping_format = COALESCE(shipping_format, %(sf)s) WHERE id = %(id)s",
                        {"now": _now(), "id": row["id"], "sf": shipping_format},
                    )
                    count += 1
                    # Per boek opslaan: tussen twee aanroepen naar Boekwinkeltjes (traag) mogen we geen rijen
                    # vasthouden, en een later probleem mag al verstuurde boeken niet weer 'ongedaan' maken
                    # (dan zouden ze bij de volgende sync nogmaals worden verstuurd).
                    conn.commit()
                except api_client.BoekwinkeltjesAPIError as e:
                    if "-> 404:" in str(e):
                        # Boek bestaat niet meer bij Boekwinkeltjes (bijv. daar handmatig
                        # verwijderd). Nogmaals proberen heeft geen zin — synchronisatie
                        # voor dit boek uitzetten, maar de rest van de batch NIET blokkeren.
                        cur.execute(
                            "UPDATE books SET pending_push = FALSE, push_enabled = FALSE WHERE id = %(id)s",
                            {"id": row["id"]},
                        )
                        skipped_gone += 1
                        conn.commit()
                    else:
                        errors.append(f"boek {row['id']}: {e}")
        conn.commit()
        detail = f"{count} {_n(count, 'boek', 'boeken')} gepusht"
        if skipped_gone:
            detail += f", {skipped_gone} {_n(skipped_gone, 'boek', 'boeken')} bestaat/bestaan niet meer bij Boekwinkeltjes (synchronisatie uitgezet)"
        if errors:
            detail += f" — {len(errors)} fout(en): " + "; ".join(errors[:5])
        _log(conn, "push", "books", "ok" if not errors else "error", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "books", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return count


# Eenmalige job: het nieuwe verplichte veld 'verzendformaat' bij Boekwinkeltjes vullen voor bestaande boeken.
SHIPPING_FORMAT_BACKFILL_MAX_SECONDS = 25 * 60   # daarna netjes stoppen; een volgende run gaat verder waar deze ophield
SHIPPING_FORMAT_BACKFILL_PAUSE = 0.2             # seconden tussen twee aanroepen, om Boekwinkeltjes niet te overspoelen
SHIPPING_FORMAT_BACKFILL_MAX_CONSECUTIVE_ERRORS = 3


def backfill_shipping_format(real=False, briefpost=None, pakketpost=None, now_func=time.monotonic, sleep_func=time.sleep):
    """
    Vult het verzendformaat bij Boekwinkeltjes voor bestaande boeken zonder verzendformaat, op basis van de
    verzendkosten: precies de briefpost-kosten (en de oude bedragen 3,75 en 1,40) -> Brievenbuspakje (1), precies de pakketpost-kosten ->
    Normaal pakket (3). Boeken met een ander bedrag worden niet aangeraakt, maar wel getoond.

    Zonder real=True is het een proefrun: er wordt niets verstuurd of opgeslagen, alleen getoond wat er zou gebeuren.
    Met real=True gaat per boek eerst een uitleesaanvraag en dan één kleine wijziging (shippingFormat plus de
    prijs zoals Boekwinkeltjes die zelf geeft, want die eist de API bij elke wijziging) naar Boekwinkeltjes; pas als dat
    gelukt is, wordt het formaat ook lokaal bewaard. Daardoor is de job veilig opnieuw te draaien: wat klaar is
    wordt overgeslagen, en wat mislukte of door de tijdslimiet bleef liggen komt bij de volgende run aan bod.

    Bij het eerste boek wordt gecontroleerd of Boekwinkeltjes de waarde ook echt heeft overgenomen; als dat niet
    zo is, stopt de job meteen (zodat een verkeerd begrepen API niet 3000 keer dezelfde fout maakt).
    Geeft een lijst met regels tekst terug.
    """
    lines = []
    conn = get_connection()
    try:
        ensure_shipping_format_column(conn)
        conn.commit()
        settings_brief, settings_pakket = get_shipping_costs_setting(conn)
        briefpost = settings_brief if briefpost is None else round(float(briefpost), 2)
        pakketpost = settings_pakket if pakketpost is None else round(float(pakketpost), 2)
        lines.append(
            f"Briefpost = €{briefpost:.2f} -> {SHIPPING_FORMAT_LABELS[1]} (1); "
            f"pakketpost = €{pakketpost:.2f} -> {SHIPPING_FORMAT_LABELS[3]} (3). "
            f"Oude briefpost-bedragen ({', '.join(f'€{a:.2f}' for a in SHIPPING_LEGACY_BRIEFPOST_AMOUNTS)}) tellen ook als {SHIPPING_FORMAT_LABELS[1]}."
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, shipping_cost FROM books
                WHERE id > 0 AND pending_create = FALSE AND push_enabled = TRUE AND shipping_format IS NULL
                ORDER BY id
                """
            )
            rows = cur.fetchall()

        todo = []        # (id, formaat)
        skipped = {}     # verzendkosten -> aantal
        for row in rows:
            fmt = shipping_format_for_cost(row["shipping_cost"], briefpost, pakketpost)
            if fmt is None:
                key = "geen bedrag" if row["shipping_cost"] is None else f"€{float(row['shipping_cost']):.2f}"
                skipped[key] = skipped.get(key, 0) + 1
            else:
                todo.append((row["id"], fmt))

        n_mailbox = sum(1 for _, f in todo if f == SHIPPING_FORMAT_MAILBOX)
        n_parcel = sum(1 for _, f in todo if f == SHIPPING_FORMAT_PARCEL)
        lines.append(f"{len(rows)} {_n(len(rows), 'boek', 'boeken')} zonder verzendformaat gevonden.")
        lines.append(f"  - {n_mailbox} -> {SHIPPING_FORMAT_LABELS[1]}")
        lines.append(f"  - {n_parcel} -> {SHIPPING_FORMAT_LABELS[3]}")
        if skipped:
            lines.append("  - NIET aangeraakt (verzendkosten komen niet overeen met briefpost of pakketpost):")
            for key, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
                lines.append(f"      {n} x {key}")

        if not real:
            lines.append("Proefrun: er is niets verstuurd of opgeslagen. Draai opnieuw met echt_uitvoeren = ja om het door te voeren.")
            return lines
        if not todo:
            lines.append("Niets te doen.")
            _log(conn, "push", "shipping_format", "ok", "niets te doen")
            conn.commit()
            return lines

        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '3s'")
        conn.commit()
        started = now_func()
        done = 0
        gone = 0
        errors = []
        consecutive_errors = 0
        verified = False
        stopped_for_time = False
        aborted = False
        local_skipped = 0
        for book_id, fmt in todo:
            if now_func() - started > SHIPPING_FORMAT_BACKFILL_MAX_SECONDS:
                stopped_for_time = True
                break
            try:
                # Boekwinkeltjes eist bij elke wijziging ook de prijs ("price: validation.required"). Daarom eerst het
                # boek zoals het NU bij hen staat ophalen en precies die prijs teruggeven: zo verandert er niets
                # aan de prijs, ook niet als die net op de site is aangepast en onze kopie nog achterloopt.
                current = api_client.get_book(book_id)
                current_data = current.get("data", current) if isinstance(current, dict) else {}
                current_price = current_data.get("price") if isinstance(current_data, dict) else None
                if current_price is None:
                    raise api_client.BoekwinkeltjesAPIError(
                        f"GET boek {book_id}: Boekwinkeltjes gaf geen prijs terug; niets verstuurd"
                    )
                api_client.update_book(book_id, {"price": current_price, "shippingFormat": fmt})
            except api_client.BoekwinkeltjesAPIError as e:
                if "-> 404:" in str(e):
                    gone += 1  # bestaat niet meer bij Boekwinkeltjes; de gewone sync ruimt dat op
                    continue
                errors.append(f"boek {book_id}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= SHIPPING_FORMAT_BACKFILL_MAX_CONSECUTIVE_ERRORS:
                    aborted = True
                    break
                continue
            consecutive_errors = 0

            if not verified:
                # Controle bij het eerste gelukte boek: heeft Boekwinkeltjes de waarde echt overgenomen?
                check = api_client.get_book(book_id)
                data = check.get("data", check) if isinstance(check, dict) else {}
                if isinstance(data, dict) and "shippingFormat" in data:
                    if data["shippingFormat"] != fmt:
                        lines.append(
                            f"GESTOPT: boek {book_id} kreeg verzendformaat {fmt} gestuurd, maar Boekwinkeltjes geeft "
                            f"{data['shippingFormat']!r} terug. Er is niets lokaal opgeslagen. Neem contact op met Boekwinkeltjes."
                        )
                        _log(conn, "push", "shipping_format", "error", f"controle mislukt bij boek {book_id}: verstuurd {fmt}, terug {data['shippingFormat']!r}")
                        conn.commit()
                        return lines
                    lines.append(f"Controle gelukt: boek {book_id} heeft bij Boekwinkeltjes nu verzendformaat {fmt} ({SHIPPING_FORMAT_LABELS[fmt]}).")
                else:
                    lines.append(
                        f"Let op: Boekwinkeltjes geeft het veld verzendformaat niet terug bij het uitlezen van boek {book_id}, "
                        f"dus de waarde kon niet worden gecontroleerd. Controleer dit boek zelf op de site."
                    )
                verified = True

            done += 1
            # Onze eigen kopie bijwerken, per boek in een eigen kleine transactie. Draait er tegelijk een sync, dan
            # kan die de rij vasthouden; we wachten dan hooguit 3 seconden (lock_timeout) en gaan door. Dat is niet erg:
            # Boekwinkeltjes is al bijgewerkt en de eerstvolgende sync haalt de waarde zelf binnen.
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE books SET shipping_format = %(f)s WHERE id = %(id)s", {"f": fmt, "id": book_id})
                conn.commit()
            except psycopg2.Error:
                conn.rollback()
                local_skipped += 1
            sleep_func(SHIPPING_FORMAT_BACKFILL_PAUSE)
        conn.commit()

        remaining = len(todo) - done - gone - len(errors)
        lines.append(f"Klaar: {done} {_n(done, 'boek', 'boeken')} bijgewerkt bij Boekwinkeltjes.")
        if gone:
            lines.append(f"{gone} {_n(gone, 'boek bestaat', 'boeken bestaan')} niet meer bij Boekwinkeltjes (overgeslagen).")
        if local_skipped:
            lines.append(
                f"{local_skipped} {_n(local_skipped, 'boek is', 'boeken zijn')} wel bij Boekwinkeltjes bijgewerkt, maar onze eigen kopie "
                f"kon even niet worden bijgewerkt (de tabel was in gebruik door een sync). De eerstvolgende sync haalt de waarde zelf binnen."
            )
        if errors:
            lines.append(f"{len(errors)} {_n(len(errors), 'fout', 'fouten')}; eerste: {errors[0]}")
        if aborted:
            lines.append(f"GESTOPT na {SHIPPING_FORMAT_BACKFILL_MAX_CONSECUTIVE_ERRORS} fouten achter elkaar. Er is niets verkeerd opgeslagen; los de fout op en draai opnieuw.")
        if stopped_for_time:
            lines.append(f"Tijdslimiet bereikt. Er zijn er nog {remaining} te gaan: start de job nog een keer, hij gaat verder waar hij ophield.")
        status = "error" if (errors or aborted) else "ok"
        _log(conn, "push", "shipping_format", status, " | ".join(lines[-4:]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "shipping_format", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return lines


def update_legacy_shipping_cost(real=False, briefpost=None, now_func=time.monotonic, sleep_func=time.sleep):
    """
    Eenmalige job: boeken die nog de oude briefpost-verzendkosten hebben (3,75, of de oude standaardwaarde 1,40;
    zie SHIPPING_LEGACY_BRIEFPOST_AMOUNTS) krijgen de huidige briefpost-kosten (standaard uit 'Hulp en instellingen',
    of het bedrag dat je meegeeft) en verzendformaat Brievenbuspakje, zowel bij Boekwinkeltjes als lokaal.
    De boekprijs blijft precies zoals hij is. Verzendkosten bij Bol (shipping_cost_bol) worden niet aangeraakt.

    Zonder real=True is het een proefrun: er wordt niets verstuurd of opgeslagen.
    Net als bij backfill_shipping_format: per boek eerst uitlezen (Boekwinkeltjes eist de prijs bij elke wijziging,
    die geven we terug zoals ze is), dan wijzigen, dan pas lokaal bewaren; bij het eerste boek wordt nagekeken of
    Boekwinkeltjes de waarden echt heeft overgenomen; opnieuw draaien is veilig (wat klaar is wordt overgeslagen).
    Boeken met een nog niet verstuurde lokale wijziging (pending_push) worden overgeslagen, zodat die later niet
    het oude bedrag terugzet; ze komen bij een volgende run aan bod.
    """
    lines = []
    conn = get_connection()
    try:
        settings_brief, _ = get_shipping_costs_setting(conn)
        target = settings_brief if briefpost is None else round(float(briefpost), 2)
        if not (0 < target < 1000):
            lines.append(f"Ongeldig bedrag voor briefpost: {target}. Er is niets gedaan.")
            return lines
        legacy = [a for a in SHIPPING_LEGACY_BRIEFPOST_AMOUNTS if round(a, 2) != target]
        lines.append(
            f"Oude briefpost-bedragen ({', '.join(f'€{a:.2f}' for a in legacy)}) worden €{target:.2f}, "
            f"met verzendformaat {SHIPPING_FORMAT_LABELS[1]}. De boekprijs blijft ongewijzigd."
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, shipping_cost, pending_push FROM books
                WHERE id > 0 AND pending_create = FALSE AND push_enabled = TRUE AND shipping_cost IS NOT NULL
                ORDER BY id
                """
            )
            rows = cur.fetchall()
        conn.rollback()  # alleen gelezen; geen transactie open laten staan

        candidates = [r for r in rows if round(float(r["shipping_cost"]), 2) in [round(a, 2) for a in legacy]]
        todo = [r["id"] for r in candidates if not r["pending_push"]]
        waiting = len(candidates) - len(todo)
        by_amount = {}
        for r in candidates:
            key = f"€{float(r['shipping_cost']):.2f}"
            by_amount[key] = by_amount.get(key, 0) + 1
        lines.append(f"{len(candidates)} {_n(len(candidates), 'boek', 'boeken')} met een oud bedrag gevonden:")
        for key, n in sorted(by_amount.items()):
            lines.append(f"  - {n} x {key}")
        if waiting:
            lines.append(
                f"  - {waiting} {_n(waiting, 'boek heeft', 'boeken hebben')} nog een niet-verstuurde wijziging en "
                f"worden nu overgeslagen; draai de job later nog eens."
            )

        if not real:
            lines.append("Proefrun: er is niets verstuurd of opgeslagen. Draai opnieuw met echt_uitvoeren = ja om het door te voeren.")
            return lines
        if not todo:
            lines.append("Niets te doen.")
            _log(conn, "push", "shipping_cost", "ok", "niets te doen")
            conn.commit()
            return lines

        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '3s'")
        conn.commit()
        started = now_func()
        done = gone = local_skipped = consecutive_errors = 0
        errors = []
        verified = stopped_for_time = aborted = False
        for book_id in todo:
            if now_func() - started > SHIPPING_FORMAT_BACKFILL_MAX_SECONDS:
                stopped_for_time = True
                break
            try:
                current = api_client.get_book(book_id)
                data = current.get("data", current) if isinstance(current, dict) else {}
                price = data.get("price") if isinstance(data, dict) else None
                if price is None:
                    raise api_client.BoekwinkeltjesAPIError(
                        f"GET boek {book_id}: Boekwinkeltjes gaf geen prijs terug; niets verstuurd"
                    )
                api_client.update_book(
                    book_id, {"price": price, "shippingCost": target, "shippingFormat": SHIPPING_FORMAT_MAILBOX}
                )
            except api_client.BoekwinkeltjesAPIError as e:
                if "-> 404:" in str(e):
                    gone += 1
                    continue
                errors.append(f"boek {book_id}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= SHIPPING_FORMAT_BACKFILL_MAX_CONSECUTIVE_ERRORS:
                    aborted = True
                    break
                continue
            consecutive_errors = 0

            if not verified:
                check = api_client.get_book(book_id)
                cdata = check.get("data", check) if isinstance(check, dict) else {}
                if isinstance(cdata, dict) and "shippingCost" in cdata:
                    try:
                        cost_ok = round(float(cdata["shippingCost"]), 2) == target
                    except (TypeError, ValueError):
                        cost_ok = False
                    price_ok = cdata.get("price") is None or round(float(cdata["price"]), 2) == round(float(price), 2)
                    if not (cost_ok and price_ok):
                        lines.append(
                            f"GESTOPT: boek {book_id} kreeg verzendkosten €{target:.2f} gestuurd, maar Boekwinkeltjes geeft "
                            f"verzendkosten {cdata.get('shippingCost')!r} en prijs {cdata.get('price')!r} terug (prijs was {price!r}). "
                            f"Er is niets lokaal opgeslagen."
                        )
                        _log(conn, "push", "shipping_cost", "error", f"controle mislukt bij boek {book_id}")
                        conn.commit()
                        return lines
                    lines.append(f"Controle gelukt: boek {book_id} heeft bij Boekwinkeltjes nu verzendkosten €{target:.2f}, de prijs is ongewijzigd.")
                else:
                    lines.append(
                        f"Let op: Boekwinkeltjes geeft de verzendkosten niet terug bij het uitlezen van boek {book_id}, dus de waarde "
                        f"kon niet worden gecontroleerd. Controleer dit boek zelf op de site."
                    )
                verified = True

            done += 1
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE books SET shipping_cost = %(c)s, shipping_format = %(f)s WHERE id = %(id)s AND pending_push = FALSE",
                        {"c": target, "f": SHIPPING_FORMAT_MAILBOX, "id": book_id},
                    )
                conn.commit()
            except psycopg2.Error:
                conn.rollback()
                local_skipped += 1
            sleep_func(SHIPPING_FORMAT_BACKFILL_PAUSE)
        conn.commit()

        remaining = len(todo) - done - gone - len(errors)
        lines.append(f"Klaar: {done} {_n(done, 'boek', 'boeken')} bijgewerkt bij Boekwinkeltjes (verzendkosten €{target:.2f}, {SHIPPING_FORMAT_LABELS[1]}).")
        if gone:
            lines.append(f"{gone} {_n(gone, 'boek bestaat', 'boeken bestaan')} niet meer bij Boekwinkeltjes (overgeslagen).")
        if local_skipped:
            lines.append(
                f"{local_skipped} {_n(local_skipped, 'boek is', 'boeken zijn')} wel bij Boekwinkeltjes bijgewerkt, maar onze eigen kopie "
                f"kon even niet worden bijgewerkt (tabel in gebruik). De eerstvolgende sync haalt de waarde zelf binnen."
            )
        if errors:
            lines.append(f"{len(errors)} {_n(len(errors), 'fout', 'fouten')}; eerste: {errors[0]}")
        if aborted:
            lines.append(f"GESTOPT na {SHIPPING_FORMAT_BACKFILL_MAX_CONSECUTIVE_ERRORS} fouten achter elkaar. Los de fout op en draai opnieuw.")
        if stopped_for_time:
            lines.append(f"Tijdslimiet bereikt. Er zijn er nog {remaining} te gaan: start de job nog een keer, hij gaat verder waar hij ophield.")
        _log(conn, "push", "shipping_cost", "error" if (errors or aborted) else "ok", " | ".join(lines[-4:]))
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "shipping_cost", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return lines


# Hoeveel bestellingen per sync-run we bij Bol mogen opvragen om een ontbrekende titel/prijs aan te vullen. Bol laat
# dat eindpunt niet vaak bevragen (8 per minuut volgens Bol's eigen documentatie); de rest komt bij de volgende run.
BOL_ORDER_DETAIL_MAX_PER_RUN = 5


def pull_bol_orders():
    """
    Haalt orders op bij Bol en slaat ze lokaal op in dezelfde 'orders'-tabel als
    Boekwinkeltjes (met platform='Bol'), zodat ze samen in één overzicht getoond
    kunnen worden. Elke orderregel (orderItem) wordt een eigen rij, met een
    negatief, van het Bol-orderItemId afgeleid id — zo kan het nooit botsen met
    een echt Boekwinkeltjes-order-id.

    Bol's lijst-eindpunt geeft alleen orderId/orderPlacedDateTime/ean/
    fulfilmentStatus terug — geen titel of prijs (dat zit alleen in het
    losse 'één order ophalen'-eindpunt, dat niet vaak bevraagd mag worden).
    Titel en prijs komen daarom uit ons eigen boek met dezelfde EAN. Staat dat boek niet (meer) in onze
    eigen tabel, of mist het een titel of prijs, dan vragen we die voor die bestelling eenmalig bij Bol op
    (zie get_order_item_details), en bewaren ze: een eenmaal bewaarde titel of prijs wordt nooit meer met
    niets overschreven, ook niet als het boek later uit onze tabel verdwijnt.
    """
    conn = get_connection()
    count = 0
    filled_from_bol = 0
    still_missing = 0
    detail_fetches = 0
    detail_cache = {}
    detail_blocked = False
    try:
        with conn.cursor() as cur:
            bol_orders = bol_client.get_orders(status="ALL")
            for order in bol_orders:
                order_id = order.get("orderId")
                order_date_raw = order.get("orderPlacedDateTime")
                order_date = None
                if order_date_raw:
                    try:
                        # Bol geeft een tijdzone mee (bijv. +02:00); die tijdzone-info
                        # laten we vallen (de kloktijd zelf is al de Nederlandse tijd),
                        # zodat dit tekstueel hetzelfde tijdzone-loze formaat heeft als
                        # Boekwinkeltjes' eigen datums — anders struikelt de gecombineerde
                        # datumconversie in het dashboard over de gemengde formaten.
                        order_date = dt.datetime.fromisoformat(order_date_raw).replace(tzinfo=None).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    except ValueError:
                        order_date = order_date_raw
                for item in order.get("orderItems") or []:
                    ean = item.get("ean")
                    if not ean:
                        continue  # regel zonder EAN (bijv. een administratieve regel) is niet bruikbaar
                    # Onderscheid tussen een DEFINITIEVE annulering en alleen een aanvraag daartoe
                    # door de klant (die jij nog moet accepteren of afwijzen):
                    #   CANCELLED               = bevestigd geannuleerd (Bol meldt fulfilmentStatus
                    #                             CANCELLED, of de hele hoeveelheid is geannuleerd)
                    #   CANCELLATION_REQUESTED  = klant vraagt om annulering, nog niet afgehandeld
                    fulfilment_status = item.get("fulfilmentStatus")
                    quantity = item.get("quantity") or 0
                    quantity_cancelled = item.get("quantityCancelled") or 0
                    fully_cancelled = fulfilment_status == "CANCELLED" or (
                        quantity > 0 and quantity_cancelled >= quantity
                    )
                    if fully_cancelled:
                        status = "CANCELLED"
                    elif item.get("cancellationRequest"):
                        status = "CANCELLATION_REQUESTED"
                    else:
                        status = fulfilment_status or "OPEN"
                    order_item_id = item.get("orderItemId") or f"{order_id}-{ean}"
                    # Python's ingebouwde hash() geeft per proces een ander resultaat (bewust
                    # gerandomiseerd) — dat is ongeschikt voor een stabiel id tussen sync-runs.
                    # md5 is wél deterministisch: dezelfde orderregel krijgt zo altijd hetzelfde id.
                    digest = hashlib.md5(f"bol-{order_item_id}".encode()).hexdigest()
                    local_id = -(int(digest[:8], 16) % (2**31))

                    book_id = None
                    title = None
                    price = None
                    if ean:
                        cur.execute(
                            "SELECT id, title, price FROM books WHERE TRIM(ean) = TRIM(%(ean)s) AND id > 0 LIMIT 1",
                            {"ean": ean},
                        )
                        match = cur.fetchone()
                        if match:
                            book_id = match["id"]
                            title = match["title"]
                            price = match["price"]

                    # Heeft deze bestelling al een titel en prijs, uit het eigen boek of uit een eerdere sync? Zo niet,
                    # dan eenmalig bij Bol opvragen (beperkt per run).
                    cur.execute("SELECT book_title, book_price FROM orders WHERE id = %(id)s", {"id": local_id})
                    existing = cur.fetchone()
                    have_title = bool(title) or bool(existing and existing["book_title"])
                    have_price = price is not None or bool(existing and existing["book_price"] is not None)
                    if not (have_title and have_price) and order_id:
                        if order_id in detail_cache:
                            details = detail_cache[order_id]
                        elif detail_blocked or detail_fetches >= BOL_ORDER_DETAIL_MAX_PER_RUN:
                            details = None
                            still_missing += 1
                        else:
                            detail_fetches += 1
                            try:
                                details = bol_client.get_order_item_details(order_id)
                            except Exception:
                                details = None
                                detail_blocked = True  # bijv. een 429 van Bol: deze run niet verder proberen
                            detail_cache[order_id] = details
                        found = (details or {}).get(order_item_id)
                        if found:
                            filled = False
                            if not title and not (existing and existing["book_title"]) and found.get("title"):
                                title = found["title"]
                                filled = True
                            if price is None and not (existing and existing["book_price"] is not None) and found.get("unit_price") is not None:
                                price = found["unit_price"]
                                filled = True
                            filled_from_bol += 1 if filled else 0

                    cur.execute(
                        """
                        INSERT INTO orders (
                            id, order_date, status, platform, book_id, book_title, book_price, book_ean
                        ) VALUES (%(id)s, %(order_date)s, %(status)s, 'Bol', %(book_id)s, %(title)s, %(price)s, %(ean)s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = excluded.status, book_id = excluded.book_id,
                            book_title = COALESCE(excluded.book_title, orders.book_title),
                            book_price = COALESCE(excluded.book_price, orders.book_price),
                            order_date = excluded.order_date
                        """,
                        {
                            "id": local_id,
                            "order_date": order_date,
                            "status": status,
                            "book_id": book_id,
                            "title": title,
                            "price": price,
                            "ean": ean,
                        },
                    )
                    count += 1
        conn.commit()
        detail = f"{count} {_n(count, 'order', 'orders')} verwerkt"
        if filled_from_bol:
            detail += f", {filled_from_bol} aangevuld met titel/prijs van Bol"
        if still_missing:
            detail += f", {still_missing} nog zonder titel/prijs (volgende sync)"
        if detail_blocked:
            detail += ", Bol-details tijdelijk niet beschikbaar"
        _log(conn, "pull", "orders", "ok", detail, platform="Bol")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "orders", "error", str(e), platform="Bol")
        conn.commit()
        raise
    return count


def full_sync():
    # Allereerst de opslagcontrole: is de database vol en dus alleen-lezen, dan falen alle stappen
    # hierna, en dan moet er toch een mail komen. Een fout hierin mag de sync nooit tegenhouden.
    try:
        check_storage_alerts()
    except Exception:
        pass
    init_db()
    n_books = pull_books()
    n_orders = pull_orders()
    try:
        pull_bol_orders()
    except Exception:
        pass  # al gelogd in pull_bol_orders(); de rest van de sync gaat gewoon door
    try:
        sync_stock_with_bol()
    except Exception:
        pass  # al gelogd in sync_stock_with_bol(); de rest van de sync gaat gewoon door
    # Let op: nieuwe boeken (pending_create) worden hier bewust NIET meer aangemaakt —
    # dat gebeurt druppelsgewijs via drip_push_new_books(), voor optimale zichtbaarheid
    # op Boekwinkeltjes' 'nieuw toegevoegd'-overzicht. Zie ook de noodgreep daarin voor
    # het einde van de avondperiode.
    n_pushed = push_pending_books()
    n_images_pushed = push_uploaded_images()
    try:
        push_new_books_to_bol()
    except Exception:
        pass  # al gelogd in push_new_books_to_bol(); de rest van de sync gaat gewoon door
    return {
        "books_pulled": n_books,
        "orders_pulled": n_orders,
        "books_pushed": n_pushed,
        "uploaded_images_pushed": n_images_pushed,
    }


def _create_one_book_at_boekwinkeltjes(cur, row):
    """
    Maakt één boek (met een tijdelijk negatief id) aan bij Boekwinkeltjes en
    vervangt het tijdelijke id door het echte, toegekende id. Geeft het nieuwe
    id terug. De aanroeper is verantwoordelijk voor het committen.
    """
    shipping_format = _format_for_row(row, get_shipping_costs_setting(cur.connection))
    payload = {
        "bookNumber": row["book_number"],
        "location": row["location"],
        "amount": row["amount"],
        "category1": row["category1"],
        "category2": row["category2"],
        "category3": row["category3"],
        "language": row["language"],
        "author": row["author"],
        "title": row["title"],
        "publisher": row["publisher"],
        "ean": row["ean"],
        "shortDescription": row["short_description"],
        "longDescription": row["long_description"],
        "price": float(row["price"]) if row["price"] is not None else None,
        "shippingCost": float(row["shipping_cost"]) if row["shipping_cost"] is not None else None,
        # Zonder verzendformaat kan een boek bij Boekwinkeltjes niet worden verkocht.
        "shippingFormat": shipping_format,
    }
    # Geen lege/None velden meesturen die de API mogelijk niet accepteert
    payload = {k: v for k, v in payload.items() if v is not None}

    result = api_client.create_book(payload)
    new_book = result.get("data", result) if isinstance(result, dict) else {}
    new_id = new_book.get("id")

    if new_id is None:
        raise api_client.BoekwinkeltjesAPIError(
            f"Aanmaken gelukt maar geen id teruggekregen voor tijdelijk boek {row['id']}: {result}"
        )

    old_id = row["id"]
    cur.execute(
        """
        UPDATE books
        SET id = %(new_id)s, pending_create = FALSE, pending_push = FALSE,
            weblink = %(weblink)s, last_synced_at = %(now)s,
            shipping_format = COALESCE(shipping_format, %(sf)s)
        WHERE id = %(old_id)s
        """,
        {
            "sf": shipping_format,
            "new_id": new_id,
            "weblink": new_book.get("weblink"),
            "now": _now(),
            "old_id": old_id,
        },
    )
    # Activiteitenlog-regels van het tijdelijke id mee laten verhuizen naar het echte id
    cur.execute(
        "UPDATE book_activity_log SET book_id = %(new_id)s WHERE book_id = %(old_id)s",
        {"new_id": new_id, "old_id": old_id},
    )
    # Geüploade-afbeeldingen-rijen ook mee laten verhuizen naar het echte id
    cur.execute(
        "UPDATE book_uploaded_images SET book_id = %(new_id)s WHERE book_id = %(old_id)s",
        {"new_id": new_id, "old_id": old_id},
    )
    return new_id


def push_new_books():
    """
    Maakt in ÉÉN KEER alle nog niet aangemaakte boeken (pending_create = TRUE) aan
    bij Boekwinkeltjes. Wordt niet meer automatisch bij elke gewone sync aangeroepen
    — zie drip_push_new_books() daarvoor, die boeken één voor één en alleen tijdens
    drukke bezoekperioden naar buiten brengt. Deze functie blijft bestaan als
    noodgreep: aan het einde van de avondperiode worden eventueel nog openstaande
    boeken in één keer alsnog aangemaakt.
    """
    conn = get_connection()
    count = 0
    errors = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM books WHERE pending_create = TRUE AND push_enabled = TRUE")
            rows = cur.fetchall()
            for row in rows:
                try:
                    _create_one_book_at_boekwinkeltjes(cur, row)
                    count += 1
                    # Per boek opslaan: is een boek eenmaal bij Boekwinkeltjes aangemaakt, dan moet het nieuwe id
                    # ook echt bewaard zijn. Een latere fout mag dat niet terugdraaien, anders wordt het boek bij
                    # de volgende poging een tweede keer aangemaakt (dubbel aanbod).
                    conn.commit()
                except api_client.BoekwinkeltjesAPIError as e:
                    errors.append(f"boek {row['id']} ({row['title']}): {e}")
        conn.commit()
        detail = f"{count} {_n(count, 'nieuw boek', 'nieuwe boeken')} aangemaakt bij Boekwinkeltjes"
        if errors:
            detail += f" — {len(errors)} {_n(len(errors), 'boek', 'boeken')} overgeslagen: " + "; ".join(errors[:5])
        _log(conn, "push", "books_new", "ok" if not errors else "error", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "books_new", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return count


# ---------- Druppelsgewijs nieuwe boeken naar Boekwinkeltjes ----------
# Boekwinkeltjes toont op de startpagina de meest recent toegevoegde boeken, wat
# tot verkopen leidt. Alle nieuwe boeken tegelijk aanmaken verspilt die
# zichtbaarheid grotendeels. Deze functie brengt ze daarom één voor één naar
# buiten, alleen tijdens drukke bezoekperioden, met een instelbaar interval. De
# instellingen staan in app_settings, en zijn aan te passen op de pagina
# 'Hulp en instellingen'. Geldt uitsluitend voor Boekwinkeltjes — Bol kent dit
# 'nieuw toegevoegd'-mechanisme niet en blijft altijd direct synchroniseren.
#
# De drukke perioden zijn per dag van de week in te stellen (het weekend is
# vaak anders dan doordeweeks), en elk blokje mag leeg blijven. Dat schema
# staat als JSON onder 'bw_drip_schedule'. De oude, losse instellingen
# (bw_drip_lunch_start enz., voor elke dag hetzelfde) gelden alleen nog als
# terugval zolang er nog geen schema is opgeslagen.

DRIP_SETTING_DEFAULTS = {
    "bw_drip_interval_minutes": "8",
    "bw_drip_lunch_start": "11:00",
    "bw_drip_lunch_end": "14:00",
    "bw_drip_endwork_start": "16:15",
    "bw_drip_endwork_end": "16:45",
    "bw_drip_evening_start": "19:00",
    "bw_drip_evening_end": "22:00",
}


def _get_setting(conn, key, default=None):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %(key)s", {"key": key})
        row = cur.fetchone()
    return row["value"] if row and row["value"] is not None else default


def _set_setting(conn, key, value):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (%(key)s, %(value)s)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            {"key": key, "value": str(value)},
        )
    conn.commit()


def _parse_hhmm(text):
    hours, minutes = text.split(":")
    return dt.time(int(hours), int(minutes))


DRIP_DAY_KEYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]  # positie = datetime.weekday()
DRIP_WINDOW_KEYS = ["lunch", "endwork", "evening"]


def _drip_schedule_raw(conn):
    """
    Het druppelschema als {dag: {periode: [van, tot] of None}}. Staat als JSON
    onder 'bw_drip_schedule' (bewaard vanuit het dashboard). Is dat er nog niet,
    of is het onleesbaar, dan wordt het afgeleid van de oude losse instellingen
    — dezelfde tijden voor elke dag — zodat alles blijft werken zoals het was.
    """
    raw = _get_setting(conn, "bw_drip_schedule")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    legacy = {}
    for window in DRIP_WINDOW_KEYS:
        legacy[window] = [
            _get_setting(conn, f"bw_drip_{window}_start", DRIP_SETTING_DEFAULTS[f"bw_drip_{window}_start"]),
            _get_setting(conn, f"bw_drip_{window}_end", DRIP_SETTING_DEFAULTS[f"bw_drip_{window}_end"]),
        ]
    return {day: {window: list(pair) for window, pair in legacy.items()} for day in DRIP_DAY_KEYS}


def _drip_windows_for_day(conn, weekday):
    """
    De (start, eind)-perioden (als dt.time) waarin het op deze weekdag
    (0 = maandag) mag druppelen. Een leeg, onvolledig of ongeldig blokje telt
    niet mee; een dag zonder enig blokje geeft een lege lijst (dan gebeurt er
    die dag niets).
    """
    day = _drip_schedule_raw(conn).get(DRIP_DAY_KEYS[weekday])
    if not isinstance(day, dict):
        return []
    windows = []
    for window in DRIP_WINDOW_KEYS:
        pair = day.get(window)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not pair[0] or not pair[1]:
            continue
        try:
            start, end = _parse_hhmm(pair[0]), _parse_hhmm(pair[1])
        except (ValueError, AttributeError, TypeError):
            continue
        if start < end:
            windows.append((start, end))
    return windows


def drip_push_new_books():
    """
    Druppelt nieuwe, via de app aangemaakte boeken één voor één naar Boekwinkeltjes
    — alleen tijdens de voor VANDAAG (dag van de week) ingestelde drukke
    bezoekperioden, met het ingestelde interval ertussen. Buiten die perioden
    gebeurt er niets, BEHALVE: is het laatste blokje van vandaag net voorbij en
    staan er nog boeken te wachten, dan gaan die alsnog in één keer de deur uit
    (zodat een boek nooit een hele dag blijft liggen). Dat gebeurt maximaal één
    keer per dag, ook als de wachtrij op dat moment leeg was — een boek dat pas
    daarna binnenkomt wacht gewoon op het eerstvolgende blokje. Een dag zonder
    enig ingevuld blokje: dan gebeurt er die dag niets. Bedoeld om vaak te
    draaien (bijv. elke 5 minuten).
    """
    conn = get_connection()
    try:
        # De ingestelde tijden (lunchpauze, enz.) zijn Nederlandse tijd — dus ook
        # 'nu' als Nederlandse tijd bepalen, niet als de tijd van de server zelf
        # (die bij GitHub Actions altijd UTC is, en anders 1-2 uur zou verschillen).
        now = dt.datetime.now(tz=ZoneInfo("Europe/Amsterdam"))
        now_time = now.time()
        today_str = now.date().isoformat()

        windows_today = _drip_windows_for_day(conn, now.weekday())
        if not windows_today:
            return  # vandaag mag er niet gedruppeld worden

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM books WHERE pending_create = TRUE AND push_enabled = TRUE")
            pending_count = cur.fetchone()["n"]

        if now_time > max(end for _, end in windows_today):
            # Noodgreep: na het laatste blokje van vandaag blijft er nooit iets
            # onnodig liggen. Maximaal één keer per dag — en dat ook vastleggen als
            # er niets te doen was, zodat een boek dat 's avonds laat of 's nachts
            # binnenkomt op het eerstvolgende blokje wacht in plaats van meteen weg te gaan.
            if _get_setting(conn, "bw_drip_last_flush_date") == today_str:
                return
            if pending_count > 0:
                push_new_books()  # opent/sluit zijn eigen verbinding en logt zelf
            _set_setting(conn, "bw_drip_last_flush_date", today_str)
            return

        if pending_count == 0:
            return  # lege wachtrij: niets te doen, en geen log nodig om de sync-runs niet vol te proppen

        if not any(start <= now_time <= end for start, end in windows_today):
            return  # buiten alle blokjes van vandaag: wachten tot het volgende

        interval_minutes = int(
            _get_setting(conn, "bw_drip_interval_minutes", DRIP_SETTING_DEFAULTS["bw_drip_interval_minutes"])
        )
        last_pushed_s = _get_setting(conn, "bw_drip_last_pushed_at")
        if last_pushed_s:
            last_pushed = dt.datetime.fromisoformat(last_pushed_s)
            if last_pushed.tzinfo is None:
                # Afkomstig van vóór deze tijdzone-reparatie (toen was 'nu' nog de
                # naïeve servertijd, in de praktijk altijd UTC) — als zodanig behandelen.
                last_pushed = last_pushed.replace(tzinfo=dt.timezone.utc)
            if (now - last_pushed).total_seconds() < interval_minutes * 60:
                return  # nog te vroeg voor het volgende boek in de rij

        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM books WHERE pending_create = TRUE AND push_enabled = TRUE ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return

        try:
            with conn.cursor() as cur:
                new_id = _create_one_book_at_boekwinkeltjes(cur, row)
            conn.commit()
            _set_setting(conn, "bw_drip_last_pushed_at", now.isoformat())
            _log(
                conn, "push", "books_new_drip", "ok",
                f"1 nieuw boek druppelsgewijs aangemaakt bij Boekwinkeltjes (tijdelijk id {row['id']} -> {new_id})",
            )
            conn.commit()
        except api_client.BoekwinkeltjesAPIError as e:
            # Dit specifieke boek mislukt (bijv. een ongeldige prijs) — de tijd toch
            # bijwerken, anders wordt precies dit boek elke paar minuten opnieuw
            # geprobeerd in plaats van keurig het ingestelde interval aan te houden.
            # Corrigeer het boek (via Boekdetails) om het bij de volgende beurt
            # alsnog te laten lukken.
            conn.rollback()
            _set_setting(conn, "bw_drip_last_pushed_at", now.isoformat())
            _log(
                conn, "push", "books_new_drip", "error",
                f"Boek {row['id']} ({row['title']}) overgeslagen: {e}",
            )
            conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "books_new_drip", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()


def push_uploaded_images():
    """
    Stuurt lokaal geüploade, nog niet gepushte afbeeldingen naar Boekwinkeltjes —
    alleen voor boeken die al een echt (positief) id hebben en waarvoor push is
    toegestaan (push_enabled = TRUE).
    """
    conn = get_connection()
    pushed = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.book_id, u.image_data, u.content_type
                FROM book_uploaded_images u
                JOIN books b ON b.id = u.book_id
                WHERE u.pushed_to_boekwinkeltjes = FALSE
                  AND b.id > 0
                  AND b.push_enabled = TRUE
                  AND COALESCE(b.amount, 0) > 0
                ORDER BY u.book_id, u.position
                """
            )
            rows = cur.fetchall()

        errors = []
        for row in rows:
            try:
                api_client.upload_book_image(row["book_id"], bytes(row["image_data"]), row["content_type"])
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE book_uploaded_images SET pushed_to_boekwinkeltjes = TRUE WHERE id = %(id)s",
                        {"id": row["id"]},
                    )
                conn.commit()
                pushed += 1
            except api_client.BoekwinkeltjesAPIError as e:
                # Geen automatische uitschakeling hier: een 404 op dit eindpunt betekent niet
                # betrouwbaar 'boek bestaat niet meer' (zie de kanttekening bij amount > 0
                # hierboven) — dat zou een nog bestaand boek onterecht helemaal kunnen
                # uitschakelen. Wordt gewoon bij de eerstvolgende sync opnieuw geprobeerd.
                errors.append(f"afbeelding {row['id']} (boek {row['book_id']}): {e}")

        detail = f"{pushed} geüploade {_n(pushed, 'afbeelding', 'afbeeldingen')} gepusht"
        if errors:
            detail += f" — {len(errors)} overgeslagen: " + "; ".join(errors[:5])
        _log(conn, "push", "uploaded_images", "ok" if not errors else "error", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "uploaded_images", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return pushed


def pull_uploaded_book_images():
    """
    Voor boeken met gepushte maar nog niet bevestigde eigen afbeeldingen: checkt of
    Boekwinkeltjes ze al heeft verwerkt (de upload gaat via een achtergrondwachtrij
    aan hun kant). Zodra dat zo is: downloadt de large- en medium-variant en slaat
    die lokaal op (in plaats van alleen de URL te bewaren), en verwijdert de eigen
    geüploade originelen — die zijn dan niet meer nodig.
    """
    conn = get_connection()
    confirmed = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT book_id FROM book_uploaded_images WHERE pushed_to_boekwinkeltjes = TRUE"
            )
            book_ids = [row["book_id"] for row in cur.fetchall()]

        for book_id in book_ids:
            images = api_client.get_book_images(book_id)
            if not images:
                # Nog niet verwerkt door Boekwinkeltjes — volgende keer opnieuw proberen
                continue

            with conn.cursor() as cur:
                for position, img in enumerate(images):
                    large_bytes = None
                    medium_bytes = None
                    if img.get("urlLarge"):
                        r = requests.get(img["urlLarge"], timeout=30)
                        if r.ok:
                            large_bytes = r.content
                    if img.get("urlMedium"):
                        r = requests.get(img["urlMedium"], timeout=30)
                        if r.ok:
                            medium_bytes = r.content

                    cur.execute(
                        """
                        INSERT INTO book_images (
                            book_id, image_id, position, url_large, url_medium, url_small,
                            image_large_data, image_medium_data, last_synced_at
                        ) VALUES (%(book_id)s, %(image_id)s, %(position)s, %(url_large)s, %(url_medium)s, %(url_small)s,
                            %(large_data)s, %(medium_data)s, %(now)s)
                        ON CONFLICT (book_id, image_id) DO UPDATE SET
                            position=excluded.position, url_large=excluded.url_large,
                            url_medium=excluded.url_medium, url_small=excluded.url_small,
                            image_large_data=excluded.image_large_data, image_medium_data=excluded.image_medium_data,
                            last_synced_at=excluded.last_synced_at
                        """,
                        {
                            "book_id": book_id,
                            "image_id": img.get("id"),
                            "position": position,
                            "url_large": img.get("urlLarge"),
                            "url_medium": img.get("urlMedium"),
                            "url_small": img.get("urlSmall"),
                            "large_data": psycopg2.Binary(large_bytes) if large_bytes else None,
                            "medium_data": psycopg2.Binary(medium_bytes) if medium_bytes else None,
                            "now": _now(),
                        },
                    )
                # Eigen geüploade originelen weg — we hebben nu de bevestigde versie
                cur.execute("DELETE FROM book_uploaded_images WHERE book_id = %(id)s", {"id": book_id})
            conn.commit()
            confirmed += 1

        _log(conn, "pull", "uploaded_images", "ok", f"{confirmed} {_n(confirmed, 'boek', 'boeken')} bevestigd en lokaal opgeslagen")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "uploaded_images", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return confirmed

def pull_images(limit=200):
    """
    Haalt afbeeldingen op voor een beperkt aantal boeken die nog geen
    afbeeldingen in de database hebben staan. Dit is bewust een aparte,
    kleine batch per keer (i.p.v. onderdeel van de gewone sync), omdat de
    API geen bulk-endpoint voor afbeeldingen heeft: het is één aanroep per
    boek, wat bij 3000+ boeken de gewone sync veel te lang zou maken.
    Draai dit los, vaker/minder vaak, tot alle boeken een keer gecontroleerd zijn.
    """
    conn = get_connection()
    books_checked = 0
    images_found = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id FROM books b
                LEFT JOIN book_images bi ON bi.book_id = b.id
                WHERE bi.book_id IS NULL AND b.id > 0
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            book_ids = [row["id"] for row in cur.fetchall()]

        errors = 0
        for book_id in book_ids:
            try:
                images = api_client.get_book_images(book_id)
            except api_client.BoekwinkeltjesAPIError:
                errors += 1
                continue
            with conn.cursor() as cur:
                if images:
                    for position, img in enumerate(images):
                        cur.execute(
                            """
                            INSERT INTO book_images (
                                book_id, image_id, position, url_large, url_medium, url_small, last_synced_at
                            ) VALUES (%(book_id)s, %(image_id)s, %(position)s, %(url_large)s, %(url_medium)s, %(url_small)s, %(now)s)
                            ON CONFLICT (book_id, image_id) DO UPDATE SET
                                position=excluded.position, url_large=excluded.url_large,
                                url_medium=excluded.url_medium, url_small=excluded.url_small,
                                last_synced_at=excluded.last_synced_at
                            """,
                            {
                                "book_id": book_id,
                                "image_id": img.get("id"),
                                "position": position,
                                "url_large": img.get("urlLarge"),
                                "url_medium": img.get("urlMedium"),
                                "url_small": img.get("urlSmall"),
                                "now": _now(),
                            },
                        )
                    images_found += len(images)
                else:
                    # Sentinel-rij: dit boek is gecontroleerd maar heeft geen afbeeldingen,
                    # anders proberen we hem bij elke run opnieuw.
                    cur.execute(
                        """
                        INSERT INTO book_images (book_id, image_id, position, last_synced_at)
                        VALUES (%(book_id)s, -1, 0, %(now)s)
                        ON CONFLICT (book_id, image_id) DO NOTHING
                        """,
                        {"book_id": book_id, "now": _now()},
                    )
            books_checked += 1

        conn.commit()
        detail = f"{books_checked} {_n(books_checked, 'boek', 'boeken')} gecontroleerd, {images_found} {_n(images_found, 'afbeelding', 'afbeeldingen')} opgeslagen"
        if errors:
            detail += f", {errors} {_n(errors, 'boek', 'boeken')} overgeslagen door een fout"
        _log(conn, "pull", "images", "ok" if not errors else "error", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "images", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return {"books_checked": books_checked, "images_found": images_found}


# ---------- Hoofdfoto via og:image van de publieke boekpagina ----------

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\'])',
    re.IGNORECASE,
)


def _extract_og_image(html):
    m = _OG_IMAGE_RE.search(html)
    if not m:
        return None
    return m.group(1) or m.group(2)


def pull_main_images(limit=200):
    """
    Haalt voor een batch boeken (zonder al bekende hoofdfoto) de publieke
    boekpagina op en leest de og:image-metatag uit — dat is de foto die
    Boekwinkeltjes zelf als hoofdfoto van het boek beschouwt. De officiële
    API geeft dit namelijk niet terug via /books of /books/{id}/images.

    Net als pull_images() is dit een aparte, geleidelijke batchtaak i.p.v.
    onderdeel van de gewone sync, om dezelfde reden: één extra HTTP-request
    per boek is bij 3000+ boeken te veel voor de gewone sync-cyclus.
    """
    conn = get_connection()
    checked = 0
    found = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, weblink FROM books
                WHERE main_image_url IS NULL AND weblink IS NOT NULL
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            rows = cur.fetchall()

        for row in rows:
            book_id = row["id"]
            weblink = row["weblink"]
            main_url = None
            try:
                resp = requests.get(
                    weblink, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.ok:
                    main_url = _extract_og_image(resp.text)
            except requests.RequestException:
                main_url = None

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE books SET main_image_url = %(url)s WHERE id = %(id)s",
                    # lege string i.p.v. NULL = "gecontroleerd, niets gevonden",
                    # anders blijft dit boek bij elke run opnieuw geprobeerd worden
                    {"url": main_url or "", "id": book_id},
                )
            checked += 1
            if main_url:
                found += 1

        conn.commit()
        checked_word = _n(checked, "boek", "boeken")
        found_word = _n(found, "hoofdfoto", "hoofdfoto's")
        _log(
            conn,
            "pull",
            "main_images",
            "ok",
            f"{checked} {checked_word} gecontroleerd, {found} {found_word} gevonden",
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "pull", "main_images", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return {"books_checked": checked, "main_images_found": found}


# ---------- Controle: staan de foto's er nog zoals eerder, en is de eerste foto nog dezelfde? (alleen lezen) ----------

MAIN_IMAGE_CHECK_WORKERS = 3
MAIN_IMAGE_CHECK_MAX_SECONDS = 30 * 60
MAIN_IMAGE_CHECK_CSV = "hoofdafbeelding_controle.csv"

MAIN_IMAGE_STATUS_SAME = "gelijk"
MAIN_IMAGE_STATUS_ORDER = "andere eerste foto"
MAIN_IMAGE_STATUS_OTHER = "andere foto's"
MAIN_IMAGE_STATUS_GONE = "foto's weg"
MAIN_IMAGE_STATUS_NO_RECORD = "geen eerdere gegevens"
MAIN_IMAGE_STATUS_NO_IMAGES = "geen foto's (ook niet eerder)"
MAIN_IMAGE_STATUS_UNREADABLE = "niet opgehaald"


def _image_key(url):
    """Vergelijkbare vorm van een afbeeldingslink: zonder http/https, zonder ?-deel, in kleine letters."""
    if not url:
        return ""
    u = re.sub(r"^https?:", "", url.strip().lower())
    return re.split(r"[?#]", u, maxsplit=1)[0]


def classify_book_images(stored, current):
    """
    Vergelijkt de foto's die we eerder van Boekwinkeltjes hebben opgeslagen ('stored': dicts met image_id, position,
    url_large, op volgorde) met wat Boekwinkeltjes nu teruggeeft ('current': dicts met id, urlLarge, in de volgorde van
    de API). We nemen aan dat de eerste foto in die lijst de hoofdfoto is, zoals de rest van de app ook doet.
    Geeft (status, nummer van de huidige eerste foto in onze oude volgorde of None).
    """
    stored = [s for s in stored if s.get("image_id") not in (None, -1)]
    if not stored and not current:
        return MAIN_IMAGE_STATUS_NO_IMAGES, None
    if not stored:
        return MAIN_IMAGE_STATUS_NO_RECORD, None
    if not current:
        return MAIN_IMAGE_STATUS_GONE, None

    stored_ids = [s["image_id"] for s in stored]
    current_ids = [c.get("id") for c in current]
    stored_keys = [_image_key(s.get("url_large")) for s in stored]
    current_keys = [_image_key(c.get("urlLarge")) for c in current]

    def first_position():
        for i, (sid, skey) in enumerate(zip(stored_ids, stored_keys)):
            if sid == current_ids[0] or (skey and skey == current_keys[0]):
                return i + 1
        return None

    if set(stored_ids) == set(current_ids):
        same = stored_ids[0] == current_ids[0]
    elif stored_keys and all(stored_keys) and set(stored_keys) == set(current_keys):
        same = stored_keys[0] == current_keys[0]
    else:
        return MAIN_IMAGE_STATUS_OTHER, first_position()
    return (MAIN_IMAGE_STATUS_SAME if same else MAIN_IMAGE_STATUS_ORDER), first_position()


def compare_main_images(output_path=MAIN_IMAGE_CHECK_CSV, limit=None, workers=MAIN_IMAGE_CHECK_WORKERS,
                        max_seconds=MAIN_IMAGE_CHECK_MAX_SECONDS, now_func=time.monotonic):
    """
    ALLEEN LEZEN. Vraagt per boek bij Boekwinkeltjes de foto's op en zet ze naast wat wij eerder hebben opgeslagen
    (book_images). Schrijft een CSV-bestand met het resultaat. Er wordt niets naar Boekwinkeltjes gestuurd en niets
    in de database veranderd. Geeft regels tekst terug voor het logboek.
    """
    from concurrent.futures import ThreadPoolExecutor

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, location FROM books WHERE id > 0 AND COALESCE(amount, 0) > 0 ORDER BY id")
            books = list(cur.fetchall())
            cur.execute(
                "SELECT book_id, image_id, position, url_large FROM book_images ORDER BY book_id, position, image_id"
            )
            stored_rows = list(cur.fetchall())
    finally:
        conn.close()

    stored_by_book = {}
    for row in stored_rows:
        stored_by_book.setdefault(row["book_id"], []).append(row)
    if limit:
        books = books[: int(limit)]

    started = now_func()
    results = []
    stopped_early = False

    def work(book):
        if now_func() - started > max_seconds:
            return book, "stop"
        try:
            return book, api_client.get_book_images(book["id"])
        except api_client.BoekwinkeltjesAPIError:
            return book, None

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for book, current in pool.map(work, books):
            if isinstance(current, str):
                stopped_early = True
                continue
            stored = stored_by_book.get(book["id"], [])
            if current is None:
                results.append((book, MAIN_IMAGE_STATUS_UNREADABLE, None, stored, []))
                continue
            status, nr = classify_book_images(stored, current)
            results.append((book, status, nr, stored, current))

    order = [MAIN_IMAGE_STATUS_GONE, MAIN_IMAGE_STATUS_OTHER, MAIN_IMAGE_STATUS_ORDER, MAIN_IMAGE_STATUS_UNREADABLE,
             MAIN_IMAGE_STATUS_NO_RECORD, MAIN_IMAGE_STATUS_NO_IMAGES, MAIN_IMAGE_STATUS_SAME]
    results.sort(key=lambda r: (order.index(r[1]), r[0]["id"]))
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["boek_id", "titel", "locatie", "status", "eerdere_eerste_foto", "huidige_eerste_foto",
                         "eerdere_aantal_fotos", "huidige_aantal_fotos", "huidige_eerste_was_eerder_foto_nr"])
        for book, status, nr, stored, current in results:
            real = [s for s in stored if s.get("image_id") not in (None, -1)]
            writer.writerow([book["id"], book["title"], book["location"] or "", status,
                             (real[0].get("url_large") or "") if real else "",
                             (current[0].get("urlLarge") or "") if current else "",
                             len(real), len(current), nr or ""])

    counts = {}
    for _, status, *_rest in results:
        counts[status] = counts.get(status, 0) + 1
    lines = [f"{len(results)} van {len(books)} boeken gecontroleerd (alleen lezen, er is niets gewijzigd)."]
    for status in order:
        lines.append(f"  {status}: {counts.get(status, 0)}")
    if stopped_early:
        lines.append(f"LET OP: gestopt door de tijdsgrens; {len(books) - len(results)} boeken zijn niet gecontroleerd. Draai opnieuw.")
    lines.append(f"Resultaat in {output_path}. Boeken waar iets is veranderd staan bovenaan.")
    return lines


# ---------- Hoofdfoto van een paar boeken bekijken (alleen lezen) ----------

def book_page_urls(book_id, title):
    """De publieke boekpagina op Boekwinkeltjes: /b/<boeknummer>/<titel-met-streepjes>/, plus de variant zonder titel."""
    urls = []
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")
    if slug:
        urls.append(f"https://www.boekwinkeltjes.nl/b/{book_id}/{slug}/")
    urls.append(f"https://www.boekwinkeltjes.nl/b/{book_id}/")
    return urls


def show_main_image(book_ids):
    """
    ALLEEN LEZEN. Toont per opgegeven boek: de foto's volgens de API (in volgorde), wat wij eerder opsloegen, en welke foto
    de publieke boekpagina nu als hoofdfoto (og:image) laat zien. Zo is te zien hoe de hoofdfoto zich verhoudt tot de volgorde.
    """
    lines = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for book_id in book_ids:
                cur.execute("SELECT id, title, main_image_url FROM books WHERE id = %(id)s", {"id": book_id})
                rows = cur.fetchall()
                title = rows[0]["title"] if rows else None
                cur.execute(
                    "SELECT image_id, position, url_large FROM book_images WHERE book_id = %(id)s AND image_id != -1 "
                    "ORDER BY position, image_id",
                    {"id": book_id},
                )
                stored = list(cur.fetchall())
                lines.append(f"Boek {book_id}: {title or '(niet in de database)'}")
                try:
                    current = api_client.get_book_images(book_id)
                except api_client.BoekwinkeltjesAPIError as e:
                    current = None
                    lines.append(f"  API: fout bij ophalen van de foto's: {e}")
                if current is not None:
                    lines.append(f"  API geeft {len(current)} foto's:")
                    for i, c in enumerate(current, start=1):
                        lines.append(f"    {i}. id {c.get('id')}  {c.get('urlLarge')}")
                lines.append(f"  Eerder bij ons opgeslagen: {len(stored)} foto's; eerste: "
                             f"{stored[0]['image_id'] if stored else '-'}")
                og, tried = None, []
                for url in book_page_urls(book_id, title):
                    ok, found = _fetch_page_main_image(url)
                    tried.append(f"{url} -> {'gelezen' if ok else 'niet gelezen'}")
                    if ok:
                        og = found
                        break
                lines.append("  Pagina's geprobeerd: " + "; ".join(tried))
                if og:
                    nr = None
                    for i, c in enumerate(current or [], start=1):
                        if _image_key(og) in (_image_key(c.get("urlLarge")), _image_key(c.get("urlMedium")), _image_key(c.get("urlSmall"))):
                            nr = i
                            break
                    lines.append(f"  Hoofdfoto op de pagina (og:image): {og}")
                    lines.append(f"  Dat is foto {nr} in de API-lijst." if nr else "  Die link staat niet in de API-lijst (andere maat of andere naam).")
                else:
                    lines.append("  Geen hoofdfoto (og:image) gevonden op de pagina.")
    finally:
        conn.close()
    return lines


def _fetch_page_main_image(url):
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
    except requests.RequestException:
        return False, None
    if not resp.ok:
        return False, None
    return True, (_extract_og_image(resp.text) or None)


# ---------- Verkennen van het hoofdfoto-formulier op de website (alleen lezen) ----------

_FORM_LINK_WORDS = re.compile(r"bewerk|afbeeld|foto|image|edit|wijzig", re.IGNORECASE)
_FORM_LINK_FORBIDDEN = re.compile(r"verwijder|delete|remove|uitlog|logout|afmeld|bestel|koop|betaal", re.IGNORECASE)


class _PageStructure(HTMLParser):
    """Leest uit een HTML-pagina de links, de formulieren (met velden en knoppen) en de knoppen buiten formulieren."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []   # (href, tekst)
        self.forms = []   # {'method', 'action', 'fields': [(tag, type, name, value)]}
        self._form = None
        self._link = None
        self._button = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._link = [a["href"], ""]
        elif tag == "form":
            self._form = {"method": (a.get("method") or "get").lower(), "action": a.get("action") or "", "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "select", "textarea") and self._form is not None:
            self._form["fields"].append((tag, a.get("type") or "", a.get("name") or "", a.get("value") or ""))
        elif tag == "button" and self._form is not None:
            self._button = [a.get("type") or "", a.get("name") or "", a.get("value") or "", ""]

    def handle_data(self, data):
        if self._link is not None:
            self._link[1] += data
        if self._button is not None:
            self._button[3] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            self.links.append((self._link[0], " ".join(self._link[1].split())))
            self._link = None
        elif tag == "form":
            self._form = None
        elif tag == "button" and self._button is not None and self._form is not None:
            self._form["fields"].append(("button", self._button[0], self._button[1], " ".join(self._button[3].split()) or self._button[2]))
            self._button = None


def _short(value, limit=12):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + "…"


def describe_page_structure(html, base_url):
    """
    Geeft regels terug die de structuur van een pagina laten zien: links die over foto's/bewerken gaan, en elk formulier
    met zijn velden. Waarden van verborgen velden worden ingekort (er kunnen sleutels in zitten) en wachtwoordvelden
    worden nooit getoond. Geeft ook de plekken waar 'hoofdafbeelding' in de tekst staat.
    """
    parser = _PageStructure()
    parser.feed(html)
    lines = []
    seen = set()
    relevant = []
    for href, text in parser.links:
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        if _FORM_LINK_FORBIDDEN.search(href) or _FORM_LINK_FORBIDDEN.search(text):
            continue
        if _FORM_LINK_WORDS.search(href) or _FORM_LINK_WORDS.search(text):
            relevant.append((absolute, text))
    lines.append(f"  Links over foto's of bewerken: {len(relevant)}")
    for absolute, text in relevant[:40]:
        lines.append(f"    {absolute}   [{text[:60]}]")
    lines.append(f"  Formulieren op de pagina: {len(parser.forms)}")
    for i, form in enumerate(parser.forms, start=1):
        lines.append(f"    Formulier {i}: {form['method'].upper()} {urljoin(base_url, form['action'])}")
        for tag, ftype, name, value in form["fields"]:
            if ftype.lower() == "password":
                lines.append(f"      {tag} type=password name={name} (waarde niet getoond)")
            elif ftype.lower() == "hidden":
                lines.append(f"      {tag} type=hidden name={name} waarde={_short(value, 8)}")
            else:
                lines.append(f"      {tag} type={ftype or '-'} name={name or '-'} waarde={_short(value, 40)}")
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = " ".join(plain.split())
    hits = [m.start() for m in re.finditer(r"hoofdafbeelding", plain, re.IGNORECASE)]
    lines.append(f"  'hoofdafbeelding' komt {len(hits)}x voor in de tekst")
    for pos in hits[:8]:
        lines.append(f"    ...{plain[max(pos - 80, 0):pos + 80]}...")
    return lines, [a for a, _ in relevant]


def boekwinkeltjes_website_login():
    """Ingelogde sessie op de WEBSITE van Boekwinkeltjes (niet de API). (session, None) of (None, foutmelding)."""
    username = os.environ.get("BOEKWINKELTJES_USERNAME")
    password = os.environ.get("BOEKWINKELTJES_PASSWORD")
    if not username or not password:
        return None, "BOEKWINKELTJES_USERNAME en/of BOEKWINKELTJES_PASSWORD ontbreken (zet ze als GitHub-secret)."
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        session.get("https://www.boekwinkeltjes.nl/login/", timeout=15)
        session.post(
            "https://www.boekwinkeltjes.nl/login/",
            data={"boekwinkeltje": username, "wachtwoord": password, "persistentCookie": "yes",
                  "form": "login", "submit": "Inloggen"},
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return None, f"Kon niet inloggen bij Boekwinkeltjes: {type(e).__name__}"
    if "secureID" not in session.cookies.get_dict():
        return None, "Inloggen bij Boekwinkeltjes is mislukt (verkeerde gebruikersnaam of wachtwoord?)."
    return session, None


def explore_main_image_form(book_id):
    """
    ALLEEN LEZEN (behalve het inloggen). Logt in op de website, opent de boekpagina en laat zien welke links en
    formulieren er zijn om foto's te beheren, zodat te zien is wat de knop 'Instellen als hoofdafbeelding' verstuurt.
    Er wordt niets aangeklikt of verstuurd en nergens op 'verwijderen' gedrukt: links met 'verwijder', 'delete' enz.
    worden overgeslagen.
    """
    lines = []
    session, error = boekwinkeltjes_website_login()
    if error:
        return [error]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM books WHERE id = %(id)s", {"id": int(book_id)})
            rows = cur.fetchall()
    finally:
        conn.close()
    title = rows[0]["title"] if rows else None
    lines.append(f"Boek {book_id}: {title or '(niet in de database)'}")
    candidates = []
    for url in book_page_urls(book_id, title):
        try:
            resp = session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            lines.append(f"{url} -> niet gelezen ({type(e).__name__})")
            continue
        lines.append(f"{url} -> HTTP {resp.status_code}")
        if not resp.ok:
            continue
        page_lines, links = describe_page_structure(resp.text, resp.url)
        lines.extend(page_lines)
        candidates = links
        break
    followed = 0
    for url in candidates:
        if followed >= 4 or "boekwinkeltjes.nl" not in url:
            continue
        if not re.search(r"afbeeld|foto|image|bewerk|edit", url, re.IGNORECASE):
            continue
        followed += 1
        lines.append("")
        lines.append(f"== Gevolgd: {url}")
        try:
            resp = session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            lines.append(f"  niet gelezen ({type(e).__name__})")
            continue
        lines.append(f"  HTTP {resp.status_code}, uiteindelijk {resp.url}")
        if resp.ok:
            page_lines, _ = describe_page_structure(resp.text, resp.url)
            lines.extend(page_lines)
    if not candidates:
        lines.append("Geen links gevonden om te volgen.")
    return lines


# ---------- Fotostofzuiger: afbeeldingsbestanden opruimen ----------

# Afbeeldingen die langer dan dit aantal uren geleden zijn vastgelegd, komen in aanmerking.
PHOTO_VACUUM_MIN_AGE_HOURS = 48
# Per tabel pas 'VACUUM FULL' draaien als er minstens zoveel is vrijgekomen. Pas dat geeft de schijfruimte
# echt terug: zonder blijft de grootte die Supabase meldt gelijk (de ruimte wordt dan alleen hergebruikt).
PHOTO_VACUUM_FULL_THRESHOLD_BYTES = 1 * 1024 * 1024
# Voor book_uploaded_images, waar we zelf niets wissen maar waar na verwerkte uploads veel lege ruimte
# kan achterblijven: pas teruggeven als er minstens zoveel leeg staat.
PHOTO_VACUUM_EMPTY_SPACE_THRESHOLD_BYTES = 10 * 1024 * 1024


def _mb(num_bytes):
    return f"{(num_bytes or 0) / (1024 * 1024):.1f}".replace(".", ",") + " MB"


def _kb(num_bytes):
    return f"{(num_bytes or 0) / 1024:.0f} kB"


def _plan_photo_cleanup(rows, cutoff):
    """
    Bepaalt van welke afbeeldingen het bestand gewist mag worden. 'rows' zijn de rijen van
    book_images (zonder de bestanden zelf) van boeken die minstens één bewaard bestand hebben;
    'cutoff' is het moment waarvóór iets "oud genoeg" is.

    Per boek blijft de voorkant altijd staan. Dat is hetzelfde plaatje als in Boekdetails (met de
    ster): het plaatje waar books.main_image_url naar wijst. Staat die als hele afbeelding (tekst) in de
    boekenrij, dan is het de foto waarvan het bestand er precies mee overeenkomt ('star_match').
    Anders de eerste foto. Van de rest wordt het bestand alleen gewist als: het ouder is dan 'cutoff',
    én de rij nog een link naar Boekwinkeltjes heeft (dan is het plaatje daar te zien en gaat er
    niets verloren). Geeft (op_te_ruimen, telling) terug.
    """
    by_book = {}
    for row in rows:
        by_book.setdefault(row["book_id"], []).append(row)

    to_clean = []
    stats = {"books": len(by_book), "front_kept": 0, "too_young": 0, "no_link": 0, "no_timestamp": 0}
    for book_id, images in by_book.items():
        images.sort(key=lambda r: (r["position"], r["image_id"]))
        main_url = images[0].get("main_image_url")
        front_id = None
        if main_url:
            for img in images:
                if main_url in (img["url_large"], img["url_medium"], img["url_small"]):
                    front_id = img["image_id"]
                    break
        if front_id is None:
            for img in images:
                if img.get("star_match"):
                    front_id = img["image_id"]
                    break
        if front_id is None:
            front_id = images[0]["image_id"]

        for img in images:
            large = img.get("large_bytes") or 0
            medium = img.get("medium_bytes") or 0
            if not (large or medium):
                continue  # geen bewaard bestand, dus niets op te ruimen
            if img["image_id"] == front_id:
                stats["front_kept"] += 1
                continue
            stamp = img.get("last_synced_at")
            if stamp is None:
                stats["no_timestamp"] += 1
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            if stamp >= cutoff:
                stats["too_young"] += 1
                continue
            if not (img["url_large"] or img["url_medium"] or img["url_small"]):
                stats["no_link"] += 1
                continue
            to_clean.append(
                {"book_id": book_id, "image_id": img["image_id"], "large_bytes": large, "medium_bytes": medium}
            )
    return to_clean, stats


def _plan_main_url_fixes(rows):
    """
    Sommige boeken hebben als voorkant (books.main_image_url) de hele afbeelding als tekst (een data-URI).
    Dat kwam doordat de ster in Boekdetails vroeger de getoonde afbeelding opsloeg, en dat is honderden kB
    per boek in de boekenrij zelf. Voor elk zo'n boek waarvan we het bestand van die foto nog hebben (de rij
    waarvan het bestand er precies mee overeenkomt: 'star_match') bestaat ook de echte link naar
    Boekwinkeltjes. Die komt dan in de plaats, en de voorkant blijft dezelfde foto. Boeken zonder
    overeenkomende foto laten we met rust. Geeft een lijst {'book_id', 'url', 'bytes'}.
    """
    by_book = {}
    for row in rows:
        by_book.setdefault(row["book_id"], []).append(row)
    fixes = []
    for book_id, images in by_book.items():
        if not any(img.get("data_uri_main") for img in images):
            continue
        images.sort(key=lambda r: (r["position"], r["image_id"]))
        for img in images:
            if not img.get("star_match"):
                continue
            url = img["url_large"] or img["url_medium"] or img["url_small"]
            if url:
                fixes.append({"book_id": book_id, "url": url, "bytes": img.get("data_uri_len") or 0})
                break
    return fixes


def _safe_rows(conn, sql, params=None):
    """Voert een rapportage-query uit. Bij een fout (bijvoorbeeld ontbrekende rechten) krijg je None
    terug, zodat het rapport niet de hele opruimtaak laat mislukken."""
    try:
        with conn.cursor() as cur:
            # Zonder parameters de tekst ongemoeid laten: een letterlijk %-teken (LIKE 'data:%')
            # zou anders als parameter-teken worden gezien.
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return cur.fetchall()
    except Exception:
        conn.rollback()
        return None


# Een herschrijving (VACUUM FULL) heeft tijdelijk ongeveer zoveel extra ruimte nodig als de tabel zelf.
# Hij gebeurt alleen als de database daarna nog ruim onder de limiet blijft: anders kan de herschrijving de
# database juist alleen-lezen maken.
VACUUM_SAFETY_FRACTION = 0.95
# Een herschrijving heeft de tabel heel even voor zichzelf nodig. Houdt een andere sessie de tabel vast, dan
# wacht hij kort (zodat anderen er niet lang achter in de rij staan), wacht een tijdje en probeert het opnieuw.
VACUUM_LOCK_ATTEMPTS = 5
VACUUM_LOCK_TIMEOUT = "8s"
VACUUM_LOCK_PAUSE_SECONDS = 20


def _is_lock_timeout(error):
    return getattr(error, "pgcode", None) == "55P03" or "lock timeout" in str(error).lower()


def _describe_lock_holders(table):
    """Wie houdt de tabel vast? Een korte tekst voor in de foutmelding (of 'onbekend' bij ontbrekende rechten)."""
    conn = get_connection()
    try:
        rows = _safe_rows(
            conn,
            "SELECT DISTINCT ON (a.pid) a.pid, a.state, a.application_name, "
            "to_char(now() - a.xact_start, 'HH24:MI:SS') AS open_for, left(a.query, 80) AS query "
            "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            f"WHERE l.relation = '{table}'::regclass AND l.granted AND l.pid <> pg_backend_pid() "
            "ORDER BY a.pid",  # 'table' komt uit een vaste lijst
        )
    finally:
        conn.close()
    if rows is None:
        return "onbekend (geen rechten om dat te zien)"
    if not rows:
        return "niemand meer (het was van voorbijgaande aard)"
    parts = []
    for r in rows[:3]:
        what = f"sessie {r['pid']} ({r['state'] or 'onbekend'}"
        if r.get("application_name"):
            what += f", {r['application_name']}"
        what += ")"
        if r.get("open_for"):
            what += f", transactie al {r['open_for']} open"
        if r.get("query"):
            what += f", laatste opdracht: {str(r['query']).strip()}"
        parts.append(what)
    return "; ".join(parts)


def _vacuum_full_tables(tables):
    """
    Herschrijft de opgegeven tabellen (VACUUM FULL), elk op een eigen verbinding, en geeft zo de lege ruimte
    terug aan de database. Er wordt niets gewist of gewijzigd. De namen komen uit een vaste lijst van de
    aanroeper. Een tabel wordt overgeslagen als de database na de herschrijving te dicht tegen de limiet
    zou komen, of als de grootte niet te meten is. Houdt een andere sessie de tabel vast, dan wordt het
    een paar keer opnieuw geprobeerd; blijft dat mislukken, dan staat in de foutmelding wie hem vasthoudt.
    Geeft (herschreven, overgeslagen, fouten) terug, met bij 'overgeslagen' paren (tabel, reden) en bij
    'fouten' teksten 'tabel: fout'.
    """
    done, skipped, errors = [], [], []
    if not tables:
        return done, skipped, errors
    conn = get_connection()
    try:
        for table in tables:
            try:
                used = _database_size_bytes(conn)
                rows = _safe_rows(conn, f"SELECT pg_total_relation_size('{table}') AS bytes")
            except Exception as e:
                errors.append(f"{table}: {e}")
                continue
            conn.commit()  # de meting is klaar: geen transactie laten openstaan terwijl er herschreven wordt
            if not rows:
                skipped.append((table, "de grootte is niet te meten"))
                continue
            table_bytes = rows[0]["bytes"]
            if used + table_bytes > VACUUM_SAFETY_FRACTION * STORAGE_LIMIT_MB * 1024 * 1024:
                skipped.append((table, f"de database zit op {_mb(used)} en de herschrijving heeft tijdelijk {_mb(table_bytes)} extra nodig"))
                continue
            for attempt in range(1, VACUUM_LOCK_ATTEMPTS + 1):
                try:
                    vacuum_conn = get_connection()
                    try:
                        vacuum_conn.autocommit = True  # VACUUM kan niet binnen een transactie
                        with vacuum_conn.cursor() as cur:
                            cur.execute(f"SET lock_timeout = '{VACUUM_LOCK_TIMEOUT}'")
                            cur.execute(f"VACUUM FULL {table}")
                        done.append(table)
                    finally:
                        vacuum_conn.close()
                    break
                except Exception as e:
                    if _is_lock_timeout(e) and attempt < VACUUM_LOCK_ATTEMPTS:
                        time.sleep(VACUUM_LOCK_PAUSE_SECONDS)  # niet in de rij blijven staan: even weg, dan opnieuw
                        continue
                    message = f"{table}: {str(e).strip()}"
                    if _is_lock_timeout(e):
                        message += f" (na {attempt} pogingen). De tabel werd vastgehouden door: {_describe_lock_holders(table)}"
                    errors.append(message)
                    break
    finally:
        conn.close()
    return done, skipped, errors


def photo_vacuum(real=False, min_age_hours=PHOTO_VACUUM_MIN_AGE_HOURS):
    """
    De fotostofzuiger: houdt de database klein door
      1. van oude foto's die niet de voorkant zijn het bestand te wissen (niet de rij, niet de link),
      2. voorkanten die als hele afbeelding (tekst) in de boekenrij staan om te zetten naar een gewone
         link, en
      3. de vrijgekomen en de leegstaande ruimte aan de database terug te geven (VACUUM FULL).

    Zonder real=True is het een proefrun: er wordt niets gewijzigd, alleen getoond wat er zou gebeuren,
    samen met een rapport over waar de ruimte zit. Geeft regels tekst terug.

    Bewust NIET aangeraakt:
      - book_uploaded_images: foto's die nog niet (bevestigd) bij Boekwinkeltjes staan. Voor een boek
        in de wachtrij zijn dat de enige exemplaren. (Alleen de lege ruimte in die tabel wordt teruggegeven.)
      - de rijen in book_images zelf (met de links): pull_images haalt foto's alleen op voor boeken
        zonder enige rij, dus weggooien van rijen zou een nieuwe ronde ophalen veroorzaken.
    """
    lines = []

    def say(text=""):
        lines.append(text)

    say(f"== Fotostofzuiger: {'ECHT OPRUIMEN' if real else 'PROEFRUN (er wordt niets verwijderd)'} ==")
    say(
        f"Regel: per boek blijft de voorkant staan. Van de overige afbeeldingen wordt het bestand gewist als het "
        f"langer dan {min_age_hours} uur geleden is vastgelegd. De regel met de link naar Boekwinkeltjes blijft, "
        f"dus de app toont zo'n foto daarna via die link."
    )
    say("Niet aangeraakt: foto's die nog niet naar Boekwinkeltjes zijn gestuurd of nog niet bevestigd zijn.")

    conn = get_connection()
    try:
        # ---------- 1. Waar zit de ruimte? ----------
        say()
        say("== 1. Waar zit de ruimte? ==")
        sizes = _safe_rows(
            conn,
            "SELECT pg_database_size(current_database()) AS this_db, "
            "(SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database) AS all_dbs",
        )
        size_before = sizes[0]["all_dbs"] if sizes else None
        if sizes:
            say(
                f"Database, zoals Supabase het telt (alle databases): {_mb(sizes[0]['all_dbs'])}"
                f"  |  alleen deze database: {_mb(sizes[0]['this_db'])}"
            )
            say("De limiet van het gratis abonnement is 500 MB database; daarboven wordt de database alleen-lezen.")
        else:
            say("Databasegrootte: niet op te vragen")
        wal = _safe_rows(conn, "SELECT COALESCE(sum(size), 0) AS bytes FROM pg_ls_waldir()")
        say(
            f"Schrijflogboek (WAL, telt mee voor het schijfgebruik): {_mb(wal[0]['bytes'])}"
            if wal else "Schrijflogboek (WAL): niet op te vragen"
        )
        top = _safe_rows(
            conn,
            "SELECT n.nspname AS schema, c.relname AS name, pg_total_relation_size(c.oid) AS bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'm') ORDER BY bytes DESC LIMIT 12",
        )
        if top:
            say("Grootste onderdelen (inclusief indexen):")
            for t in top:
                say(f"   {t['schema']}.{t['name']}: {_mb(t['bytes'])}")
        books_count = _safe_rows(conn, "SELECT count(*) AS n FROM books")
        if books_count and top:
            books_size = next((t["bytes"] for t in top if t["schema"] == "public" and t["name"] == "books"), None)
            if books_size and books_count[0]["n"]:
                say(
                    f"Tabel books: {books_count[0]['n']} boeken, gemiddeld {_kb(books_size / books_count[0]['n'])} per boek "
                    f"(inclusief indexen)"
                )
        log_info = _safe_rows(conn, "SELECT count(*) AS n, min(run_at) AS oldest FROM sync_log")
        if log_info:
            say(f"Synchronisatielogboek: {log_info[0]['n']} regels, oudste van {str(log_info[0]['oldest'])[:10]}")

        # ---------- 2. De afbeeldingen ----------
        say()
        say("== 2. De afbeeldingen ==")
        stat = _safe_rows(
            conn,
            "SELECT count(*) FILTER (WHERE image_id != -1) AS images, "
            "count(*) FILTER (WHERE image_large_data IS NOT NULL OR image_medium_data IS NOT NULL) AS with_files, "
            "COALESCE(sum(octet_length(image_large_data)), 0) AS large_bytes, "
            "COALESCE(sum(octet_length(image_medium_data)), 0) AS medium_bytes FROM book_images",
        )
        if stat:
            st_row = stat[0]
            n_files = st_row["with_files"] or 0
            say(
                f"book_images: {st_row['images']} afbeeldingen, waarvan {n_files} met een bewaard bestand "
                f"(groot {_mb(st_row['large_bytes'])}, middel {_mb(st_row['medium_bytes'])}"
                + (f"; gemiddeld {_kb((st_row['large_bytes'] + st_row['medium_bytes']) / n_files)} per afbeelding)" if n_files else ")")
            )
        uploaded = _safe_rows(
            conn,
            "SELECT u.pushed_to_boekwinkeltjes AS pushed, "
            "CASE WHEN b.id IS NULL THEN 'boek bestaat niet meer' "
            "WHEN b.id < 0 THEN 'boek nog niet bij Boekwinkeltjes aangemaakt' "
            "WHEN COALESCE(b.queued, FALSE) THEN 'boek staat in de wachtrij' "
            "WHEN COALESCE(b.push_enabled, TRUE) = FALSE THEN 'synchronisatie staat uit' "
            "WHEN COALESCE(b.amount, 0) <= 0 THEN 'aantal is 0' "
            "ELSE 'wacht op verwerking' END AS reason, "
            "count(*) AS n, COALESCE(sum(octet_length(u.image_data)), 0) AS bytes, min(u.created_at) AS oldest "
            "FROM book_uploaded_images u LEFT JOIN books b ON b.id = u.book_id "
            "GROUP BY 1, 2 ORDER BY bytes DESC",
        )
        empty_space = 0
        if uploaded is not None:
            if uploaded:
                say("book_uploaded_images (eigen uploads, niet aangeraakt):")
                for u in uploaded:
                    say(
                        f"   {'gepusht' if u['pushed'] else 'nog niet gepusht'}, {u['reason']}: {u['n']} foto's, "
                        f"{_mb(u['bytes'])}, oudste van {str(u['oldest'])[:10]}"
                    )
            else:
                say("book_uploaded_images: leeg")
            table_size = _safe_rows(conn, "SELECT pg_total_relation_size('book_uploaded_images') AS bytes")
            if table_size:
                live = sum(u["bytes"] for u in uploaded)
                empty_space = max(table_size[0]["bytes"] - live, 0)
                say(
                    f"   Tabelgrootte {_mb(table_size[0]['bytes'])}, echte inhoud {_mb(live)}: {_mb(empty_space)} staat leeg "
                    f"(dat wordt hergebruikt voor nieuwe uploads)"
                )
        data_uri = _safe_rows(
            conn, "SELECT count(*) AS n, COALESCE(sum(length(main_image_url)), 0) AS bytes FROM books WHERE main_image_url LIKE 'data:%'"
        )
        if data_uri:
            say(
                f"Boeken waarvan de voorkant als hele afbeelding (tekst) in de boekenrij zelf staat: {data_uri[0]['n']}"
                f" ({_mb(data_uri[0]['bytes'])})"
            )

        # ---------- 3. Wat de stofzuiger doet ----------
        say()
        say("== 3. Wat de stofzuiger doet ==")
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=min_age_hours)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bi.book_id, bi.image_id, bi.position, bi.url_large, bi.url_medium, bi.url_small,
                       bi.last_synced_at,
                       COALESCE(octet_length(bi.image_large_data), 0) AS large_bytes,
                       COALESCE(octet_length(bi.image_medium_data), 0) AS medium_bytes,
                       CASE WHEN b.main_image_url LIKE 'data:%' THEN NULL ELSE b.main_image_url END AS main_image_url,
                       COALESCE(b.main_image_url LIKE 'data:%', FALSE) AS data_uri_main,
                       CASE WHEN b.main_image_url LIKE 'data:%' THEN length(b.main_image_url) END AS data_uri_len,
                       CASE WHEN b.main_image_url LIKE 'data:%'
                                 AND (bi.image_large_data IS NOT NULL OR bi.image_medium_data IS NOT NULL)
                            THEN COALESCE(
                                   b.main_image_url = ('data:image/jpeg;base64,'
                                       || replace(encode(bi.image_large_data, 'base64'), chr(10), ''))
                                   OR b.main_image_url = ('data:image/jpeg;base64,'
                                       || replace(encode(bi.image_medium_data, 'base64'), chr(10), '')),
                                   FALSE)
                            ELSE FALSE END AS star_match
                FROM book_images bi
                JOIN books b ON b.id = bi.book_id
                WHERE bi.image_id != -1
                  AND bi.book_id IN (
                      SELECT book_id FROM book_images
                      WHERE image_large_data IS NOT NULL OR image_medium_data IS NOT NULL
                  )
                ORDER BY bi.book_id, bi.position, bi.image_id
                """
            )
            rows = cur.fetchall()
        to_clean, plan = _plan_photo_cleanup(rows, cutoff)
        fixes = _plan_main_url_fixes(rows)
        large_total = sum(i["large_bytes"] for i in to_clean)
        medium_total = sum(i["medium_bytes"] for i in to_clean)
        freed_planned = large_total + medium_total
        fix_bytes_planned = sum(f["bytes"] for f in fixes)
        say(f"Boeken met bewaarde bestanden: {plan['books']}")
        say(f"Voorkanten die blijven staan: {plan['front_kept']}")
        say(f"Nog te jong (minder dan {min_age_hours} uur): {plan['too_young']}")
        if plan["no_link"]:
            say(f"Overgeslagen omdat er geen link naar Boekwinkeltjes is (enige exemplaar): {plan['no_link']}")
        if plan["no_timestamp"]:
            say(f"Overgeslagen omdat het vastlegmoment onbekend is: {plan['no_timestamp']}")
        say(
            f"{'Op te ruimen' if real else 'Zou worden opgeruimd'}: {len(to_clean)} afbeeldingen = {_mb(freed_planned)} "
            f"(groot {_mb(large_total)}, middel {_mb(medium_total)})"
        )
        for item in sorted(to_clean, key=lambda i: -(i["large_bytes"] + i["medium_bytes"]))[:8]:
            say(f"   boek {item['book_id']}, afbeelding {item['image_id']}: {_kb(item['large_bytes'] + item['medium_bytes'])}")
        if data_uri and data_uri[0]["n"]:
            say(
                f"Voorkanten als tekst in de boekenrij om te zetten naar een gewone link: "
                f"{len(fixes)} van de {data_uri[0]['n']} ({_mb(fix_bytes_planned)}); de overige "
                f"{max(data_uri[0]['n'] - len(fixes), 0)} hebben geen bijbehorende foto meer en blijven zoals ze zijn."
            )
        elif fixes:
            say(f"Voorkanten om te zetten naar een gewone link: {len(fixes)} ({_mb(fix_bytes_planned)})")

        # ---------- 4. Opruimen ----------
        migrated = 0
        migrated_bytes = 0
        cleaned = 0
        freed_actual = 0
        if real and fixes:
            for index, fix in enumerate(fixes, start=1):
                with conn.cursor() as cur:
                    # Alleen als de voorkant nog steeds een hele afbeelding (tekst) is: is hij tussentijds
                    # aangepast, dan blijft die keuze staan.
                    cur.execute(
                        "UPDATE books SET main_image_url = %(url)s WHERE id = %(book_id)s AND LEFT(main_image_url, 5) = 'data:'",
                        {"url": fix["url"], "book_id": fix["book_id"]},
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        migrated += 1
                        migrated_bytes += fix["bytes"]
                if index % 50 == 0:
                    conn.commit()
            conn.commit()

        if real and to_clean:
            per_book = {}
            for item in to_clean:
                per_book.setdefault(item["book_id"], []).append(item["image_id"])
            for index, (book_id, image_ids) in enumerate(per_book.items(), start=1):
                with conn.cursor() as cur:
                    # De voorwaarden staan hier nog eens, voor het geval er tussen kiezen en wissen iets is veranderd.
                    cur.execute(
                        """
                        UPDATE book_images
                        SET image_large_data = NULL, image_medium_data = NULL
                        WHERE book_id = %(book_id)s AND image_id = ANY(%(image_ids)s)
                          AND last_synced_at < %(cutoff)s
                          AND (image_large_data IS NOT NULL OR image_medium_data IS NOT NULL)
                          AND (COALESCE(url_large, '') <> '' OR COALESCE(url_medium, '') <> '' OR COALESCE(url_small, '') <> '')
                        """,
                        {"book_id": book_id, "image_ids": image_ids, "cutoff": cutoff},
                    )
                    cleaned += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if index % 50 == 0:
                    conn.commit()
            conn.commit()
            # Raakte het wissen minder rijen dan gepland (er was tussen kiezen en wissen iets veranderd),
            # dan rekenen we met het evenredige deel.
            freed_actual = freed_planned * (cleaned / len(to_clean))

        # Welke tabellen krijgen hun ruimte teruggegeven? Pas dat maakt de gemelde grootte kleiner.
        def _vacuum_plan(images_bytes, books_bytes):
            tables = []
            if images_bytes >= PHOTO_VACUUM_FULL_THRESHOLD_BYTES:
                tables.append("book_images")
            if books_bytes >= PHOTO_VACUUM_FULL_THRESHOLD_BYTES:
                tables.append("books")
            if empty_space >= PHOTO_VACUUM_EMPTY_SPACE_THRESHOLD_BYTES:
                tables.append("book_uploaded_images")
            return tables

        vacuumed = []
        vacuum_skipped = []
        vacuum_errors = []
        size_after = None
        if real:
            # Zelfde reden als bij reclaim_space: de rapportage hierboven heeft tabellen gelezen in een transactie die
            # nog openstaat (bijvoorbeeld als er niets te wissen viel en dus nergens is gecommit). Eerst afsluiten,
            # anders houdt deze verbinding de tabellen vast die hieronder worden herschreven.
            conn.commit()
            vacuumed, vacuum_skipped, vacuum_errors = _vacuum_full_tables(_vacuum_plan(freed_actual, migrated_bytes))
            after = _safe_rows(conn, "SELECT COALESCE(sum(pg_database_size(datname)), 0) AS all_dbs FROM pg_database")
            size_after = after[0]["all_dbs"] if after else None

        say()
        if real:
            say(f"Opgeruimd: {cleaned} afbeeldingen ({_mb(freed_actual)} aan bestanden gewist).")
            if cleaned != len(to_clean):
                say(f"LET OP: {len(to_clean)} gepland, maar {cleaned} gewist; de rest was intussen veranderd en is met rust gelaten.")
            if fixes:
                say(f"Voorkanten omgezet naar een gewone link: {migrated} van de {len(fixes)} ({_mb(migrated_bytes)}).")
            if vacuumed:
                say("Ruimte teruggegeven aan de database (VACUUM FULL): " + ", ".join(vacuumed) + ".")
            elif not vacuum_errors and not vacuum_skipped:
                say("Er was te weinig om aan de database terug te geven; de ruimte wordt hergebruikt.")
            for table, reason in vacuum_skipped:
                say(f"Overgeslagen (niet veilig): {table}, want {reason}.")
            for error in vacuum_errors:
                say(f"LET OP: VACUUM FULL mislukte voor {error}; de ruimte is vrij maar de gemelde grootte daalt nog niet.")
            if size_before is not None and size_after is not None:
                say(f"Database: {_mb(size_before)} -> {_mb(size_after)}")
        else:
            would = _vacuum_plan(freed_planned, fix_bytes_planned)
            if would:
                say("Bij echt opruimen wordt daarna de ruimte teruggegeven (VACUUM FULL) voor: " + ", ".join(would) + ".")
            say("Proefrun: er is niets gewijzigd. Draai opnieuw met echt_verwijderen = ja om werkelijk op te ruimen.")

        if real:
            parts = [f"{cleaned} {_n(cleaned, 'afbeelding', 'afbeeldingen')} opgeruimd ({_mb(freed_actual)})"]
            if cleaned != len(to_clean):
                parts[0] += f" ({len(to_clean)} gepland, de rest was intussen veranderd)"
            if fixes:
                parts.append(f"{migrated} {_n(migrated, 'voorkant', 'voorkanten')} omgezet naar een gewone link ({_mb(migrated_bytes)})")
            parts.append(f"{plan['front_kept']} voorkanten bewaard")
            if vacuumed:
                parts.append("ruimte teruggegeven: " + ", ".join(vacuumed))
            if vacuum_skipped:
                parts.append("overgeslagen omdat het niet veilig was: " + ", ".join(t for t, _ in vacuum_skipped))
            detail = ", ".join(parts)
            if size_before is not None and size_after is not None:
                detail += f"; database {_mb(size_before)} -> {_mb(size_after)}"
            if vacuum_errors:
                detail += " — VACUUM FULL mislukte: " + "; ".join(vacuum_errors)
        else:
            detail = (
                f"proefrun: {len(to_clean)} {_n(len(to_clean), 'afbeelding', 'afbeeldingen')} ({_mb(freed_planned)}) "
                f"zouden worden opgeruimd, {len(fixes)} {_n(len(fixes), 'voorkant', 'voorkanten')} omgezet ({_mb(fix_bytes_planned)}), "
                f"{plan['front_kept']} voorkanten blijven"
            )
        _log(conn, "cleanup", "images", "error" if vacuum_errors else "ok", detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "cleanup", "images", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return lines


# ---------- Ruimte terugwinnen: alleen lege ruimte teruggeven, niets wissen ----------

def reclaim_space(real=True):
    """
    Geeft lege ruimte in de database terug aan Supabase, zonder iets te wissen of te wijzigen: geen foto,
    geen boek, geen enkel gegeven. Het gaat om book_uploaded_images: daar worden eigen uploads tijdelijk
    bewaard en na verwerking weer verwijderd. Wat vrijkomt blijft als lege ruimte in de tabel staan, en
    telt zo mee voor de limiet, tot de tabel wordt herschreven (VACUUM FULL). Dat gebeurt pas als er
    minstens 10 MB leegstaat, en alleen als de database daarna nog ruim onder de limiet blijft.

    Met real=False alleen een rapport. Geeft regels tekst terug.
    """
    lines = []

    def say(text=""):
        lines.append(text)

    say(f"== Ruimte terugwinnen: {'UITVOEREN' if real else 'ALLEEN RAPPORT'} (verwijdert geen gegevens) ==")
    say("Geeft lege ruimte in de database terug. Er wordt geen foto, boek of ander gegeven gewist of gewijzigd.")

    conn = get_connection()
    try:
        sizes = _safe_rows(
            conn,
            "SELECT pg_database_size(current_database()) AS this_db, "
            "(SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database) AS all_dbs",
        )
        size_before = sizes[0]["all_dbs"] if sizes else None
        say(
            f"Database, zoals Supabase het telt: {_mb(sizes[0]['all_dbs'])} van {STORAGE_LIMIT_MB} MB"
            if sizes else "Databasegrootte: niet op te vragen"
        )
        top = _safe_rows(
            conn,
            "SELECT n.nspname AS schema, c.relname AS name, pg_total_relation_size(c.oid) AS bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'm') ORDER BY bytes DESC LIMIT 5",
        )
        if top:
            say("Grootste onderdelen:")
            for t in top:
                say(f"   {t['schema']}.{t['name']}: {_mb(t['bytes'])}")

        total = _safe_rows(conn, "SELECT pg_total_relation_size('book_uploaded_images') AS bytes")
        live = _safe_rows(conn, "SELECT COALESCE(sum(octet_length(image_data)), 0) AS bytes FROM book_uploaded_images")
        measured = bool(total) and live is not None and bool(live)
        empty = 0
        if measured:
            empty = max(total[0]["bytes"] - live[0]["bytes"], 0)
            say(
                f"book_uploaded_images: tabelgrootte {_mb(total[0]['bytes'])}, echte inhoud {_mb(live[0]['bytes'])}, "
                f"leeg {_mb(empty)}"
            )
        else:
            say("LET OP: de lege ruimte in book_uploaded_images is niet te bepalen.")
        will = measured and empty >= PHOTO_VACUUM_EMPTY_SPACE_THRESHOLD_BYTES
        if measured and not will:
            say(f"Minder dan {_mb(PHOTO_VACUUM_EMPTY_SPACE_THRESHOLD_BYTES)} leeg: niets te doen.")

        vacuumed, skipped, errors = [], [], []
        size_after = None
        say()
        if will and not real:
            say(f"Bij uitvoeren wordt book_uploaded_images herschreven; dat geeft ongeveer {_mb(empty)} terug.")
        if will and real:
            # De metingen hierboven hebben een transactie geopend waarin book_uploaded_images is gelezen. Zolang die
            # openstaat, houdt deze verbinding de tabel vast en kan de herschrijving (op een andere verbinding) er niet
            # bij: dan wacht de taak op zichzelf. Daarom die transactie eerst afsluiten.
            conn.commit()
            vacuumed, skipped, errors = _vacuum_full_tables(["book_uploaded_images"])
            after = _safe_rows(conn, "SELECT COALESCE(sum(pg_database_size(datname)), 0) AS bytes FROM pg_database")
            size_after = after[0]["bytes"] if after else None
            if vacuumed:
                say(f"Ruimte teruggegeven: book_uploaded_images ({_mb(empty)} was leeg).")
            for table, reason in skipped:
                say(f"Overgeslagen (niet veilig): {table}, want {reason}.")
            for error in errors:
                say(f"LET OP: herschrijven mislukte voor {error}.")
            if size_before is not None and size_after is not None:
                say(f"Database: {_mb(size_before)} -> {_mb(size_after)}")

        if not measured:
            detail, status = "lege ruimte niet te bepalen", "error"
        elif not will:
            detail, status = f"niets te doen: {_mb(empty)} leeg in book_uploaded_images (onder {_mb(PHOTO_VACUUM_EMPTY_SPACE_THRESHOLD_BYTES)})", "ok"
        elif not real:
            detail, status = f"alleen rapport: {_mb(empty)} leeg in book_uploaded_images, zou worden teruggegeven", "ok"
        else:
            parts = []
            if vacuumed:
                parts.append(f"ruimte teruggegeven: book_uploaded_images ({_mb(empty)} was leeg)")
            if skipped:
                parts.append("overgeslagen omdat het niet veilig was: " + ", ".join(t for t, _ in skipped))
            if errors:
                parts.append("herschrijven mislukte: " + "; ".join(errors))
            detail = ", ".join(parts)
            if size_before is not None and size_after is not None and vacuumed:
                detail += f"; database {_mb(size_before)} -> {_mb(size_after)}"
            status = "error" if errors else "ok"
        _log(conn, "cleanup", "space", status, detail)
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "cleanup", "space", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()
    return lines



# ---------- Opslagbewaking: een mail als de database vol raakt ----------

# De limiet van het gratis Supabase-abonnement: komt de database boven deze grootte, dan wordt hij
# alleen-lezen en kan er niets meer worden opgeslagen. (De 2 GB 'provisioned disk size' in Supabase is
# schijfruimte en niet de limiet.) Stap je over op een betaald abonnement, pas dit dan aan, en
# DB_LIMIT_MB in dashboard/Home.py, zodat de meldingen en de grafiek kloppen.
STORAGE_LIMIT_MB = 500
# Bij welk percentage van de limiet er een mail komt. 100 = vol: de database is dan alleen-lezen.
STORAGE_ALERT_LEVELS = (90, 95, 98, 100)
# Een niveau telt pas als verlaten als het gebruik zoveel procentpunt eronder zakt. Anders zou een
# database die rond 90% schommelt steeds opnieuw mailen.
STORAGE_ALERT_HYSTERESIS = 3
STORAGE_ALERT_SETTING = "storage_alert_level"


def _storage_alert_decision(pct, stored_level):
    """
    Bepaalt of er een mail moet komen. 'pct' is het gebruik in procenten van de limiet, 'stored_level'
    het niveau dat al gemeld is. Geeft (te_melden_niveau of None, op_te_slaan_niveau) terug.
    Is er in één keer meer dan één niveau gepasseerd, dan komt er één mail, voor het hoogste.
    """
    reached = max([level for level in STORAGE_ALERT_LEVELS if pct >= level], default=0)
    if reached > stored_level:
        return reached, reached
    if stored_level and pct < stored_level - STORAGE_ALERT_HYSTERESIS:
        return None, reached  # ruim eronder: niveau terugzetten (geen mail), zodat een volgende stijging weer meldt
    return None, stored_level


def _database_size_bytes(conn):
    """De grootte zoals Supabase de limiet toepast: de som over alle databases."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(sum(pg_database_size(datname)), 0) AS bytes FROM pg_database")
        return int(cur.fetchone()["bytes"])


def _storage_alert_mail(level, used_bytes, pct, top_tables, test=False):
    """Onderwerp en tekst van de opslagmelding."""
    used_mb = used_bytes / (1024 * 1024)
    pct_text = f"{pct:.1f}".replace(".", ",")
    if test:
        subject = "Proefbericht: opslagmelding van het boekbeheersysteem"
        intro = "Dit is een proefbericht, om te laten zien dat de melding aankomt. Er is niets gewijzigd."
    elif level >= 100:
        subject = "🚨 Boekbeheersysteem: de database is vol (alleen-lezen)"
        intro = (
            f"De database zit op {pct_text}% van de limiet van het gratis Supabase-abonnement en is daarmee "
            f"alleen-lezen geworden of staat op het punt dat te worden. Er kan dan niets meer worden "
            f"opgeslagen: synchronisaties, nieuwe boeken en foto-uploads falen."
        )
    else:
        subject = f"⚠️ Boekbeheersysteem: database op {level}% van de gratis ruimte"
        intro = (
            f"De database van je boekbeheersysteem zit op {pct_text}% van de limiet van het gratis "
            f"Supabase-abonnement (melding bij {level}%)."
        )
    lines = [
        intro,
        "",
        f"Gebruikt: {used_mb:.1f} MB van {STORAGE_LIMIT_MB} MB ({max(STORAGE_LIMIT_MB - used_mb, 0):.1f} MB vrij)".replace(".", ","),
        f"Gemeten: {dt.datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%d-%m-%Y %H:%M')}",
    ]
    if top_tables:
        lines += ["", "Grootste onderdelen:"]
        lines += [f"  {t['schema']}.{t['name']}: {_mb(t['bytes'])}" for t in top_tables]
    lines += [
        "",
        "Wat je kunt doen:",
        "1. Betaald abonnement: Supabase, jouw organisatie, Billing, upgrade naar Pro ($25 per maand, 8 GB database). "
        "Laat het weten als je overstapt, dan wordt de limiet in dit systeem aangepast.",
        "2. Ruimte vrijmaken: GitHub, Actions, 'Fotostofzuiger (afbeeldingen opruimen)', Run workflow met "
        "echt_verwijderen = ja. Dat wist de bestanden van oude foto's die niet de voorkant zijn.",
        "",
        f"Volgende meldingen komen bij {', '.join(str(l) + '%' for l in STORAGE_ALERT_LEVELS if l > level)}." if level < STORAGE_ALERT_LEVELS[-1] and not test else "",
    ]
    return subject, "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def check_storage_alerts(test_mail=False):
    """
    Controleert hoe vol de database is en mailt bij 90, 95, 98 en 100% van de limiet. Elk niveau wordt
    één keer gemeld; wat al gemeld is, staat in app_settings. Het niveau wordt pas opgeslagen als de mail
    echt is verstuurd, zodat een mislukte mail bij de volgende controle opnieuw wordt geprobeerd.
    Met test_mail=True komt er een proefbericht en wordt er niets opgeslagen. Geeft regels tekst terug.
    """
    lines = []

    def say(text=""):
        lines.append(text)

    conn = get_connection()
    try:
        used_bytes = _database_size_bytes(conn)
        used_mb = used_bytes / (1024 * 1024)
        pct = used_mb / STORAGE_LIMIT_MB * 100
        try:
            stored = int(_get_setting(conn, STORAGE_ALERT_SETTING, "0") or 0)
        except (TypeError, ValueError):
            stored = 0
        say("== Opslagcontrole ==")
        say(f"Database, zoals Supabase het telt: {_mb(used_bytes)} van {STORAGE_LIMIT_MB} MB ({f'{pct:.1f}'.replace('.', ',')}%)")
        say(f"Meldingen bij: {', '.join(str(l) + '%' for l in STORAGE_ALERT_LEVELS)}  |  laatst gemeld niveau: {stored if stored else 'geen'}")

        level, new_level = _storage_alert_decision(pct, stored)
        if test_mail or level:
            top_tables = _safe_rows(
                conn,
                "SELECT n.nspname AS schema, c.relname AS name, pg_total_relation_size(c.oid) AS bytes "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'm') ORDER BY bytes DESC LIMIT 5",
            )
            subject, body = _storage_alert_mail(level or 0, used_bytes, pct, top_tables, test=test_mail)
            try:
                notifications.send_email(subject=subject, body=body)
            except Exception as e:
                say(f"Mail {'(proefbericht) ' if test_mail else ''}mislukt: {e}")
                if not test_mail:
                    say("Het niveau is niet opgeslagen; de volgende controle probeert het opnieuw.")
                return lines
            if test_mail:
                say("Proefbericht verstuurd. Er is niets opgeslagen of gewijzigd.")
                return lines
            say(f"Mail verstuurd voor {level}%.")
            try:
                _set_setting(conn, STORAGE_ALERT_SETTING, new_level)
                _log(conn, "check", "storage", "ok", f"Opslagmelding verstuurd: {level}% ({_mb(used_bytes)} van {STORAGE_LIMIT_MB} MB)")
                conn.commit()
            except Exception:
                conn.rollback()  # bijvoorbeeld als de database al alleen-lezen is; dan kan dit niet worden vastgelegd
                say("Let op: het niveau kon niet worden opgeslagen, dus de volgende controle mailt opnieuw.")
        elif new_level != stored:
            try:
                _set_setting(conn, STORAGE_ALERT_SETTING, new_level)
            except Exception:
                conn.rollback()
            say(f"Gebruik is gedaald: niveau van {stored if stored else 'geen'} naar {new_level if new_level else 'geen'} (geen mail).")
        else:
            say("Geen melding nodig." if not stored else f"Geen nieuwe melding nodig (niveau {stored}% is al gemeld).")
    finally:
        conn.close()
    return lines



# ---------- Waakhond: een mail als de synchronisatie stilstaat ----------

# Na hoeveel uur zonder geslaagde synchronisatie er een mail komt (de eerste), en daarna herinneringen bij de volgende.
WATCHDOG_ALERT_HOURS = (3, 12, 24, 48, 96)
WATCHDOG_STATE_KEY = "watchdog_state"
WATCHDOG_WEEKLY_KEY = "watchdog_weekly"
# De onderdelen die moeten blijven draaien: (sleutel, naam, welke regel in sync_log een geslaagde keer is).
# Bol staat apart, want die kant kan stuklopen (bijvoorbeeld door een gewijzigde sleutel) terwijl Boekwinkeltjes doorloopt.
WATCHDOG_STREAMS = (
    ("bw", "Boekwinkeltjes (boeken ophalen)", {"direction": "pull", "resource": "books", "platform": "BW"}),
    ("bol", "Bol (bestellingen ophalen)", {"direction": "pull", "resource": "orders", "platform": "Bol"}),
)


# De einddatum van de GitHub-sleutel (dezelfde instelling als op Hulp en instellingen in het dashboard): mailen zoveel
# dagen van tevoren. Verloopt de sleutel, dan stoppen alle geplande taken tegelijk. De waakhond zelf gebruikt die sleutel niet.
WATCHDOG_TOKEN_KEY = "github_token_expires"
WATCHDOG_TOKEN_DAYS = (30, 14, 7, 3, 1)


WATCHDOG_TOKEN_NEVER = "never"  # in het dashboard aangevinkt: de sleutel verloopt niet, dus er is niets te bewaken


def _parse_token_expiry(raw):
    """De ingestelde einddatum als datum, 'never' als de sleutel niet verloopt, of None (niet ingesteld of onleesbaar)."""
    text = str(raw).strip() if raw else ""
    if text.lower() == WATCHDOG_TOKEN_NEVER:
        return WATCHDOG_TOKEN_NEVER
    try:
        return dt.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _watchdog_token_decision(days_left, token_state, expires_iso):
    """
    Bepaalt of er een mail komt over de einddatum van de GitHub-sleutel. 'days_left' is het aantal dagen tot de einddatum
    (None = niet ingesteld), 'token_state' wat eerder is gemeld ({'for': einddatum, 'level': ...} of None). Geeft
    (gebeurtenis, nieuwe_toestand) terug; de gebeurtenis is None, ('warn', dagen) of ('expired', -1). Elke grens (30, 14, 7, 3
    en 1 dag) wordt per einddatum één keer gemeld, en de einddatum zelf telt al als verlopen. Is er een nieuwe, verre einddatum
    ingevuld (de sleutel is vernieuwd), dan wordt de toestand opgeruimd en begint het opnieuw.
    """
    if days_left is None or days_left > WATCHDOG_TOKEN_DAYS[0]:
        return None, None
    level = -1 if days_left <= 0 else min(days for days in WATCHDOG_TOKEN_DAYS if days_left <= days)
    stored = token_state.get("level") if token_state and token_state.get("for") == expires_iso else None
    if stored is None or level < stored:
        return (("expired", -1) if level == -1 else ("warn", level)), {"for": expires_iso, "level": level}
    return None, token_state


def _watchdog_token_line(token_expiry, today):
    if token_expiry == WATCHDOG_TOKEN_NEVER:
        return "- GitHub-sleutel: verloopt niet (zo ingesteld)"
    if not token_expiry:
        return "- GitHub-sleutel: einddatum niet ingesteld (vul hem in bij Hulp en instellingen in het dashboard)"
    days = (token_expiry - today).days
    when = token_expiry.strftime("%d-%m-%Y")
    if days <= 0:
        return f"- GitHub-sleutel: VERLOPEN of verloopt vandaag ({when})"
    return f"- GitHub-sleutel: verloopt op {when} (nog {days} {'dag' if days == 1 else 'dagen'})"


def _watchdog_token_mail(event, days_left, expiry):
    when = expiry.strftime("%d-%m-%Y")
    if event == "expired":
        subject = "🚨 Boekbeheer: de GitHub-sleutel is verlopen"
        intro = (
            f"De GitHub-sleutel is verlopen of verloopt vandaag ({when}). De geplande taken en de knoppen in het "
            f"dashboard werken daardoor niet meer: elke aanroep geeft 'Unauthorized' en de synchronisaties stoppen."
        )
    else:
        unit = "dag" if days_left == 1 else "dagen"
        subject = f"⚠️ Boekbeheer: de GitHub-sleutel verloopt over {days_left} {unit}"
        intro = (
            f"De GitHub-sleutel verloopt op {when}, over {days_left} {unit}. Daarna geeft elke geplande taak bij cron-job.org "
            f"'Unauthorized', werken de knoppen in het dashboard niet meer en stoppen de synchronisaties."
        )
    lines = [
        intro,
        "",
        "Zo vernieuw je hem:",
        "1. GitHub (profielfoto rechtsboven): Settings, Developer settings, Personal access tokens, Fine-grained tokens.",
        "2. Maak een nieuwe sleutel voor alleen de repository boekbeheer, met het recht 'Actions: Read and write', en kies de "
        "langst mogelijke looptijd. Of kies 'Regenerate token' bij de bestaande sleutel, als GitHub dat aanbiedt.",
        "3. Kopieer de nieuwe waarde meteen (GitHub toont hem maar één keer) en deel hem nergens, ook niet in een chat.",
        "4. Vervang hem op twee plekken: in de Streamlit-instellingen (GITHUB_TOKEN) en in de kopregel Authorization "
        "('Bearer ...') van elke job bij cron-job.org.",
        "5. Vul de nieuwe einddatum in bij Hulp en instellingen in het dashboard. Dan stopt deze waarschuwing.",
        "",
        f"Deze waarschuwing komt bij {', '.join(str(d) for d in WATCHDOG_TOKEN_DAYS)} dagen voor de einddatum, en als hij verlopen is.",
    ]
    return subject, "\n".join(lines) + "\n"


def _watchdog_decision(age_hours, stream_state, current_for):
    """
    Bepaalt wat er voor één onderdeel moet gebeuren. 'age_hours' is het aantal uur sinds de laatste geslaagde keer
    (None = nog nooit), 'stream_state' wat eerder is gemeld ({'for': ..., 'level': ...} of None) en 'current_for'
    herkent de huidige stilstand (het tijdstip van de laatste geslaagde keer). Geeft (gebeurtenis, nieuwe_toestand)
    terug; de gebeurtenis is None, ('alert', uren) of ('recovered', None). Elk niveau wordt per stilstand één keer
    gemeld, en een herstel ook.
    """
    age = float("inf") if age_hours is None else age_hours
    reached = max([hours for hours in WATCHDOG_ALERT_HOURS if age >= hours], default=0)
    stored = 0
    if stream_state and stream_state.get("for") == current_for:
        stored = int(stream_state.get("level") or 0)
    if reached > stored:
        return ("alert", reached), {"for": current_for, "level": reached}
    if reached == 0 and stream_state:
        return ("recovered", None), None
    return None, stream_state


def _watchdog_weekly_due(local, last_week_key):
    """Is het tijd voor het weekoverzicht? Maandag tussen 7 en 12 uur (Nederlandse tijd), één keer per week."""
    iso = local.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    return local.weekday() == 0 and 7 <= local.hour < 12 and last_week_key != week, week


def _hours_text(hours):
    if hours is None or hours == float("inf"):
        return "onbekend"
    return f"{hours:.1f}".replace(".", ",") + " uur"


def _local_text(moment, tz):
    return moment.astimezone(tz).strftime("%d-%m-%Y %H:%M") if moment else "nog nooit"


def _watchdog_status_lines(last_ok, now, tz):
    lines = []
    for key, name, _where in WATCHDOG_STREAMS:
        moment = last_ok.get(key)
        age = (now - moment).total_seconds() / 3600 if moment else None
        lines.append(
            f"- {name}: laatste geslaagde keer {_local_text(moment, tz)}"
            + (f" ({_hours_text(age)} geleden)" if moment else "")
        )
    return lines


def _watchdog_recent_lines(recent, tz):
    lines = []
    for row in recent or []:
        detail = f" — {row['detail']}" if row.get("detail") else ""
        lines.append(
            f"  {_local_text(row['run_at'], tz)}  {row['direction']} {row['resource']} ({row['platform']}): {row['status']}{detail}"
        )
    return lines


WATCHDOG_CHECKLIST = [
    "Dit kun je nakijken:",
    "1. cron-job.org: staan de jobs nog aan? Is er geen einddatum verstreken en is er geen job uitgeschakeld?",
    "2. De GitHub-sleutel in de aanroepen van cron-job.org: is die niet verlopen? Dan geeft elke aanroep 'Unauthorized'.",
    "3. GitHub zelf: staat er een storing op githubstatus.com, of staat er een vastgelopen run bij Actions? Bij "
    "'Pending' eerst de lopende run annuleren.",
    "4. Supabase: is de database bereikbaar en niet vol?",
    "",
    "Deze waakhond draait los van cron-job.org en van je GitHub-sleutel, via GitHub's eigen tijdschema.",
]


def _watchdog_alert_mail(events, last_ok, recent, now, tz):
    names = " en ".join(name for _key, name, _level in events)
    lines = ["De waakhond ziet dat er te lang geen geslaagde synchronisatie is geweest.", ""]
    for key, name, level in events:
        moment = last_ok.get(key)
        age = (now - moment).total_seconds() / 3600 if moment else None
        lines.append(f"- {name}: laatste geslaagde keer {_local_text(moment, tz)} ({_hours_text(age)} geleden; melding bij {level} uur)")
    lines += ["", "Laatste regels uit het synchronisatielogboek:"] + (_watchdog_recent_lines(recent, tz) or ["  (geen)"]) + [""]
    lines += WATCHDOG_CHECKLIST
    return f"⚠️ Boekbeheer: synchronisatie staat stil ({names})", "\n".join(lines) + "\n"


def _watchdog_recovery_mail(events, last_ok, state, tz):
    names = " en ".join(name for _key, name in events)
    lines = ["De synchronisatie draait weer.", ""]
    for key, name in events:
        before = state.get(key, {}).get("for")
        try:
            gap_start = dt.datetime.fromisoformat(before) if before and before != "never" else None
        except ValueError:
            gap_start = None
        now_ok = last_ok.get(key)
        gap = (now_ok - gap_start).total_seconds() / 3600 if gap_start and now_ok else None
        lines.append(
            f"- {name}: weer geslaagd op {_local_text(now_ok, tz)}"
            + (f"; de stilstand duurde {_hours_text(gap)} (sinds {_local_text(gap_start, tz)})" if gap is not None else "")
        )
    return f"✅ Boekbeheer: synchronisatie draait weer ({names})", "\n".join(lines) + "\n"


def _watchdog_overview_mail(last_ok, overview, db_bytes, week, now, tz, test=False, token_expiry=None):
    stale = [
        name for key, name, _w in WATCHDOG_STREAMS
        if not last_ok.get(key) or (now - last_ok[key]).total_seconds() / 3600 >= WATCHDOG_ALERT_HOURS[0]
    ]
    today = now.astimezone(tz).date()
    token_days = (token_expiry - today).days if isinstance(token_expiry, dt.date) else None
    lines = ["LET OP: " + " en ".join(stale) + " staat stil." if stale else "Alles werkt."]
    if token_days is not None and token_days <= WATCHDOG_TOKEN_DAYS[0]:
        lines.append("LET OP: de GitHub-sleutel " + ("is verlopen." if token_days <= 0 else f"verloopt over {token_days} dagen."))
    lines.append("")
    lines += _watchdog_status_lines(last_ok, now, tz)
    lines.append(_watchdog_token_line(token_expiry, today))
    if overview is not None:
        lines.append(
            f"- Afgelopen 24 uur, Boekwinkeltjes: {overview.get('ok', 0)} geslaagde en {overview.get('error', 0)} mislukte synchronisaties van boeken"
        )
    if db_bytes is not None:
        pct = db_bytes / (1024 * 1024) / STORAGE_LIMIT_MB * 100
        lines.append(f"- Database: {_mb(db_bytes)} van {STORAGE_LIMIT_MB} MB ({f'{pct:.1f}'.replace('.', ',')}%)")
    lines += [
        "",
        "Krijg je dit overzicht een maandag niet, dan staat ook de waakhond zelf stil: GitHub zet tijdschema's in een "
        "openbare repository na 60 dagen zonder activiteit uit. Zet hem dan opnieuw aan bij Actions.",
    ]
    subject = ("Proefbericht: " if test else "") + f"Boekbeheer-waakhond: weekoverzicht ({week})"
    if test:
        lines.insert(0, "Dit is een proefbericht; er is niets opgeslagen of gewijzigd.")
        lines.insert(1, "")
    return subject, "\n".join(lines) + "\n"


def _watchdog_read(conn, need_overview):
    """Leest alles wat de waakhond nodig heeft in één keer, zodat een storing in de database op één plek wordt gevangen."""
    data = {"last_ok": {}, "state": {}, "weekly": None, "recent": [], "overview": None, "db_bytes": None, "token_expires": None}
    for key, _name, where in WATCHDOG_STREAMS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(run_at) AS last_ok FROM sync_log "
                "WHERE direction = %(direction)s AND resource = %(resource)s AND platform = %(platform)s AND status = 'ok'",
                where,
            )
            row = cur.fetchone()
        data["last_ok"][key] = row["last_ok"] if row else None
    raw = _get_setting(conn, WATCHDOG_STATE_KEY, "") or ""
    try:
        parsed = json.loads(raw) if raw else {}
        data["state"] = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        data["state"] = {}
    data["weekly"] = _get_setting(conn, WATCHDOG_WEEKLY_KEY, None)
    data["token_expires"] = _get_setting(conn, WATCHDOG_TOKEN_KEY, None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_at, direction, resource, platform, status, left(detail, 120) AS detail "
            "FROM sync_log ORDER BY run_at DESC LIMIT 8"
        )
        data["recent"] = cur.fetchall()
    if need_overview:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) AS n FROM sync_log WHERE direction = 'pull' AND resource = 'books' "
                "AND platform = 'BW' AND run_at > now() - interval '24 hours' GROUP BY status"
            )
            data["overview"] = {row["status"]: row["n"] for row in cur.fetchall()}
        try:
            data["db_bytes"] = _database_size_bytes(conn)
        except Exception:
            data["db_bytes"] = None
    return data


def watchdog_check(now=None, test_mail=False):
    """
    De waakhond: controleert of de synchronisatie nog draait en mailt als dat te lang niet zo is. Draait elk uur, via
    GitHub's eigen tijdschema en dus los van cron-job.org en je GitHub-sleutel (zie .github/workflows/watchdog.yml).
    Per onderdeel (Boekwinkeltjes, Bol) komt er een mail zodra de laatste geslaagde synchronisatie langer dan 3 uur
    geleden is, daarna herinneringen bij 12, 24, 48 en 96 uur, en een mail zodra het weer werkt. Elke maandagochtend komt
    een kort weekoverzicht: zo betekent stilte iets, want ook een tijdschema op GitHub kan stoppen.
    Met test_mail=True komt alleen het overzicht als proefbericht en wordt er niets opgeslagen.
    Geeft (regels tekst, mislukt) terug; 'mislukt' is True als een mail niet verstuurd kon worden of de database niet
    bereikbaar was (dan wordt de workflow rood).
    """
    lines = []
    failed = False
    tz = ZoneInfo("Europe/Amsterdam")
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(tz)

    def say(text=""):
        lines.append(text)

    def send(subject, body):
        nonlocal failed
        try:
            notifications.send_email(subject=subject, body=body)
            return True
        except Exception as e:
            failed = True
            say(f"Mail mislukt: {e}")
            return False

    say("== Waakhond ==")
    say(f"Controle op {local:%d-%m-%Y %H:%M} (Nederlandse tijd)")

    conn = None
    try:
        conn = get_connection()
        data = _watchdog_read(conn, need_overview=True)
    except Exception as e:
        failed = True
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        say(f"De database is niet bereikbaar ({type(e).__name__}).")
        if local.hour % 6 == 0:
            send(
                "⚠️ Boekbeheer-waakhond: de database is niet bereikbaar",
                "De waakhond kan de database niet bereiken, dus ook niet controleren of de synchronisatie draait.\n\n"
                f"Soort fout: {type(e).__name__}\n\n"
                "Dit kun je nakijken: staat er een storing op status.supabase.com, is het project niet gepauzeerd of vol, "
                "en klopt SUPABASE_DB_URL nog bij de GitHub-secrets?\n\n"
                "Deze melding komt hooguit om de 6 uur terug zolang het probleem blijft.\n",
            )
        else:
            say("Geen mail nu: dit wordt hooguit om de 6 uur gemeld.")
        return lines, failed

    try:
        last_ok, state = data["last_ok"], data["state"]
        token_expiry = _parse_token_expiry(data["token_expires"])
        for line in _watchdog_status_lines(last_ok, now, tz):
            say(line)
        say(_watchdog_token_line(token_expiry, local.date()))

        if test_mail:
            week = f"{local.isocalendar()[0]}-W{local.isocalendar()[1]:02d}"
            subject, body = _watchdog_overview_mail(
                last_ok, data["overview"], data["db_bytes"], week, now, tz, test=True, token_expiry=token_expiry
            )
            if send(subject, body):
                say("Proefbericht verstuurd. Er is niets opgeslagen of gewijzigd.")
            return lines, failed

        # --- stilstand en herstel, per onderdeel ---
        new_state = dict(state)
        alerts, recoveries = [], []
        for key, name, _where in WATCHDOG_STREAMS:
            moment = last_ok.get(key)
            age = (now - moment).total_seconds() / 3600 if moment else None
            current_for = moment.isoformat() if moment else "never"
            event, stream_state = _watchdog_decision(age, state.get(key), current_for)
            if event and event[0] == "alert":
                alerts.append((key, name, event[1]))
                new_state[key] = stream_state
            elif event and event[0] == "recovered":
                recoveries.append((key, name))
        saved_state = dict(state)
        if alerts:
            subject, body = _watchdog_alert_mail(alerts, last_ok, data["recent"], now, tz)
            if send(subject, body):
                say("Mail verstuurd: " + ", ".join(f"{name} ({level} uur)" for _k, name, level in alerts))
                for key, _name, _level in alerts:
                    saved_state[key] = new_state[key]
            else:
                say("De stilstand is niet vastgelegd; de volgende controle probeert de mail opnieuw.")
        if recoveries:
            subject, body = _watchdog_recovery_mail(recoveries, last_ok, state, tz)
            if send(subject, body):
                say("Mail verstuurd: weer in orde (" + ", ".join(name for _k, name in recoveries) + ")")
                for key, _name in recoveries:
                    saved_state.pop(key, None)
            else:
                say("Het herstel is niet vastgelegd; de volgende controle probeert de mail opnieuw.")
        # --- einddatum van de GitHub-sleutel ---
        has_date = isinstance(token_expiry, dt.date)   # 'never' en 'niet ingesteld' hebben geen datum om af te tellen
        days_left = (token_expiry - local.date()).days if has_date else None
        token_event, token_state = _watchdog_token_decision(
            days_left, state.get("token"), token_expiry.isoformat() if has_date else None
        )
        token_mailed = False
        if token_event:
            subject, body = _watchdog_token_mail(token_event[0], days_left, token_expiry)
            if send(subject, body):
                token_mailed = True
                say(f"Mail verstuurd: de GitHub-sleutel verloopt {'(is verlopen)' if token_event[0] == 'expired' else f'over {days_left} dag(en)'}")
                saved_state["token"] = token_state
            else:
                say("De waarschuwing over de GitHub-sleutel is niet vastgelegd; de volgende controle probeert het opnieuw.")
        elif token_state is None and "token" in saved_state:
            saved_state.pop("token")  # een nieuwe, verre einddatum (de sleutel is vernieuwd): opruimen

        if saved_state != state:
            try:
                _set_setting(conn, WATCHDOG_STATE_KEY, json.dumps(saved_state))
            except Exception:
                conn.rollback()
                say("Let op: de toestand kon niet worden opgeslagen, dus de volgende controle kan dezelfde mail opnieuw sturen.")
        if not alerts and not recoveries and not token_mailed:
            say("Alles draait; geen melding nodig.")

        # --- weekoverzicht ---
        due, week = _watchdog_weekly_due(local, data["weekly"])
        if due:
            subject, body = _watchdog_overview_mail(
                last_ok, data["overview"], data["db_bytes"], week, now, tz, token_expiry=token_expiry
            )
            if send(subject, body):
                say(f"Weekoverzicht verstuurd ({week}).")
                try:
                    _set_setting(conn, WATCHDOG_WEEKLY_KEY, week)
                except Exception:
                    conn.rollback()
    finally:
        conn.close()
    return lines, failed



# ---------- Nieuwe boeken pushen naar Bol ----------

# 'MijnLeverbelofte' is niet zomaar een beschrijving maar de letterlijke, echte
# deliveryCode voor een eigen ingesteld leverbeloofteprofiel — bevestigd via
# Bol's documentatie en een onafhankelijke bron. Bol vult daarbij zelf de
# juiste levertijd in, afhankelijk van wanneer de bestelling binnenkomt.
BOL_DELIVERY_CODE = "MijnLeverbelofte"

BOL_ECONOMIC_OPERATOR_NAME = "Severein Services"


# Termen in 'Bijzonderheden' die de app zelf neerzet (jaar, bladzijden, band) of die
# gewoon een standaardkenmerk zijn: dat is geen opmerking over de staat van het boek,
# dus die gaan nooit als toelichting naar Bol.
_BOL_STANDARD_TERM = re.compile(
    r"\d{4}|\d+\s*(?:pp|p|blz|pag)\.?|\d*e?\s*(?:druk|herdruk)"
    r"|gebonden|paperback|mass market paperback|hardcover|hardback|softcover|pocket|ingenaaid|geniet"
    r"|spiraalgebonden|spiraal|spiral-bound|board book|library binding|kartonboek|gebrocheerd",
    re.IGNORECASE,
)
# Bol wijst een toelichting af met een e-mailadres of telefoonnummer erin. Zo'n opmerking
# dan liever weglaten dan de aanbieding laten mislukken (die zou elke halfuur opnieuw
# geprobeerd worden, met steeds een foutmelding).
_BOL_COMMENT_FORBIDDEN = re.compile(r"@|\+?\d[\d\s().-]{6,}\d")
_BOL_COMMENT_MAX_CHARS = 500  # Bol staat 2000 toe; dit blijft er ruim onder


def _bol_condition_remark(short_description):
    """
    Het laatste deel van 'Bijzonderheden' (na de laatste komma), als dat een opmerking
    over de staat van het boek is, bijv. 'kaft ontbreekt' in '1999, 200pp, gebonden,
    kaft ontbreekt'. Is het laatste deel een standaardterm (jaar, bladzijden, band,
    druk) of bevat het een e-mailadres/telefoonnummer, dan is er geen opmerking: None.
    """
    last = (short_description or "").split(",")[-1]
    last = re.sub(r"\s+", " ", last).strip(" .;:-\u2013\u2014\t")
    if not last or _BOL_STANDARD_TERM.fullmatch(last) or _BOL_COMMENT_FORBIDDEN.search(last):
        return None
    return last[:_BOL_COMMENT_MAX_CHARS]


def _derive_bol_condition(short_description):
    """
    Leidt de Bol-conditie (en een toelichtend commentaar) af uit het veld
    'Bijzonderheden', volgens vaste regels:
      - 'nieuw' (maar niet 'nieuwstaat') en/of 'folie' -> nieuw
      - 'nieuwstaat' -> tweedehands / zo goed als nieuw, 'Geen leessporen'
      - 'leessporen' -> tweedehands / redelijk, 'Leessporen'
      - anders -> tweedehands / goed, 'Leessporen'
    Bij 'redelijk' en 'goed' (dus niet bij nieuw en zo goed als nieuw) komt daar de
    laatste opmerking uit 'Bijzonderheden' achter, zodat een koper het ook weet als er
    bijv. een kaft ontbreekt: '1999, 200pp, gebonden, kaft ontbreekt' wordt
    'Leessporen, kaft ontbreekt'. Bevat die opmerking zelf al 'leessporen' (bijv. 'veel
    leessporen'), dan vervangt zij het vaste woord in plaats van erbij te komen.
    """
    text = (short_description or "").lower()
    # De conditie staat hier in een neutrale vorm: {'category': 'NEW'} of {'category': 'SECONDHAND',
    # 'state': ...}. bol_client._condition_payload vertaalt dat naar wat de gekozen versie van
    # Bol's API verwacht (versie 10 wil bij 'nieuw' ook een 'name', versie 11 niet).
    if ("nieuw" in text and "nieuwstaat" not in text) or "folie" in text:
        return {"category": "NEW"}, None
    if "nieuwstaat" in text:
        return {"category": "SECONDHAND", "state": "AS_NEW"}, "Geen leessporen"

    condition = (
        {"category": "SECONDHAND", "state": "MODERATE"}
        if "leessporen" in text
        else {"category": "SECONDHAND", "state": "GOOD"}
    )
    comment = "Leessporen"
    remark = _bol_condition_remark(short_description)
    if remark:
        if "leessporen" in remark.lower():
            comment = remark[0].upper() + remark[1:]
        else:
            comment = f"Leessporen, {remark}"
    return condition, comment


def push_new_books_to_bol():
    """
    Maakt voor elk boek dat nog geen Bol-aanbieding heeft een nieuwe aanbieding
    aan bij Bol — alleen als: er een ISBN bekend is, het boek niet als
    'ongeschikt voor Bol' is gemarkeerd (shipping_cost_bol leeg), én het boek
    via de app zelf is aangemaakt (Nieuw boek of bulk-import), dus NIET via de
    gewone Boekwinkeltjes-pull. Dit is bewust: boeken die al vóór deze functie
    bestond zijn aangemaakt, of die alleen op Boekwinkeltjes horen te staan,
    mogen nooit met terugwerkende kracht alsnog naar Bol gepusht worden.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.ean, b.title, b.location, b.amount, b.shipping_cost_bol, b.short_description
                FROM books b
                LEFT JOIN bol_offer_mapping m ON m.ean = b.ean
                WHERE b.id > 0 AND COALESCE(b.queued, FALSE) = FALSE
                  AND b.ean IS NOT NULL AND b.ean != '' AND b.ean != '0'
                  AND b.shipping_cost_bol IS NOT NULL
                  AND b.created_via_app = TRUE
                  AND m.ean IS NULL
                """
            )
            candidates = cur.fetchall()

        # De verantwoordelijke partij en het leverbelofte-profiel worden alleen opgezocht als er
        # echt een aanbieding aangemaakt moet worden. Bij versie 10 kost dat opzoeken een
        # volledige export, en Bol staat er maar 9 per uur toe; dit liep bij elke sync, ook
        # als er niets te doen was.
        economic_operator_id = None
        delivery_profile_id = None
        if candidates:
            # Eerst proberen af te lezen uit een bestaande aanbieding (betrouwbaarder
            # gebleken dan de aparte Economic Operators-API); pas als dat niets
            # oplevert, de opzoeking op naam proberen als reserveweg.
            economic_operator_id = bol_client.get_economic_operator_id_from_offers()
            if not economic_operator_id:
                economic_operator_id = bol_client.get_economic_operator_id(BOL_ECONOMIC_OPERATOR_NAME)
            # Het leverbelofte-profiel van je bestaande aanbiedingen (alleen nodig bij versie 11).
            delivery_profile_id = bol_client.get_delivery_profile_id_from_offers()

        created = 0
        errors = []
        for book in candidates:
            try:
                condition, comment = _derive_bol_condition(book["short_description"])

                new_offer_id = bol_client.create_offer(
                    ean=book["ean"],
                    condition=condition,
                    price=float(book["shipping_cost_bol"]),
                    stock_amount=int(book["amount"] or 0),
                    reference=book["location"],
                    delivery_code=BOL_DELIVERY_CODE,
                    economic_operator_id=economic_operator_id,
                    comment=comment,
                    delivery_profile_id=delivery_profile_id,
                )

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO bol_offer_mapping (ean, offer_id, bol_stock, last_synced_at)
                        VALUES (%(ean)s, %(offer_id)s, %(stock)s, %(now)s)
                        ON CONFLICT (ean) DO UPDATE SET
                            offer_id = excluded.offer_id, bol_stock = excluded.bol_stock,
                            last_synced_at = excluded.last_synced_at
                        """,
                        {"ean": book["ean"], "offer_id": new_offer_id, "stock": book["amount"] or 0, "now": _now()},
                    )
                conn.commit()
                created += 1
            except bol_client.BolAPIError as e:
                errors.append(f"boek {book['id']} ({book['title']}): {e}")

        detail = f"{created} {_n(created, 'nieuw boek', 'nieuwe boeken')} aangemaakt bij Bol"
        if errors:
            detail += f" — {len(errors)} fout(en): " + "; ".join(errors[:5])
        _log(conn, "push", "new_offers", "ok" if not errors else "error", detail, platform="Bol")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "push", "new_offers", "error", str(e), platform="Bol")
        conn.commit()
        raise


def _choose_one_offer_per_ean(offers, mapped_offer_ids):
    """
    Bol kan meer dan één aanbieding voor hetzelfde ean hebben (bijvoorbeeld een nieuwe én een
    tweedehands, of eentje die ooit via het Bol-dashboard is aangemaakt). De koppeltabel kent
    er maar één per ean. Welke dat is, mag niet afhangen van de volgorde waarin Bol ze
    toevallig teruggeeft: bij versie 11 is dat de volgorde van laatste wijziging, en die
    wisselt zodra een van de aanbiedingen wordt aangeraakt (ook door onze eigen voorraadupdate).
    Regel: de aanbieding waar de koppeling al naar wijst blijft gekozen; bestaat die nog niet,
    dan de aanbieding met het kleinste offerId. Zo is de keuze stabiel van run tot run.
    'mapped_offer_ids' is {ean: offer_id} zoals nu in de koppeltabel staat.
    """
    chosen = {}
    for offer in offers:
        ean = offer["ean"]

        def rank(o):
            return (0 if mapped_offer_ids.get(ean) == o["offer_id"] else 1, o["offer_id"])

        best = chosen.get(ean)
        if best is None or rank(offer) <= rank(best):  # bij gelijke stand: de laatste, meest actuele
            chosen[ean] = offer
    return list(chosen.values())


_BOL_CONDITION_COLUMNS_CHECKED = False


def _ensure_bol_condition_columns(conn):
    """
    Zorgt dat bol_offer_mapping de kolommen voor de conditie van het Bol-aanbod heeft (voor de taartgrafiek op Home).
    Eerst kijken en alleen aanmaken als ze ontbreken, en meteen vastleggen: ook 'ADD COLUMN IF NOT EXISTS' vergrendelt
    de tabel tot het einde van de transactie, en dan moet Home wachten tot de hele voorraadsync klaar is
    (en loopt zijn vraag af met 'QueryCanceled').
    """
    global _BOL_CONDITION_COLUMNS_CHECKED
    if _BOL_CONDITION_COLUMNS_CHECKED:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'bol_offer_mapping' "
            "AND column_name IN ('condition_category', 'condition_state')"
        )
        present = {row["column_name"] for row in cur.fetchall()}
        if "condition_category" not in present:
            cur.execute("ALTER TABLE bol_offer_mapping ADD COLUMN IF NOT EXISTS condition_category TEXT")
        if "condition_state" not in present:
            cur.execute("ALTER TABLE bol_offer_mapping ADD COLUMN IF NOT EXISTS condition_state TEXT")
    conn.commit()
    _BOL_CONDITION_COLUMNS_CHECKED = True


def sync_stock_with_bol():
    """
    Voorraad afstemmen met Bol — met opzet ASYMMETRISCH:

    - Een verkoop bij Bol (een nieuwe, bevestigde order — gedetecteerd via
      pull_bol_orders(), niet via een kaal voorraadgetal) verlaagt onze eigen
      voorraad met 1, en die verlaging wordt ook naar Boekwinkeltjes gestuurd.
    - Een lager voorraadgetal bij Bol ZONDER bijbehorende order (bijvoorbeeld
      omdat een boek daar handmatig is verwijderd of op 0 gezet omdat het daar
      niet meer past) wordt NOOIT gebruikt om onze eigen voorraad of die bij
      Boekwinkeltjes te verlagen — dat is een keuze die niets met de fysieke
      voorraad te maken hoeft te hebben.
    - Is ONS aantal lager dan wat Bol laat zien (bijv. na een Boekwinkeltjes-
      verkoop of een zojuist verwerkte Bol-verkoop), dan zetten we Bol's aantal
      wél naar beneden bij, om overselling bij Bol te voorkomen. Nooit
      andersom: we zetten Bol's aantal nooit omhoog, want dat zou een bewuste
      verwijdering/verlaging bij Bol ongedaan maken.

    Vereist dat pull_books() en pull_bol_orders() hiervoor al gedraaid hebben,
    zodat zowel de lokale voorraad als de Bol-orders actueel zijn.
    """
    conn = get_connection()
    try:
        bol_offers = bol_client.get_all_offers()
        with conn.cursor() as cur:
            cur.execute("SELECT ean, offer_id FROM bol_offer_mapping")
            mapped_offer_ids = {row["ean"]: row["offer_id"] for row in cur.fetchall()}
        bol_offers = _choose_one_offer_per_ean(bol_offers, mapped_offer_ids)
        _ensure_bol_condition_columns(conn)
        with conn.cursor() as cur:
            for offer in bol_offers:
                cur.execute(
                    """
                    INSERT INTO bol_offer_mapping (ean, offer_id, bol_stock, last_synced_at, condition_category, condition_state)
                    VALUES (%(ean)s, %(offer_id)s, %(stock)s, %(now)s, %(cat)s, %(state)s)
                    ON CONFLICT (ean) DO UPDATE SET
                        offer_id = excluded.offer_id,
                        bol_stock = excluded.bol_stock,
                        last_synced_at = excluded.last_synced_at,
                        -- Geeft Bol geen conditie terug (bijv. versie 10), dan blijft de bekende staan.
                        condition_category = COALESCE(excluded.condition_category, bol_offer_mapping.condition_category),
                        condition_state = CASE WHEN excluded.condition_category IS NULL
                                               THEN bol_offer_mapping.condition_state ELSE excluded.condition_state END
                    """,
                    {
                        "ean": offer["ean"], "offer_id": offer["offer_id"], "stock": offer["stock"], "now": _now(),
                        "cat": offer.get("condition_category"), "state": offer.get("condition_state"),
                    },
                )
        conn.commit()

        # Stap 1: nieuwe, bevestigde Bol-verkopen verwerken (via echte orders,
        # nooit via een kaal voorraadgetal) — elke order hooguit één keer.
        # Een orderregel met alleen een annuleringsaanvraag (CANCELLATION_REQUESTED) telt
        # bewust nog wél als verkocht, tot de annulering definitief is: zo kan er niet per
        # ongeluk een boek dubbel verkocht worden terwijl de aanvraag nog openstaat.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, book_id FROM orders
                WHERE platform = 'Bol' AND COALESCE(stock_adjusted, FALSE) = FALSE
                  AND status != 'CANCELLED' AND book_id IS NOT NULL
                """
            )
            new_sales = cur.fetchall()

        sold_count = 0
        for sale in new_sales:
            with conn.cursor() as cur:
                cur.execute("SELECT amount, price FROM books WHERE id = %(id)s", {"id": sale["book_id"]})
                book_row = cur.fetchone()
                if book_row:
                    new_amount = max((book_row["amount"] or 0) - 1, 0)
                    cur.execute(
                        "UPDATE books SET amount = %(amount)s WHERE id = %(id)s",
                        {"amount": new_amount, "id": sale["book_id"]},
                    )
                    try:
                        payload = {"amount": new_amount}
                        if book_row["price"] is not None:
                            payload["price"] = float(book_row["price"])
                        api_client.update_book(sale["book_id"], payload)
                    except Exception:
                        pass  # wordt bij de eerstvolgende sync opnieuw geprobeerd, dankzij stap 2 hieronder
                cur.execute("UPDATE orders SET stock_adjusted = TRUE WHERE id = %(id)s", {"id": sale["id"]})
            conn.commit()
            sold_count += 1

        # Stap 2: onze eigen voorraad (nooit andersom) naar Bol pushen, maar
        # uitsluitend als bescherming tegen overselling — dus alleen als WIJ
        # minder hebben dan Bol op dit moment laat zien.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.id, b.ean, b.amount, m.offer_id, m.bol_stock
                FROM books b
                JOIN bol_offer_mapping m ON m.ean = b.ean
                WHERE b.id > 0 AND COALESCE(b.queued, FALSE) = FALSE
                  AND b.ean IS NOT NULL AND b.ean != '' AND b.ean != '0'
                """
            )
            rows = cur.fetchall()

        bol_adjustments = 0
        errors = []
        gone = []  # aanbiedingen die bij Bol niet meer bestaan (bijvoorbeeld daar handmatig verwijderd)
        for row in rows:
            local_amount = row["amount"] or 0
            bol_stock = row["bol_stock"] or 0
            if local_amount < bol_stock:
                try:
                    bol_client.update_offer_stock(row["offer_id"], local_amount)
                    bol_adjustments += 1
                except bol_client.BolAPIError as e:
                    if getattr(e, "status_code", None) != 404:
                        errors.append(f"Bol {row['offer_id']}: {e}")
                        continue
                    # Een 404 betekent meestal dat de aanbieding bij Bol niet meer bestaat. Dan hield de koppeltabel
                    # nog het laatste voorraadgetal van toen, en probeerde elke sync opnieuw het aantal aan te passen.
                    # Eerst nagaan of de aanbieding echt weg is, anders is er iets anders aan de hand.
                    try:
                        exists = bol_client.offer_exists(row["offer_id"])
                    except Exception:
                        exists = None
                    if exists is False:
                        # De koppeling blijft staan (anders zou de app denken dat dit boek nog geen Bol-aanbieding heeft
                        # en er een nieuwe aanmaken); alleen het onthouden getal gaat naar 0. Verschijnt de aanbieding
                        # later weer bij Bol, dan wordt dit bij de volgende sync vanzelf ververst.
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE bol_offer_mapping SET bol_stock = 0 WHERE offer_id = %(offer_id)s",
                                {"offer_id": row["offer_id"]},
                            )
                        conn.commit()
                        gone.append(f"{row.get('ean') or '?'} ({str(row['offer_id'])[:8]})")
                    elif exists is True:
                        errors.append(
                            f"Bol {row['offer_id']}: {e} — maar de aanbieding bestaat wel (opvragen lukt), dus dit is "
                            f"niet 'verwijderd bij Bol'"
                        )
                    else:
                        errors.append(f"Bol {row['offer_id']}: {e}")

        detail = (
            f"{sold_count} {_n(sold_count, 'Bol-verkoop', 'Bol-verkopen')} verwerkt, "
            f"{bol_adjustments} {_n(bol_adjustments, 'Bol-aanbieding', 'Bol-aanbiedingen')} naar beneden bijgesteld"
        )
        if gone:
            detail += (
                f", {len(gone)} {_n(len(gone), 'aanbieding bestaat', 'aanbiedingen bestaan')} niet meer bij Bol "
                f"(onthouden getal op 0 gezet, voorraad niet aangepast): " + ", ".join(gone[:5])
            )
        if errors:
            detail += f" — {len(errors)} fout(en): " + "; ".join(errors[:5])
        _log(conn, "sync", "stock", "ok" if not errors else "error", detail, platform="Bol")
        conn.commit()
    except Exception as e:
        conn.rollback()
        _log(conn, "sync", "stock", "error", str(e), platform="Bol")
        conn.commit()
        raise


def quick_stock_sync():
    """
    Lichte, snelle synchronisatie voor tussen de volledige syncs in: ververst
    de boekenlijst (voor actuele Boekwinkeltjes-voorraad) en de Bol-orders
    (om echte verkopen te herkennen), en trekt de voorraad daarna gelijk.
    Bedoeld om vaak te draaien (bijv. elke 15 minuten).
    """
    pull_books()
    try:
        pull_bol_orders()
    except Exception:
        pass  # al gelogd in pull_bol_orders(); zonder verse orders werkt sync_stock_with_bol nog steeds, alleen minder actueel
    sync_stock_with_bol()


def _rows_to_csv_bytes(rows):
    """Zet een lijst dict-achtige databaserijen om naar CSV-bytes (utf-8-sig, net als de eerdere handmatige export)."""
    if not rows:
        return "".encode("utf-8-sig")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    return output.getvalue().encode("utf-8-sig")


# E-mailbijlagen worden door het versturen groter (base64, ongeveer +37%). Gmail weigert berichten boven 25 MB,
# dus houden we de bijlage zelf onder de 17 MB.
EMAIL_MAX_ATTACHMENT_BYTES = 17 * 1024 * 1024


class BackupProblem(Exception):
    """De back-up is niet (veilig) op de bedoelde plek gekomen; het draaien van de job wordt 'rood'."""


def _build_backup_zip(today_str, books_rows, orders_rows):
    """Eén gecomprimeerd zip-bestand met boeken_JJJJ-MM-DD.csv en orders_JJJJ-MM-DD.csv (CSV comprimeert zeer goed)."""
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(f"boeken_{today_str}.csv", _rows_to_csv_bytes(books_rows))
        archive.writestr(f"orders_{today_str}.csv", _rows_to_csv_bytes(orders_rows))
    return buffer.getvalue()


def send_daily_csv_export():
    """
    Dagelijkse back-up van de volledige boeken- en orderlijst (CSV, gezipt).

    Is Dropbox ingesteld (zie dropbox_backup.py), dan komt de back-up in Dropbox te staan (map 'back-ups'; de laatste
    60 dagen blijven bewaard, daarna alleen die van de 1e en 15e) en bevat de e-mail alleen een melding met een link naar Dropbox, zonder bijlage. Zo staat de
    back-up buiten je mailbox en blijft hij niet afhankelijk van de maximale grootte van een e-mail.

    Lukt Dropbox niet, of is het niet ingesteld, dan gaat de back-up als bijlage mee als die klein genoeg is. Is hij te
    groot voor een e-mail, dan komt er geen bijlage maar een foutmelding, en wordt de job 'rood' in GitHub Actions: een
    back-up die ongemerkt niet aankomt is erger dan een melding.
    """
    import dropbox_backup

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM books")
            books_rows = cur.fetchall()
            cur.execute("SELECT * FROM orders")
            orders_rows = cur.fetchall()

        today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        zip_bytes = _build_backup_zip(today_str, books_rows, orders_rows)
        filename = f"boekbeheer_back-up_{today_str}.zip"
        size_mb = len(zip_bytes) / 1024 / 1024
        summary = f"{len(books_rows)} boeken, {len(orders_rows)} orders, {size_mb:.1f} MB"
        can_attach = len(zip_bytes) <= EMAIL_MAX_ATTACHMENT_BYTES
        subject = f"📚 Boekbeheersysteem: dagelijkse back-up ({today_str})"

        dropbox_error = None
        stored_in_dropbox = False
        pruned = 0
        prune_error = None
        if dropbox_backup.is_configured():
            try:
                token = dropbox_backup.get_access_token()
                dropbox_backup.upload_backup(token, filename, zip_bytes)
                stored_in_dropbox = True
                try:
                    pruned = dropbox_backup.prune_old_backups(token)
                except Exception as e:  # opruimen is bijzaak: de back-up zelf is gelukt
                    prune_error = str(e)
            except Exception as e:
                dropbox_error = str(e)

        if stored_in_dropbox:
            body = (
                f"De back-up van vandaag ({summary}) is opgeslagen in je Dropbox, in de map 'back-ups' van de app-map.\n"
                f"Bestand: {filename}\n\n"
                f"Open Dropbox (inloggen vereist): {dropbox_backup.DROPBOX_FOLDER_URL}\n\n"
                f"Er is bewust geen openbare link gemaakt, omdat de back-up persoonsgegevens van kopers bevat. "
                f"De laatste {dropbox_backup.KEEP_DAYS} dagen blijven alle back-ups bewaard, daarna alleen die van de 1e en de 15e"
                + (f"; {pruned} oudere {_n(pruned, 'back-up is', 'back-ups zijn')} vandaag opgeruimd." if pruned else ".")
            )
            if prune_error:
                body += f"\n\nLet op: oude back-ups opruimen lukte niet ({prune_error}). Dit is geen probleem voor de back-up van vandaag."
            notifications.send_email(subject=subject, body=body)
            detail = f"back-up in Dropbox gezet ({summary})"
            if pruned:
                detail += f", {pruned} oude opgeruimd"
            if prune_error:
                detail += f"; opruimen mislukt: {prune_error}"
            _log(conn, "export", "daily_csv", "ok", detail)
            conn.commit()
            return

        if dropbox_error is None:
            # Dropbox is niet ingesteld: de back-up gaat als bijlage mee, als dat past.
            if can_attach:
                notifications.send_email(
                    subject=subject,
                    body=(
                        f"Bijgevoegd: een volledige export van je boeken ({len(books_rows)}) en orders ({len(orders_rows)}) "
                        f"van vandaag, als eenvoudige back-up tegen dataverlies ({size_mb:.1f} MB, gezipt)."
                    ),
                    attachments=[(filename, zip_bytes, "application/zip")],
                )
                _log(conn, "export", "daily_csv", "ok", f"back-up gemaild ({summary})")
                conn.commit()
                return
            raise BackupProblem(
                f"De back-up ({summary}) is te groot voor een e-mail en Dropbox is niet ingesteld, dus er is vandaag GEEN "
                f"back-up gemaakt. Stel Dropbox in (zie dropbox_backup.py) om dit op te lossen."
            )

        # Dropbox is wel ingesteld maar mislukte.
        if can_attach:
            notifications.send_email(
                subject=f"⚠️ {subject} — Dropbox mislukt, back-up als bijlage",
                body=(
                    f"Het opslaan in Dropbox is mislukt: {dropbox_error}\n\n"
                    f"Om geen back-up te missen is die van vandaag ({summary}) alsnog als bijlage bijgevoegd."
                ),
                attachments=[(filename, zip_bytes, "application/zip")],
            )
            raise BackupProblem(f"Opslaan in Dropbox mislukt ({dropbox_error}); de back-up is als bijlage gemaild ({summary}).")
        raise BackupProblem(
            f"Opslaan in Dropbox mislukt ({dropbox_error}) en de back-up ({summary}) is te groot voor een e-mail, "
            f"dus er is vandaag GEEN back-up gemaakt."
        )
    except Exception as e:
        conn.rollback()
        _log(conn, "export", "daily_csv", "error", str(e))
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    result = full_sync()
    print(result)

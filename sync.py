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
from zoneinfo import ZoneInfo

import psycopg2
import requests

import api_client
import bol_client
import notifications
from db import get_connection, init_db


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _n(count, singular, plural):
    """Enkelvoud bij precies 1, anders meervoud (dus ook bij 0)."""
    return singular if count == 1 else plural


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
                        price, shipping_cost, shipping_category, listing_date, weblink, last_synced_at
                    ) VALUES (%(id)s, %(bookNumber)s, %(location)s, %(amount)s, %(category1)s, %(category2)s, %(category3)s,
                        %(language)s, %(author)s, %(title)s, %(publisher)s, %(publisher_name)s, %(publisher_address)s, %(publisher_contact)s,
                        %(ean)s, %(shortDescription)s, %(longDescription)s,
                        %(price)s, %(shippingCost)s, %(shippingCategory)s, %(date)s, %(weblink)s, %(last_synced_at)s)
                    ON CONFLICT (id) DO UPDATE SET
                        book_number=excluded.book_number, location=excluded.location, amount=excluded.amount,
                        category1=excluded.category1, category2=excluded.category2, category3=excluded.category3,
                        language=excluded.language, author=excluded.author, title=excluded.title,
                        publisher=excluded.publisher, publisher_name=excluded.publisher_name,
                        publisher_address=excluded.publisher_address, publisher_contact=excluded.publisher_contact,
                        ean=excluded.ean,
                        short_description=excluded.short_description, long_description=excluded.long_description,
                        price=excluded.price, shipping_cost=excluded.shipping_cost,
                        shipping_category=excluded.shipping_category, listing_date=excluded.listing_date,
                        weblink=excluded.weblink, last_synced_at=excluded.last_synced_at
                    WHERE books.pending_push = FALSE
                    """,
                    params,
                )
                count += 1
                seen_ids.append(book.get("id"))

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
        conn.commit()
        _log(conn, "pull", "books", "ok", f"{count} {_n(count, 'boek', 'boeken')} verwerkt, {marked_not_in_stock} op voorraad 0 gezet (niet meer bij Boekwinkeltjes)")
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
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM books WHERE pending_push = TRUE AND pending_create = FALSE AND push_enabled = TRUE"
            )
            rows = cur.fetchall()
            for row in rows:
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
                    "weblink": row["weblink"],
                }
                # Geen lege/None velden meesturen die de API mogelijk niet accepteert
                payload = {k: v for k, v in payload.items() if v is not None}
                try:
                    api_client.update_book(row["id"], payload)
                    cur.execute(
                        "UPDATE books SET pending_push = FALSE, last_synced_at = %(now)s WHERE id = %(id)s",
                        {"now": _now(), "id": row["id"]},
                    )
                    count += 1
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
    Titel en prijs komen daarom uit ons eigen boek met dezelfde EAN.
    """
    conn = get_connection()
    count = 0
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

                    cur.execute(
                        """
                        INSERT INTO orders (
                            id, order_date, status, platform, book_id, book_title, book_price, book_ean
                        ) VALUES (%(id)s, %(order_date)s, %(status)s, 'Bol', %(book_id)s, %(title)s, %(price)s, %(ean)s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = excluded.status, book_id = excluded.book_id,
                            book_title = excluded.book_title, book_price = excluded.book_price,
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
        _log(conn, "pull", "orders", "ok", f"{count} {_n(count, 'order', 'orders')} verwerkt", platform="Bol")
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
            weblink = %(weblink)s, last_synced_at = %(now)s
        WHERE id = %(old_id)s
        """,
        {
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


def _vacuum_full_tables(tables):
    """
    Herschrijft de opgegeven tabellen (VACUUM FULL), elk op een eigen verbinding, en geeft zo de lege ruimte
    terug aan de database. Er wordt niets gewist of gewijzigd. De namen komen uit een vaste lijst van de
    aanroeper. Een tabel wordt overgeslagen als de database na de herschrijving te dicht tegen de limiet
    zou komen, of als de grootte niet te meten is. Geeft (herschreven, overgeslagen, fouten) terug, met bij
    'overgeslagen' paren (tabel, reden) en bij 'fouten' teksten 'tabel: fout'.
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
            if not rows:
                skipped.append((table, "de grootte is niet te meten"))
                continue
            table_bytes = rows[0]["bytes"]
            if used + table_bytes > VACUUM_SAFETY_FRACTION * STORAGE_LIMIT_MB * 1024 * 1024:
                skipped.append((table, f"de database zit op {_mb(used)} en de herschrijving heeft tijdelijk {_mb(table_bytes)} extra nodig"))
                continue
            try:
                vacuum_conn = get_connection()
                try:
                    vacuum_conn.autocommit = True  # VACUUM kan niet binnen een transactie
                    with vacuum_conn.cursor() as cur:
                        cur.execute("SET lock_timeout = '30s'")
                        cur.execute(f"VACUUM FULL {table}")
                    done.append(table)
                finally:
                    vacuum_conn.close()
            except Exception as e:
                errors.append(f"{table}: {e}")
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

    say(f"== Ruimte terugwinnen: {'UITVOEREN' if real else 'ALLEEN RAPPORT'} (wist niets) ==")
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
        with conn.cursor() as cur:
            for offer in bol_offers:
                cur.execute(
                    """
                    INSERT INTO bol_offer_mapping (ean, offer_id, bol_stock, last_synced_at)
                    VALUES (%(ean)s, %(offer_id)s, %(stock)s, %(now)s)
                    ON CONFLICT (ean) DO UPDATE SET
                        offer_id = excluded.offer_id,
                        bol_stock = excluded.bol_stock,
                        last_synced_at = excluded.last_synced_at
                    """,
                    {"ean": offer["ean"], "offer_id": offer["offer_id"], "stock": offer["stock"], "now": _now()},
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
                SELECT b.id, b.amount, m.offer_id, m.bol_stock
                FROM books b
                JOIN bol_offer_mapping m ON m.ean = b.ean
                WHERE b.id > 0 AND COALESCE(b.queued, FALSE) = FALSE
                  AND b.ean IS NOT NULL AND b.ean != '' AND b.ean != '0'
                """
            )
            rows = cur.fetchall()

        bol_adjustments = 0
        errors = []
        for row in rows:
            local_amount = row["amount"] or 0
            bol_stock = row["bol_stock"] or 0
            if local_amount < bol_stock:
                try:
                    bol_client.update_offer_stock(row["offer_id"], local_amount)
                    bol_adjustments += 1
                except bol_client.BolAPIError as e:
                    errors.append(f"Bol {row['offer_id']}: {e}")

        detail = (
            f"{sold_count} {_n(sold_count, 'Bol-verkoop', 'Bol-verkopen')} verwerkt, "
            f"{bol_adjustments} {_n(bol_adjustments, 'Bol-aanbieding', 'Bol-aanbiedingen')} naar beneden bijgesteld"
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


def send_daily_csv_export():
    """
    Stuurt een dagelijkse e-mail met de volledige boeken- en orderlijst als
    CSV-bijlagen — een eenvoudige, periodieke back-up tegen dataverlies.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM books")
            books_rows = cur.fetchall()
            cur.execute("SELECT * FROM orders")
            orders_rows = cur.fetchall()

        today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        attachments = [
            (f"boeken_{today_str}.csv", _rows_to_csv_bytes(books_rows), "text/csv"),
            (f"orders_{today_str}.csv", _rows_to_csv_bytes(orders_rows), "text/csv"),
        ]
        notifications.send_email(
            subject=f"📚 Boekbeheersysteem: dagelijkse back-up ({today_str})",
            body=(
                f"Bijgevoegd: een volledige export van je boeken ({len(books_rows)}) en "
                f"orders ({len(orders_rows)}) van vandaag, als eenvoudige back-up tegen dataverlies."
            ),
            attachments=attachments,
        )
        _log(
            conn, "export", "daily_csv", "ok",
            f"CSV-back-up gemaild ({len(books_rows)} boeken, {len(orders_rows)} orders)",
        )
        conn.commit()
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

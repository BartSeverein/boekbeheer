"""
bw_live.py — Haalt de huidige stand van boeken en bestellingen RECHTSTREEKS bij Boekwinkeltjes op (alleen lezen),
zonder iets in onze eigen database te veranderen. Bedoeld voor de pagina 'Back-up bekijken'.
Zonder Streamlit en met een in te wisselen 'get'-functie, zodat het los te testen is.
"""

import pandas as pd
import requests

BASE_URLS = {
    "live": "https://www.boekwinkeltjes.nl/webservice/v1",
    "sandbox": "https://api-zandbak.boekwinkeltjes.nl/webservice/v1",
}


class BWLiveError(Exception):
    pass


def base_url_for(env):
    return BASE_URLS["live" if str(env or "").strip().lower() == "live" else "sandbox"]


def _extract_list(data, key_hint):
    payload = (data or {}).get("data", [])
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get(key_hint), list):
            return payload[key_hint]
        return [payload]
    return []


def _get_json(get, url, api_key, params):
    try:
        resp = get(url, headers={"api-key": api_key}, params=params, timeout=60)
    except requests.RequestException as e:
        raise BWLiveError(f"Geen verbinding met Boekwinkeltjes: {e}")
    if not resp.ok:
        raise BWLiveError(f"Boekwinkeltjes gaf een fout ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def fetch_books(api_key, base_url, get=requests.get, per_page=500, max_pages=200):
    items = []
    for page in range(1, max_pages + 1):
        batch = _extract_list(_get_json(get, f"{base_url}/books", api_key, {"page": page, "per_page": per_page}), "books")
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


def fetch_orders(api_key, base_url, get=requests.get, max_pages=500):
    items, seen = [], set()
    for page in range(1, max_pages + 1):
        batch = _extract_list(_get_json(get, f"{base_url}/orders", api_key, {"page": page}), "orders")
        new = [o for o in batch if o.get("id") not in seen]
        if not new:
            break
        seen.update(o.get("id") for o in new)
        items.extend(new)
    return items


BOOK_COLUMNS = ["id", "title", "author", "location", "amount", "price", "shipping_cost", "shipping_format", "ean",
                "category1", "category2", "category3", "short_description"]
ORDER_COLUMNS = ["id", "order_date", "status", "online_payment_status", "book_id", "book_title", "buyer_name",
                 "book_price", "book_shipping_cost"]


def books_to_frame(items):
    rows = [{"id": b.get("id"), "title": b.get("title"), "author": b.get("author"), "location": b.get("location"),
             "amount": b.get("amount"), "price": b.get("price"), "shipping_cost": b.get("shippingCost"),
             "shipping_format": b.get("shippingFormat"), "ean": b.get("ean"), "category1": b.get("category1"),
             "category2": b.get("category2"), "category3": b.get("category3"),
             "short_description": b.get("shortDescription")} for b in items]
    return pd.DataFrame(rows, columns=BOOK_COLUMNS)


def orders_to_frame(items):
    rows = []
    for o in items:
        book = o.get("book") or {}
        buyer = o.get("buyer") or {}
        rows.append({"id": o.get("id"), "order_date": o.get("date"), "status": o.get("status"),
                     "online_payment_status": o.get("onlinePaymentStatus"), "book_id": book.get("id"),
                     "book_title": book.get("title"), "buyer_name": buyer.get("name"),
                     "book_price": book.get("price"), "book_shipping_cost": book.get("shippingCost")})
    return pd.DataFrame(rows, columns=ORDER_COLUMNS)

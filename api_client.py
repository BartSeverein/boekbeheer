"""
api_client.py — Kleine wrapper rond de Boekwinkeltjes webservice (v1).

Documentatie: https://api-docs.boekwinkeltjes.nl/
"""

import os
import requests
import tenacity
from dotenv import load_dotenv

load_dotenv()

API_KEY_SANDBOX = os.environ.get("BOEKWINKELTJES_API_KEY_SANDBOX")
API_KEY_LIVE = os.environ.get("BOEKWINKELTJES_API_KEY_LIVE")
ENV = os.environ.get("BOEKWINKELTJES_ENV", "sandbox").strip().lower()

if ENV == "live":
    API_KEY = API_KEY_LIVE
    BASE_URL = "https://www.boekwinkeltjes.nl/webservice/v1"
else:
    API_KEY = API_KEY_SANDBOX
    BASE_URL = "https://api-zandbak.boekwinkeltjes.nl/webservice/v1"

if not API_KEY:
    raise RuntimeError(
        f"Geen API-sleutel gevonden voor omgeving '{ENV}'. Controleer "
        f"BOEKWINKELTJES_ENV en de bijbehorende BOEKWINKELTJES_API_KEY_{ENV.upper()} in .env."
    )

print(f"Boekwinkeltjes API-omgeving: {ENV} ({BASE_URL})")

HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}


class BoekwinkeltjesAPIError(Exception):
    pass


_UNSET = object()


def _is_retryable_response(response):
    """429 (te druk) en 503 (tijdelijk niet beschikbaar) komen in aanmerking voor een nieuwe poging."""
    return response.status_code in (429, 503)


# Exponentiële backoff bij 429/503: 2s, 4s, 8s, 16s (max 5 pogingen in totaal),
# zodat drukte bij Boekwinkeltjes soepel wordt opgevangen in plaats van meteen
# een foutmelding te geven.
@tenacity.retry(
    retry=tenacity.retry_if_result(_is_retryable_response),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
    stop=tenacity.stop_after_attempt(5),
)
def _send(method, url, **kwargs):
    return requests.request(method, url, **kwargs)


def _request(method, path, params=None, json=None, on_404=_UNSET):
    url = f"{BASE_URL}{path}"
    resp = _send(method, url, headers=HEADERS, params=params, json=json, timeout=30)

    if resp.status_code == 404 and on_404 is not _UNSET:
        return on_404

    if not resp.ok:
        raise BoekwinkeltjesAPIError(
            f"{method} {url} -> {resp.status_code}: {resp.text[:500]}"
        )

    if resp.text.strip() == "":
        return None
    return resp.json()


# ---------- Images ----------

def get_book_images(book_id):
    """Geeft de lijst met afbeeldingen voor één boek terug (leeg als er geen zijn, of als het boek niet meer bestaat)."""
    data = _request("GET", f"/books/{book_id}/images", on_404={"data": []})
    return _extract_list(data, "images")


def upload_book_image(book_id, image_bytes, content_type="image/jpeg"):
    """Stuurt een rauwe afbeelding (bytes) naar Boekwinkeltjes voor dit boek."""
    url = f"{BASE_URL}/books/{book_id}/images"
    headers = {"api-key": API_KEY, "Content-Type": content_type}
    resp = _send("POST", url, headers=headers, data=image_bytes, timeout=60)
    if not resp.ok:
        raise BoekwinkeltjesAPIError(f"POST {url} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.text.strip() else None

def get_settings():
    return _request("GET", "/settings")


def get_shipping_fees():
    return _request("GET", "/shipping-fees")


def set_shipping_fee_type(type_, fee=None, free=None):
    """type_: 'none', 'fixed', or 'variable'."""
    payload = {"type": type_}
    if fee is not None:
        payload["fee"] = fee
    if free is not None:
        payload["free"] = free
    return _request("PUT", "/shipping-fees", json=payload)


def create_shipping_fee_category(name, fee=None, free=None):
    payload = {"name": name}
    if fee is not None:
        payload["fee"] = fee
    if free is not None:
        payload["free"] = free
    return _request("POST", "/shipping-fees", json=payload)


# ---------- Books ----------

def get_books_page(page=1, per_page=500, **filters):
    params = {"page": page, "per_page": per_page, **filters}
    return _request("GET", "/books", params=params)


def _extract_list(data, key_hint):
    """
    Haalt de daadwerkelijke lijst met items uit een API-respons.

    De documentatie suggereert dat 'data' direct een lijst is, maar in de praktijk
    kan de respons genest zijn, bijvoorbeeld:
        {"status": 200, "data": {"books": [...], "paginator": {...}}}
    Deze functie handelt beide vormen af.
    """
    payload = data.get("data", [])
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if key_hint in payload and isinstance(payload[key_hint], list):
            return payload[key_hint]
        # Eén los object (geen lijst, geen bekende nesting) -> als één item behandelen.
        return [payload]
    return []


def iter_all_books(**filters):
    """Generator that yields every book, walking through pagination automatically."""
    page = 1
    per_page = filters.pop("per_page", 500)
    max_pages = 1000  # veiligheidsgrens tegen een oneindige lus
    while page <= max_pages:
        data = get_books_page(page=page, per_page=per_page, **filters)
        books = _extract_list(data, "books")
        if not books:
            break
        print(f"  pagina {page}: {len(books)} boek(en) opgehaald")
        yield from books
        if len(books) < per_page:
            # Minder dan een volle pagina terugkregen -> dit was de laatste pagina.
            break
        page += 1
    else:
        print(f"Waarschuwing: gestopt na {max_pages} pagina's (veiligheidsgrens).")


def get_book(book_id):
    return _request("GET", f"/books/{book_id}")


def create_book(payload):
    return _request("POST", "/books", json=payload)


def update_book(book_id, payload):
    return _request("PATCH", f"/books/{book_id}", json=payload)


def delete_book(book_id):
    return _request("DELETE", f"/books/{book_id}")


# ---------- Orders ----------

def get_orders_page(page=1, **filters):
    params = {"page": page, **filters}
    return _request("GET", "/orders", params=params)


def iter_all_orders(**filters):
    page = 1
    max_pages = 1000  # veiligheidsgrens tegen een oneindige lus
    seen_ids = set()
    while page <= max_pages:
        data = get_orders_page(page=page, **filters)
        orders = _extract_list(data, "orders")
        if not orders:
            break

        new_orders = [o for o in orders if o.get("id") not in seen_ids]
        if not new_orders:
            # Deze pagina bevat alleen orders die we al hadden -> geen echte
            # paginering meer, stop om een oneindige lus te voorkomen.
            break

        print(f"  pagina {page}: {len(new_orders)} nieuwe order(s) opgehaald")
        for o in new_orders:
            seen_ids.add(o.get("id"))
        yield from new_orders
        page += 1
    else:
        print(f"Waarschuwing: gestopt na {max_pages} pagina's (veiligheidsgrens).")


def get_order(order_id):
    return _request("GET", f"/orders/{order_id}")


def update_order(order_id, payload):
    return _request("PATCH", f"/orders/{order_id}", json=payload)

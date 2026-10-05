"""
bol_client.py — Wrapper rond Bol.com's Retailer API, specifiek voor het lezen en
bijwerken van je eigen voorraad (voor de anti-oversell-synchronisatie met
Boekwinkeltjes.nl). Leest BOL_CLIENT_ID/BOL_CLIENT_SECRET uit de omgeving
(GitHub Actions-secrets), net zoals api_client.py dat voor Boekwinkeltjes doet.

LET OP — nog te verifiëren na de eerste echte run:
De exacte kolomnamen in Bol's offer-exportbestand (CSV) zijn door Bol niet
letterlijk gedocumenteerd aangetroffen. _parse_offer_export_csv() zoekt daarom
op waarschijnlijke kolomnamen (o.a. 'ean', 'offerid', 'stock', 'correctedstock').
Controleer na de eerste run in de sync-log of dit voor jouw export klopt.
"""

import base64
import csv
import io
import os
import time

import requests
import tenacity

CLIENT_ID = os.environ.get("BOL_CLIENT_ID")
CLIENT_SECRET = os.environ.get("BOL_CLIENT_SECRET")

BASE_URL = "https://api.bol.com/retailer"
LOGIN_URL = "https://login.bol.com/token"

_token_cache = {"token": None, "expires_at": 0}


class BolAPIError(Exception):
    pass


def _error_detail(resp, limit=700):
    """
    Een leesbare samenvatting van een foutantwoord van Bol. Bij een validatiefout (400)
    staat de echte reden in 'violations' (welk veld, en waarom). In de ruwe tekst komt
    dat pas na de eerste ~300 tekens, dus daar afkappen verstopte precies wat je nodig
    hebt om het te kunnen oplossen. Het request-id blijft erbij, want daar heeft Bol's
    support iets aan. Is het antwoord geen JSON, dan blijft het de eerste 300 tekens.
    """
    raw = (resp.text or "")[:300]
    try:
        data = resp.json()
    except ValueError:
        return raw
    if not isinstance(data, dict):
        return raw
    parts = []
    violations = data.get("violations")
    if isinstance(violations, list) and violations:
        shown = [
            ", ".join(f"{key}: {value}" for key, value in violation.items())
            if isinstance(violation, dict) else str(violation)
            for violation in violations[:5]
        ]
        parts.append("; ".join(shown))
    else:
        parts.extend(str(data[key]) for key in ("detail", "title") if data.get(key))
    if not parts:
        return raw
    summary = " — ".join(parts)[:limit]
    if data.get("X-Request-ID"):
        summary += f" (request-id {data['X-Request-ID']})"
    return summary


def _is_retryable_response(response):
    """429 (te druk) en 503 (tijdelijk niet beschikbaar) komen in aanmerking voor een nieuwe poging."""
    return response.status_code in (429, 503)


# Exponentiële backoff bij 429/503: 2s, 4s, 8s, 16s (max 5 pogingen in totaal),
# zodat drukte bij Bol soepel wordt opgevangen in plaats van meteen een foutmelding
# te geven. Geldt voor ELK verzoek aan Bol (login, orders, voorraad, enz.).
@tenacity.retry(
    retry=tenacity.retry_if_result(_is_retryable_response),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
    stop=tenacity.stop_after_attempt(5),
)
def _send(method, url, **kwargs):
    return requests.request(method, url, **kwargs)


def _get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not CLIENT_ID or not CLIENT_SECRET:
        raise BolAPIError("BOL_CLIENT_ID/BOL_CLIENT_SECRET ontbreken in de omgeving.")

    credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = _send(
        "POST",
        LOGIN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    if not resp.ok:
        raise BolAPIError(f"Bol-login mislukt ({resp.status_code}): {_error_detail(resp)}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise BolAPIError(f"Bol-login gaf geen access_token terug: {resp.text[:300]}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + data.get("expires_in", 299) - 20  # kleine marge
    return token


def _headers(accept="application/vnd.retailer.v10+json"):
    return {"Accept": accept, "Authorization": f"Bearer {_get_access_token()}"}


def _request_offer_export():
    """Vraagt een export van alle eigen aanbiedingen aan. Geeft het process-status-id terug."""
    resp = _send(
        "POST",
        f"{BASE_URL}/offers/export",
        headers={**_headers(), "Content-Type": "application/vnd.retailer.v10+json"},
        json={"format": "CSV"},
        timeout=20,
    )
    if resp.status_code not in (200, 202):
        raise BolAPIError(f"Kon offer-export niet aanvragen ({resp.status_code}): {_error_detail(resp)}")
    data = resp.json()
    return data.get("processStatusId")


def _wait_for_process(process_status_id, max_wait_seconds=180, poll_interval=5):
    """Wacht tot een asynchroon Bol-verzoek is verwerkt. Geeft het entityId terug bij succes."""
    waited = 0
    while waited < max_wait_seconds:
        resp = _send(
            "GET", f"https://api.bol.com/shared/process-status/{process_status_id}", headers=_headers(), timeout=15
        )
        if not resp.ok:
            raise BolAPIError(f"Kon processtatus niet ophalen ({resp.status_code}): {_error_detail(resp)}")
        status = resp.json()
        state = status.get("status")
        if state == "SUCCESS":
            return status.get("entityId")
        if state == "FAILURE":
            raise BolAPIError(f"Bol-verzoek mislukt: {status}")
        time.sleep(poll_interval)
        waited += poll_interval
    raise BolAPIError("Bol-verzoek duurde te lang (timeout bij wachten op verwerking).")


def _parse_offer_export_csv(csv_text):
    """
    Zet de ruwe CSV-tekst van Bol's offer-export om naar een lijst dicts
    {'ean': ..., 'offer_id': ..., 'stock': ...}. Zoekt op waarschijnlijke
    kolomnamen omdat Bol de exacte namen niet expliciet documenteert — zie de
    kanttekening bovenaan dit bestand.
    """
    # Bol's CSV gebruikt vermoedelijk ';' als scheidingsteken (Europese conventie,
    # vergelijkbaar met andere Bol-exports); val terug op ',' als dat niets oplevert.
    for delimiter in (";", ","):
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) > 1:
            break
    else:
        return []

    lower_fieldnames = {f.lower(): f for f in fieldnames}

    def _find_column(*keywords):
        for key_lower, original in lower_fieldnames.items():
            if all(kw in key_lower for kw in keywords):
                return original
        return None

    ean_col = _find_column("ean")
    offer_id_col = _find_column("offerid") or _find_column("offer", "id")
    stock_col = (
        _find_column("correctedstock")
        or _find_column("stock", "amount")
        or _find_column("stock")
        or _find_column("amount")
    )
    operator_col = _find_column("economicoperatorid") or _find_column("economic", "operator")

    if not ean_col or not offer_id_col or not stock_col:
        raise BolAPIError(
            f"Kon de verwachte kolommen niet vinden in Bol's exportbestand. "
            f"Gevonden kolommen: {fieldnames}. Verwacht iets met 'ean', 'offerId' en 'stock'."
        )

    rows = []
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    for row in reader:
        ean = (row.get(ean_col) or "").strip()
        offer_id = (row.get(offer_id_col) or "").strip()
        stock_raw = (row.get(stock_col) or "").strip()
        if not ean or not offer_id:
            continue
        try:
            stock = int(float(stock_raw)) if stock_raw else 0
        except ValueError:
            stock = 0
        economic_operator_id = (row.get(operator_col) or "").strip() if operator_col else ""
        rows.append({"ean": ean, "offer_id": offer_id, "stock": stock, "economic_operator_id": economic_operator_id})
    return rows


def get_all_offers():
    """
    Vraagt een volledige export van al je eigen Bol-aanbiedingen aan en geeft
    een lijst terug van {'ean', 'offer_id', 'stock'} — dit is de manier om aan
    de EAN<->offerId-koppeling te komen (Bol laat aanbiedingen niet via EAN
    opzoeken, alleen via offerId).
    """
    process_status_id = _request_offer_export()
    report_id = _wait_for_process(process_status_id)
    resp = _send(
        "GET",
        f"{BASE_URL}/offers/export/{report_id}",
        headers=_headers(accept="application/vnd.retailer.v10+csv"),
        timeout=30,
    )
    if not resp.ok:
        raise BolAPIError(f"Kon exportbestand niet downloaden ({resp.status_code}): {_error_detail(resp)}")
    return _parse_offer_export_csv(resp.text)


def update_offer_stock(offer_id, amount, managed_by_retailer=True):
    """
    Werkt de voorraad van één eigen Bol-aanbieding bij. Dit gebeurt asynchroon bij
    Bol (een 202-antwoord bevestigt alleen dat het verzoek is ontvangen, niet dat
    het al verwerkt is).
    """
    resp = _send(
        "PUT",
        f"{BASE_URL}/offers/{offer_id}/stock",
        headers={**_headers(), "Content-Type": "application/vnd.retailer.v10+json"},
        json={"amount": amount, "managedByRetailer": managed_by_retailer},
        timeout=20,
    )
    if resp.status_code not in (200, 202):
        raise BolAPIError(f"Kon voorraad niet bijwerken bij Bol ({resp.status_code}): {_error_detail(resp)}")
    return resp.json()


ECONOMIC_OPERATOR_URL = "https://api.bol.com/economic-operators"
_economic_operator_cache = {}


def get_economic_operator_id_from_offers():
    """
    Leest de economicOperatorId af uit je bestaande Bol-aanbiedingen (via
    get_all_offers(), dat we al gebruiken voor de voorraadsync) — betrouwbaarder
    dan de aparte Economic Operators-API, waarvan het exacte eindpunt niet met
    zekerheid is vast te stellen. Geeft None terug als geen van je aanbiedingen
    een economicOperatorId bevat (bijv. omdat je nog geen enkele aanbieding hebt,
    of Bol dat veld nog niet in de export van jouw account heeft toegevoegd).
    """
    for offer in get_all_offers():
        operator_id = offer.get("economic_operator_id")
        if operator_id:
            return operator_id
    return None


def get_economic_operator_id(name):
    """
    Zoekt de economicOperatorId op die bij Bol hoort bij de marktdeelnemer met
    deze naam (bijv. 'Severein Services'), zodat je zelf geen technische code
    hoeft op te zoeken/in te vullen.

    ONZEKER: het exacte pad en de vorm van Bol's 'marktdeelnemers'-eindpunt zijn
    niet met een echte respons geverifieerd (deze API is relatief nieuw, voor de
    EU Digital Services Act). Deze functie is daarom defensief geschreven, en
    kan bijstelling nodig hebben na de eerste echte poging.
    """
    if name in _economic_operator_cache:
        return _economic_operator_cache[name]

    try:
        resp = _send(
            "GET",
            ECONOMIC_OPERATOR_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {_get_access_token()}"},
            timeout=15,
        )
    except requests.RequestException as e:
        raise BolAPIError(f"Kon marktdeelnemers niet ophalen: {e}")
    if not resp.ok:
        raise BolAPIError(f"Kon marktdeelnemers niet ophalen ({resp.status_code}): {_error_detail(resp)}")
    try:
        data = resp.json()
    except ValueError:
        raise BolAPIError("Onverwacht antwoord bij het ophalen van marktdeelnemers.")

    operators = data if isinstance(data, list) else (data.get("economicOperators") or data.get("items") or [])
    for op in operators:
        op_name = (op.get("name") or op.get("companyName") or "").strip().lower()
        if op_name == name.strip().lower():
            operator_id = op.get("economicOperatorId") or op.get("id")
            if operator_id:
                _economic_operator_cache[name] = operator_id
                return operator_id

    raise BolAPIError(
        f"Geen marktdeelnemer gevonden bij Bol met naam '{name}'. Controleer of de naam exact "
        f"overeenkomt met wat er in je Bol Seller Dashboard staat."
    )


def create_offer(ean, condition, price, stock_amount, reference, delivery_code, economic_operator_id, comment=None):
    """
    Maakt een nieuwe aanbieding aan bij Bol. Dit gebeurt asynchroon: deze functie
    geeft alleen het process-status-id terug — gebruik _wait_for_process() (via
    get_new_offer_id) om het echte, nieuwe offerId te krijgen zodra het is verwerkt.
    """
    condition_payload = dict(condition)
    if comment:
        condition_payload["comment"] = comment

    payload = {
        "ean": ean,
        "condition": condition_payload,
        "reference": (reference or "")[:100],
        "onHoldByRetailer": False,
        "economicOperatorId": economic_operator_id,
        "pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": price}]},
        "stock": {"amount": stock_amount, "managedByRetailer": True},
        "fulfilment": {"method": "FBR", "deliveryCode": delivery_code},
    }
    resp = _send(
        "POST",
        f"{BASE_URL}/offers",
        headers={**_headers(), "Content-Type": "application/vnd.retailer.v10+json"},
        json=payload,
        timeout=20,
    )
    if resp.status_code not in (200, 202):
        raise BolAPIError(f"Kon aanbieding niet aanmaken ({resp.status_code}): {_error_detail(resp)}")
    return resp.json().get("processStatusId")


def get_new_offer_id(process_status_id):
    """Wacht tot het aanmaken van een aanbieding is verwerkt en geeft het nieuwe offerId terug."""
    return _wait_for_process(process_status_id)


def get_orders(status="ALL", fulfilment_method="ALL", max_pages=20):
    """
    Haalt (met paginering) een lijst van eigen Bol-orders op via de Retailer API.
    Geeft de ruwe orders terug zoals Bol ze aanlevert (elke order kan meerdere
    orderItems bevatten — dat uitsplitsen gebeurt aan de aanroepende kant).
    """
    all_orders = []
    page = 1
    while page <= max_pages:
        resp = _send(
            "GET",
            f"{BASE_URL}/orders",
            headers=_headers(),
            params={"status": status, "fulfilment-method": fulfilment_method, "page": page},
            timeout=20,
        )
        if not resp.ok:
            raise BolAPIError(f"Kon orders niet ophalen ({resp.status_code}): {_error_detail(resp)}")
        data = resp.json()
        orders = data.get("orders") or []
        if not orders:
            break
        all_orders.extend(orders)
        if len(orders) < 50:
            break
        page += 1
    return all_orders

"""
bol_client.py — Wrapper rond Bol.com's Retailer API, specifiek voor het lezen en
bijwerken van je eigen voorraad (voor de anti-oversell-synchronisatie met
Boekwinkeltjes.nl). Leest BOL_CLIENT_ID/BOL_CLIENT_SECRET uit de omgeving
(GitHub Actions-secrets), net zoals api_client.py dat voor Boekwinkeltjes doet.

AANBIEDINGEN-API, TWEE VERSIES: voor het aanmaken van aanbiedingen, het ophalen van het
aanbod en het bijwerken van de voorraad staan de versies 10 en 11 van Bol's API naast
elkaar in dit bestand; OFFERS_API_VERSION hieronder bepaalt welke gebruikt wordt. Versie 10
vervalt op 1 februari 2027. Na de overstap kan de versie-10-code (de functies met '_v10'
erin, _request_offer_export, _wait_for_process en _parse_offer_export_csv) worden opgeruimd.

LET OP — nog te verifiëren na de eerste echte run (geldt voor versie 10):
De exacte kolomnamen in Bol's offer-exportbestand (CSV) zijn door Bol niet
letterlijk gedocumenteerd aangetroffen. _parse_offer_export_csv() zoekt daarom
op waarschijnlijke kolomnamen (o.a. 'ean', 'offerid', 'stock', 'correctedstock').
Controleer na de eerste run in de sync-log of dit voor jouw export klopt.
"""

import base64
import csv
import io
import itertools
import os
import time

import requests
import tenacity

CLIENT_ID = os.environ.get("BOL_CLIENT_ID")
CLIENT_SECRET = os.environ.get("BOL_CLIENT_SECRET")

BASE_URL = "https://api.bol.com/retailer"
LOGIN_URL = "https://login.bol.com/token"

# Welke versie van Bol's aanbiedingen-API ("Offers") de app gebruikt voor het aanmaken van
# aanbiedingen, het ophalen van het aanbod en het bijwerken van de voorraad. Versie 10 wordt
# op 1 februari 2027 door Bol uitgezet; versie 11 is de opvolger en werkt anders (direct
# antwoord i.p.v. wachten op een processtatus, een lijst met pagina's i.p.v. een CSV-export,
# 'state' i.p.v. 'name' bij de conditie, 'schedule' i.p.v. een leveringscode).
# Zet dit op 11 zodra de controle (workflow 'Test Bol aanbiedingen v11') er goed uitziet.
# Terug naar 10 kan op dezelfde manier, tot 1 februari 2027.
# Orders, concurrerende aanbiedingen enz. zijn aparte onderdelen van Bol's API en blijven op v10.
OFFERS_API_VERSION = 10
V11_MEDIA_TYPE = "application/vnd.retailer.v11+json"

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


def _get_all_offers_v10():
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


def _update_offer_stock_v10(offer_id, amount, managed_by_retailer=True):
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
    if _offers_version() == 11:
        # Pagina voor pagina, en stoppen zodra er één is gevonden (niet eerst alles ophalen).
        for offer in _iter_offers_v11():
            operator_id = str(offer.get("economicOperatorId") or "").strip()
            if operator_id:
                return operator_id
        return None
    for offer in _get_all_offers_v10():
        operator_id = offer.get("economic_operator_id")
        if operator_id:
            return operator_id
    return None


def get_delivery_profile_id_from_offers(sample_size=300):
    """
    Het leverbelofte-profiel (profileId) dat je bestaande aanbiedingen gebruiken, zodat
    nieuwe aanbiedingen hetzelfde profiel krijgen. Kijkt naar de eerste paar honderd
    aanbiedingen met je eigen leverbelofte en neemt het profiel dat daar het meest
    voorkomt. Alleen relevant voor versie 11; bij versie 10 (die een leveringscode
    gebruikt) of als er geen profiel te vinden is: None, dan beslist Bol zelf.
    """
    if _offers_version() != 11:
        return None
    counts = {}
    for offer in itertools.islice(_iter_offers_v11(), sample_size):
        fulfilment = offer.get("fulfilment") or {}
        profile_id = fulfilment.get("profileId")
        if fulfilment.get("schedule") == "MY_DELIVERY_PROMISE" and profile_id:
            counts[profile_id] = counts.get(profile_id, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda pid: (counts[pid], pid))


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


def _offers_version():
    """De ingestelde versie van de aanbiedingen-API, gecontroleerd."""
    if OFFERS_API_VERSION not in (10, 11):
        raise BolAPIError(f"Onbekende OFFERS_API_VERSION: {OFFERS_API_VERSION!r} (kies 10 of 11).")
    return OFFERS_API_VERSION


def _condition_payload(condition, comment, version):
    """
    Zet onze interne conditie om naar wat de gekozen API-versie verwacht. Intern:
      {'category': 'NEW'}  of  {'category': 'SECONDHAND', 'state': 'AS_NEW'|'GOOD'|'MODERATE'|'REASONABLE'}
    Versie 11: {'category': 'NEW'} resp. {'category': 'SECONDHAND', 'state': ..., 'comment': ...}
    Versie 10: altijd met 'name' erbij ('NEW' of de staat), anders wijst Bol het af.
    Een toelichting hoort alleen bij tweedehands; bij nieuw laat Bol die niet toe.
    """
    category = (condition or {}).get("category")
    state = (condition or {}).get("state")
    if category not in ("NEW", "SECONDHAND"):
        raise BolAPIError(f"Onbekende conditie: {condition!r}")
    if category == "SECONDHAND" and state not in ("AS_NEW", "GOOD", "MODERATE", "REASONABLE"):
        raise BolAPIError(f"Onbekende staat bij een tweedehands conditie: {condition!r}")
    if version == 11:
        payload = {"category": category}
        if category == "SECONDHAND":
            payload["state"] = state
    else:
        payload = {"name": state if category == "SECONDHAND" else "NEW", "category": category}
    if comment and category == "SECONDHAND":
        payload["comment"] = comment
    return payload


def _create_offer_v10(ean, condition, price, stock_amount, reference, delivery_code, economic_operator_id, comment=None):
    """
    Versie 10: een nieuwe aanbieding aanmaken gebeurt asynchroon. We krijgen een
    process-status-id terug en wachten daarna tot Bol het heeft verwerkt, om het
    echte, nieuwe offerId terug te kunnen geven.
    """
    payload = {
        "ean": ean,
        "condition": _condition_payload(condition, comment, 10),
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
    return get_new_offer_id(resp.json().get("processStatusId"))


def get_new_offer_id(process_status_id):
    """Wacht tot het aanmaken van een aanbieding is verwerkt en geeft het nieuwe offerId terug (alleen versie 10)."""
    return _wait_for_process(process_status_id)


def _create_offer_v11(ean, condition, price, stock_amount, reference, economic_operator_id, comment=None,
                      delivery_profile_id=None):
    """
    Versie 11: Bol verwerkt dit direct en geeft meteen het nieuwe offerId terug (geen
    processtatus meer). Landen laten we weg, dan geldt de landinstelling van je Bol-account
    (net als bij versie 10). Zonder verantwoordelijke partij in de EU komt een aanbieding
    nooit online, dus dan liever hier een duidelijke fout dan een aanbieding die offline blijft.
    """
    if not economic_operator_id:
        raise BolAPIError(
            "Geen economicOperatorId bekend: een aanbieding zonder verantwoordelijke partij in de EU komt bij Bol nooit online."
        )
    fulfilment = {"method": "FBR", "schedule": "MY_DELIVERY_PROMISE"}
    if delivery_profile_id:
        # Je bestaande aanbiedingen verwijzen naar een eigen leverbelofte-profiel; nieuwe doen dat
        # dan ook, zodat ze niet per ongeluk een ander profiel krijgen.
        fulfilment["profileId"] = delivery_profile_id
    payload = {
        "ean": ean,
        "condition": _condition_payload(condition, comment, 11),
        "economicOperatorId": economic_operator_id,
        "onHoldByRetailer": False,
        "pricing": {"bundlePrices": [{"quantity": 1, "unitPrice": price}]},
        "fulfilment": fulfilment,
        "stock": {"amount": stock_amount, "managedByRetailer": True},
    }
    if reference:
        payload["reference"] = reference[:100]
    resp = _send(
        "POST",
        f"{BASE_URL}/offers",
        headers={**_headers(accept=V11_MEDIA_TYPE), "Content-Type": V11_MEDIA_TYPE},
        json=payload,
        timeout=20,
    )
    if resp.status_code not in (200, 201, 202):
        raise BolAPIError(f"Kon aanbieding niet aanmaken ({resp.status_code}): {_error_detail(resp)}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    offer_id = data.get("offerId") if isinstance(data, dict) else None
    if not offer_id:
        raise BolAPIError(f"Bol bevestigde het aanmaken, maar gaf geen offerId terug: {(resp.text or '')[:300]}")
    return offer_id


def create_offer(ean, condition, price, stock_amount, reference, delivery_code, economic_operator_id, comment=None,
                 delivery_profile_id=None):
    """
    Maakt een nieuwe aanbieding aan bij Bol en geeft het nieuwe offerId terug. Welke
    versie van Bol's API daarvoor wordt gebruikt, staat bovenin dit bestand
    (OFFERS_API_VERSION). 'delivery_code' geldt alleen voor versie 10; versie 11 gebruikt
    altijd je eigen leverbelofte ('MY_DELIVERY_PROMISE'), eventueel met het profiel
    'delivery_profile_id' (zie get_delivery_profile_id_from_offers).
    """
    if _offers_version() == 11:
        return _create_offer_v11(
            ean, condition, price, stock_amount, reference, economic_operator_id, comment, delivery_profile_id
        )
    return _create_offer_v10(ean, condition, price, stock_amount, reference, delivery_code, economic_operator_id, comment)


def _update_offer_stock_v11(offer_id, amount, managed_by_retailer=True):
    """Versie 11: de voorraad bijwerken met een PATCH. Bol verwerkt dit direct (geen processtatus)."""
    resp = _send(
        "PATCH",
        f"{BASE_URL}/offers/{offer_id}",
        headers={**_headers(accept=V11_MEDIA_TYPE), "Content-Type": V11_MEDIA_TYPE},
        json={"stock": {"amount": amount, "managedByRetailer": managed_by_retailer}},
        timeout=20,
    )
    if resp.status_code not in (200, 202, 204):
        raise BolAPIError(f"Kon voorraad niet bijwerken bij Bol ({resp.status_code}): {_error_detail(resp)}")
    body = (resp.text or "").strip()
    if not body:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def update_offer_stock(offer_id, amount, managed_by_retailer=True):
    """Werkt de voorraad van één eigen Bol-aanbieding bij, via de ingestelde versie van Bol's API."""
    if _offers_version() == 11:
        return _update_offer_stock_v11(offer_id, amount, managed_by_retailer)
    return _update_offer_stock_v10(offer_id, amount, managed_by_retailer)


def _extract_offer_list(data):
    """
    Haalt de lijst aanbiedingen uit een antwoord van 'aanbiedingen ophalen' (versie 11).
    De sleutel waaronder Bol die lijst zet, is niet in de beschrijving terug te vinden;
    we verwachten 'offers' en zoeken anders zelf een lijst waarin 'offerId' voorkomt.
    Een vorm die we niet herkennen geeft een duidelijke fout, in plaats van stilzwijgend
    'geen aanbiedingen' te melden (dan zou de voorraadsync denken dat er niets te doen is).
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        offers = data.get("offers")
        if isinstance(offers, list):
            return offers
        for value in data.values():
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and any("offerId" in v for v in value):
                return value
        if not data or set(data) <= {"page", "links"}:
            return []  # een geldige, lege pagina
    shape = sorted(data) if isinstance(data, dict) else type(data).__name__
    raise BolAPIError(f"Onverwachte vorm van Bol's lijst met aanbiedingen (velden: {shape}).")


def _iter_offers_v11(page_size=100, max_pages=500):
    """
    Geeft alle eigen aanbiedingen één voor één terug, pagina voor pagina (Bol geeft er
    maximaal 100 per pagina). Een volgende pagina vraag je op met de 'nextCursor' uit het
    vorige antwoord; is die leeg, dan ben je bij de laatste. Meer pagina's dan verwacht, of
    een cursor die zich herhaalt, geeft een fout in plaats van stilzwijgend af te kappen.
    """
    cursor = None
    for _ in range(max_pages):
        params = {"page-size": page_size}
        if cursor:
            params["cursor"] = cursor
        resp = _send("GET", f"{BASE_URL}/offers", headers=_headers(accept=V11_MEDIA_TYPE), params=params, timeout=30)
        if not resp.ok:
            raise BolAPIError(f"Kon aanbiedingen niet ophalen ({resp.status_code}): {_error_detail(resp)}")
        try:
            data = resp.json()
        except ValueError:
            raise BolAPIError(f"Onleesbaar antwoord bij het ophalen van aanbiedingen: {(resp.text or '')[:300]}")
        yield from _extract_offer_list(data)
        next_cursor = (data.get("page") or {}).get("nextCursor") if isinstance(data, dict) else None
        if not next_cursor:
            return
        if next_cursor == cursor:
            raise BolAPIError("Bol gaf twee keer dezelfde 'nextCursor' terug; afgebroken om niet eindeloos door te lopen.")
        cursor = next_cursor
    raise BolAPIError(f"Meer dan {max_pages} pagina's met aanbiedingen; afgebroken.")


def _offer_to_row(offer):
    """
    Zet één aanbieding uit versie 11 om naar dezelfde vorm als de CSV-export van versie 10
    {'ean', 'offer_id', 'stock', 'economic_operator_id'}. 'stock' is het getal dat Bol
    laat ZIEN: 'correctedStock' (voorraad min openstaande bestellingen) als dat er is,
    anders gewoon het ingestelde aantal — precies wat de CSV-kolom 'correctedStock' ook
    was. Volgens Bol laten ze 'correctedStock' weg als er niets openstaat.
    """
    ean = str(offer.get("ean") or "").strip()
    offer_id = str(offer.get("offerId") or "").strip()
    if not ean or not offer_id:
        return None
    stock_info = offer.get("stock") or {}
    value = stock_info.get("correctedStock")
    if value is None:
        value = stock_info.get("amount")
    try:
        stock = int(float(value)) if value is not None else 0
    except (TypeError, ValueError):
        stock = 0
    return {
        "ean": ean,
        "offer_id": offer_id,
        "stock": stock,
        "economic_operator_id": str(offer.get("economicOperatorId") or "").strip(),
    }


def _get_all_offers_v11_from(offers):
    """Zet een al opgehaalde lijst om naar regels en ontdubbelt op offerId (de laatste keer geldt)."""
    by_offer_id = {}
    for offer in offers:
        row = _offer_to_row(offer)
        if row:
            by_offer_id.pop(row["offer_id"], None)
            by_offer_id[row["offer_id"]] = row
    return list(by_offer_id.values())


def _get_all_offers_v11():
    """
    Versie 11: alle eigen aanbiedingen via de lijst met pagina's, in dezelfde vorm als de
    CSV-export. Bol zet de lijst op volgorde van laatste wijziging. Wordt een aanbieding
    gewijzigd terwijl we pagina's aan het ophalen zijn (bijv. door een verkoop), dan schuift
    hij naar het einde en komt hij twee keer langs; dan geldt de laatste, meest actuele keer.
    """
    return _get_all_offers_v11_from(_iter_offers_v11())


def get_all_offers():
    """
    Geeft al je eigen Bol-aanbiedingen als lijst van {'ean', 'offer_id', 'stock',
    'economic_operator_id'} — dit is de manier om aan de EAN<->offerId-koppeling te komen.
    Versie 10 doet dat via een CSV-export, versie 11 via een lijst met pagina's.
    """
    if _offers_version() == 11:
        return _get_all_offers_v11()
    return _get_all_offers_v10()


def check_offers_v11(write_noop=False):
    """
    Veilige controle van Bol's aanbiedingen-API versie 11, bedoeld om vóór de overstap
    te draaien (workflow 'Test Bol aanbiedingen v11'). Geeft regels tekst terug.

    Alleen lezen, behalve als write_noop=True: dan wordt bij één bestaande aanbieding de
    voorraad "bijgewerkt" naar precies het aantal dat er al staat (dus zonder iets te
    veranderen), alleen om te bewijzen dat het bijwerken werkt.

    Het laat zien wat Bol teruggeeft, hoe de app het leest, en hoe dat zich verhoudt tot wat
    de app nu via versie 10 ziet — zo zie je vooraf of de overstap hetzelfde beeld geeft.
    """
    lines = []

    def say(text=""):
        lines.append(text)

    say("== 1. Eerste pagina ophalen (alleen lezen) ==")
    resp = _send(
        "GET", f"{BASE_URL}/offers", headers=_headers(accept=V11_MEDIA_TYPE), params={"page-size": 5}, timeout=30
    )
    say(f"HTTP-status: {resp.status_code}")
    if not resp.ok:
        say(f"Mislukt: {_error_detail(resp)}")
        return lines
    try:
        data = resp.json()
    except ValueError:
        say(f"Mislukt: het antwoord is geen JSON: {(resp.text or '')[:300]}")
        return lines
    say(f"Velden in het antwoord: {sorted(data) if isinstance(data, dict) else type(data).__name__}")
    offers = _extract_offer_list(data)
    say(f"Aanbiedingen op deze pagina: {len(offers)}  |  paginering: {data.get('page') if isinstance(data, dict) else None}")
    if offers:
        first = offers[0]
        say(f"Velden van een aanbieding: {sorted(first)}")
        say(f"  ean={first.get('ean')}  conditie={first.get('condition')}")
        say(f"  voorraad={first.get('stock')}")
        say(f"  levering={first.get('fulfilment')}")
        say(f"  verantwoordelijke partij aanwezig: {'ja' if first.get('economicOperatorId') else 'NEE'}")
        say(f"  zoals de app het leest: {_offer_to_row(first)}")

    say()
    say("== 2. Alle aanbiedingen ophalen (alleen lezen) ==")
    all_offers = list(_iter_offers_v11())
    rows11 = _get_all_offers_v11_from(all_offers)
    say(f"Totaal: {len(all_offers)} aanbiedingen, waarvan {len(rows11)} bruikbaar en uniek (met ean en offerId)")
    with_operator = sum(1 for r in rows11 if r["economic_operator_id"])
    say(f"Met verantwoordelijke partij (economicOperatorId): {with_operator} van {len(rows11)}")
    methods, categories, schedules, profiles = {}, {}, {}, {}
    for o in all_offers:
        fulfilment = o.get("fulfilment") or {}
        methods[fulfilment.get("method") or "onbekend"] = methods.get(fulfilment.get("method") or "onbekend", 0) + 1
        schedules[fulfilment.get("schedule") or "-"] = schedules.get(fulfilment.get("schedule") or "-", 0) + 1
        profile = fulfilment.get("profileId")
        if profile:
            profiles[profile] = profiles.get(profile, 0) + 1
        category = (o.get("condition") or {}).get("category") or "onbekend"
        categories[category] = categories.get(category, 0) + 1
    say(f"Levering: {methods}  |  leverbelofte: {schedules}  |  conditie: {categories}")
    say(f"Leverbelofte-profielen op je aanbiedingen: {profiles or 'geen'}")
    say(f"Dit profiel krijgen nieuwe aanbiedingen: {get_delivery_profile_id_from_offers() if _offers_version() == 11 else '(alleen bij versie 11)'}")
    offers_per_ean = {}
    for r in rows11:
        offers_per_ean.setdefault(r["ean"], []).append(r["offer_id"])
    duplicates = {ean: ids for ean, ids in offers_per_ean.items() if len(ids) > 1}
    if duplicates:
        by_id = {str(o.get("offerId")): o for o in all_offers}
        say(f"Meer dan één aanbieding voor hetzelfde ean: {len(duplicates)} ean's (de app koppelt er per ean maar één):")
        for ean, ids in sorted(duplicates.items())[:15]:
            parts = []
            for offer_id in sorted(ids):
                o = by_id.get(offer_id, {})
                stock = o.get("stock") or {}
                parts.append(f"{offer_id[:8]} ({(o.get('condition') or {}).get('state') or (o.get('condition') or {}).get('category')}, voorraad {stock.get('correctedStock', stock.get('amount'))})")
            say(f"   {ean}: " + "  |  ".join(parts))

    say()
    say("== 3. Vergelijken met wat de app nu ziet (versie 10, CSV-export) ==")
    try:
        rows10 = _get_all_offers_v10()
    except BolAPIError as e:
        say(f"Vergelijking niet mogelijk, versie 10 gaf een fout: {e}")
    else:
        # Per aanbieding vergelijken (offerId is uniek); per ean zou dubbele ean's als ruis geven.
        map10 = {r["offer_id"]: r for r in rows10}
        map11 = {r["offer_id"]: r for r in rows11}
        only10 = sorted(set(map10) - set(map11))
        only11 = sorted(set(map11) - set(map10))
        both = sorted(set(map10) & set(map11))
        diff_stock = [oid for oid in both if map10[oid]["stock"] != map11[oid]["stock"]]
        diff_ean = [oid for oid in both if map10[oid]["ean"] != map11[oid]["ean"]]
        say(f"Versie 10: {len(map10)} aanbiedingen  |  versie 11: {len(map11)} aanbiedingen  |  in beide: {len(both)}")
        say(f"Alleen in versie 10: {len(only10)} {only10[:5]}")
        say(f"Alleen in versie 11: {len(only11)} {only11[:5]}")
        say(f"Zelfde aanbieding, ander ean: {len(diff_ean)} {diff_ean[:5]}")
        say(f"Zelfde aanbieding, ander voorraadgetal: {len(diff_stock)}")
        for offer_id in diff_stock[:5]:
            say(f"   {offer_id[:8]} (ean {map10[offer_id]['ean']}): versie 10 = {map10[offer_id]['stock']}, versie 11 = {map11[offer_id]['stock']}")
        say("(Een klein verschil in voorraad kan komen doordat Bol 'correctedStock' met enige vertraging bijwerkt.)")

    if write_noop:
        say()
        say("== 4. Voorraad bijwerken zonder iets te veranderen ==")
        candidate = next(
            (o for o in all_offers
             if (o.get("fulfilment") or {}).get("method") == "FBR" and isinstance((o.get("stock") or {}).get("amount"), int)),
            None,
        )
        if not candidate:
            say("Geen aanbieding gevonden die je zelf verzendt (FBR) met een voorraadaantal; overgeslagen.")
        else:
            amount = candidate["stock"]["amount"]
            managed = candidate["stock"].get("managedByRetailer", True)
            say(f"Aanbieding met ean {candidate.get('ean')}: voorraad blijft {amount} (zelf beheerd: {managed}).")
            try:
                _update_offer_stock_v11(candidate["offerId"], amount, managed)
            except BolAPIError as e:
                say(f"Mislukt: {e}")
            else:
                check = _send(
                    "GET", f"{BASE_URL}/offers/{candidate['offerId']}", headers=_headers(accept=V11_MEDIA_TYPE), timeout=20
                )
                after = (check.json().get("stock") or {}).get("amount") if check.ok else None
                say(f"Gelukt. Voorraad daarna: {after} ({'ongewijzigd, zoals bedoeld' if after == amount else 'LET OP: anders dan verwacht'}).")
    else:
        say()
        say("(Het bijwerken van voorraad is niet getest; dat kan door bij de workflow de optie 'voorraad_test' aan te zetten.)")
    return lines


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

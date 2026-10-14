"""
webshop/common.py — kleine hulpfuncties die generator, deploy en orderimport delen.

De webshop is een statische site (gemaakt door build.py) plus een paar PHP-scripts. Alles hier is bewust
zonder externe bibliotheken, zodat het ook in een kale GitHub Actions-run werkt.
"""
import os
import re

# ---------- Zoektekst normaliseren ----------
# Dezelfde tabel wordt door build.py als PHP-bestand (norm.php) meegeleverd, zodat de zoekterm in search.php
# precies zo wordt genormaliseerd als de zoektekst in de catalogus. Pas je hier iets aan: opnieuw bouwen.
NORM_MAP = {}
for _chars, _to in (
    ("àáâãäåāăą", "a"), ("çćč", "c"), ("ďđ", "d"), ("èéêëēėęě", "e"), ("ìíîïīį", "i"), ("łĺľ", "l"),
    ("ñńň", "n"), ("òóôõöøōő", "o"), ("ŕř", "r"), ("śšş", "s"), ("ťţ", "t"), ("ùúûüūůűų", "u"),
    ("ýÿ", "y"), ("źżž", "z"),
):
    for _c in _chars:
        NORM_MAP[_c] = _to
NORM_MAP.update({"œ": "oe", "æ": "ae", "ß": "ss", "ĳ": "ij"})
# Ook de hoofdletter-varianten, zodat PHP (zonder intl/mbstring) hetzelfde resultaat geeft als Python.
for _c, _to in list(NORM_MAP.items()):
    if _c.upper() != _c and len(_c.upper()) == 1:
        NORM_MAP[_c.upper()] = _to


def norm(text):
    """Kleine letters, zonder accenten, alleen a-z, 0-9 en enkele spaties. Identiek aan norm() in lib.php."""
    text = "".join(NORM_MAP.get(ch, ch) for ch in (text or ""))
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def slugify(text, max_len=60):
    slug = norm(text).replace(" ", "-")
    slug = slug[:max_len].rstrip("-")
    return slug or "boek"


def eur(cents):
    """Nederlandse schrijfwijze met een spatie na het eurotekens: € 9,45 (vanaf 10.000: € 12.345,00)."""
    cents = int(cents)
    euros, rest = divmod(abs(cents), 100)
    euros_text = f"{euros:,}".replace(",", ".") if euros >= 10000 else str(euros)
    return f"{'-' if cents < 0 else ''}€ {euros_text},{rest:02d}"


def to_cents(value):
    if value is None:
        return None
    return int(round(float(value) * 100))


# ---------- Instellingen uit de omgeving ----------

def shop_config():
    """
    Instellingen van de webshop, uit omgevingsvariabelen (in GitHub Actions: secrets/variabelen).
    Niets hiervan is geheim behalve WEBSHOP_MOLLIE_API_KEY en WEBSHOP_DATA_KEY; die komen alleen in config.php op de server.
    """
    base = (os.environ.get("WEBSHOP_BASE_URL") or "https://severein.nl").rstrip("/")
    return {
        "base_url": base,
        "name": os.environ.get("WEBSHOP_NAME") or "Severein",
        "tagline": os.environ.get("WEBSHOP_TAGLINE") or "Tweedehands boeken, zorgvuldig verpakt en snel verzonden.",
        "contact_email": os.environ.get("WEBSHOP_CONTACT_EMAIL") or "",
        "default_shipping_cents": int(os.environ.get("WEBSHOP_DEFAULT_SHIPPING_CENTS") or 395),
        "extra_per_additional_book_cents": int(os.environ.get("WEBSHOP_EXTRA_PER_BOOK_CENTS") or 0),
        "free_shipping_from_cents": int(os.environ.get("WEBSHOP_FREE_SHIPPING_FROM_CENTS") or 0),
        "countries": [c.strip().upper() for c in (os.environ.get("WEBSHOP_COUNTRIES") or "NL").split(",") if c.strip()],
        "data_key": os.environ.get("WEBSHOP_DATA_KEY") or "",
        "mollie_key": os.environ.get("WEBSHOP_MOLLIE_API_KEY") or "",
        "mollie_api_base": os.environ.get("WEBSHOP_MOLLIE_API_BASE") or "https://api.mollie.com/v2",
        "webhook_base": (os.environ.get("WEBSHOP_WEBHOOK_BASE") or base).rstrip("/"),
        "owner_name": os.environ.get("WEBSHOP_OWNER_NAME") or "",
        "address": os.environ.get("WEBSHOP_ADDRESS") or "",
        "kvk": os.environ.get("WEBSHOP_KVK") or "",
        "vat": os.environ.get("WEBSHOP_VAT") or "",
        "phone": os.environ.get("WEBSHOP_PHONE") or "",
    }

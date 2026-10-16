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


def eur(cents, lang="nl"):
    """Bedrag per taal: nl € 9,45 · en € 9.45 · de/fr 9,45 € (vanaf 10.000: duizendtallen met punt/komma/spatie)."""
    cents = int(cents)
    euros, rest = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    if lang == "en":
        e = f"{euros:,}" if euros >= 10000 else str(euros)
        return f"{sign}€ {e}.{rest:02d}"
    if lang in ("de", "fr"):
        e = (f"{euros:,}".replace(",", "\u202f" if lang == "fr" else ".")) if euros >= 10000 else str(euros)
        return f"{sign}{e},{rest:02d}\u00a0€"
    e = f"{euros:,}".replace(",", ".") if euros >= 10000 else str(euros)
    return f"{sign}€ {e},{rest:02d}"


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
    default_ship = int(os.environ.get("WEBSHOP_DEFAULT_SHIPPING_CENTS") or 395)

    def money(text):
        return int(round(float(text.strip().replace(",", ".").replace("€", "")) * 100))
    # Verzendtarieven per land: WEBSHOP_SHIPPING_RATES="NL=3.95/6.95,DE=7.50/12.00,FR=8.00/13.00,OTHER=10.00/16.00"
    # (klein/groot pakje). OTHER geldt voor elk ander land uit WEBSHOP_COUNTRIES.
    rates = {}
    for part in (os.environ.get("WEBSHOP_SHIPPING_RATES") or "").replace(";", ",").split(","):
        if "=" not in part:
            continue
        code, value = part.split("=", 1)
        code = code.strip().upper()
        code = "OTHER" if code in ("OTHER", "OVERIG", "*") else code
        try:
            small, _, large = value.partition("/")
            rates[code] = (money(small), money(large or small))
        except ValueError:
            pass
    # Oude instelling WEBSHOP_COUNTRY_SURCHARGE (toeslag op het NL-tarief) blijft werken als er geen eigen tarief is.
    if "NL" not in rates:
        rates["NL"] = (default_ship, max(default_ship, 695))
    for part in (os.environ.get("WEBSHOP_COUNTRY_SURCHARGE") or "").replace(";", ",").split(","):
        if "=" in part:
            code, value = part.split("=", 1)
            code = code.strip().upper()
            try:
                add = money(value)
            except ValueError:
                continue
            if code not in rates:
                rates[code] = (rates["NL"][0] + add, rates["NL"][1] + add)
    countries = [c.strip().upper() for c in (os.environ.get("WEBSHOP_COUNTRIES") or "NL").split(",") if c.strip()]
    # Een land zonder eigen tarief en zonder OTHER doen we niet aan.
    skipped = [c for c in countries if c != "NL" and c not in rates and "OTHER" not in rates]
    countries = [c for c in countries if c == "NL" or c in rates or "OTHER" in rates]
    if "NL" not in countries:
        countries.insert(0, "NL")
    from .i18n import LANGS
    langs = [l.strip().lower() for l in (os.environ.get("WEBSHOP_LANGS") or "nl,en,de,fr").split(",") if l.strip().lower() in LANGS]
    if "nl" not in langs:
        langs.insert(0, "nl")
    return {
        "base_url": base,
        "name": os.environ.get("WEBSHOP_NAME") or "Severein",
        "tagline": os.environ.get("WEBSHOP_TAGLINE") or "Eindeloze variatie in buitengewone boeken: van de mooiste beelden tot de beste verhalen!",
        "year": __import__("datetime").date.today().year,
        "copyright_name": os.environ.get("WEBSHOP_COPYRIGHT_NAME") or "Severein Services",
        "tagline_custom": bool(os.environ.get("WEBSHOP_TAGLINE")),
        "contact_email": os.environ.get("WEBSHOP_CONTACT_EMAIL") or "",
        "default_shipping_cents": default_ship,
        # Een boek geldt als klein pakje als zijn eigen verzendprijs (uit de database) hierop of eronder zit, anders groot.
        "small_max_cents": int(os.environ.get("WEBSHOP_SMALL_MAX_CENTS") or 400),
        "shipping_rates": {c: list(v) for c, v in rates.items()},
        "extra_per_additional_book_cents": int(os.environ.get("WEBSHOP_EXTRA_PER_BOOK_CENTS") or 0),
        "free_shipping_from_cents": int(os.environ.get("WEBSHOP_FREE_SHIPPING_FROM_CENTS") or 0),
        "countries": countries or ["NL"],
        "countries_skipped": skipped,
        "langs": langs,
        "deepl_key": os.environ.get("WEBSHOP_DEEPL_KEY") or "",
        "deepl_max_chars": int(os.environ.get("WEBSHOP_DEEPL_MAX_CHARS") or 100000),
        # Harde grens over alle runs samen (alles wat ooit is vertaald, opgeteld); houd dit onder je DeepL-tegoed.
        "deepl_total_chars": int(os.environ.get("WEBSHOP_DEEPL_TOTAL_CHARS") or 900000),
        # Bijzonderheden (korte tekst per boek) worden standaard wel vertaald; de lange beschrijving alleen met WEBSHOP_TRANSLATE_BOOKS=ja.
        "translate_short": (os.environ.get("WEBSHOP_TRANSLATE_SHORT") or "ja").strip().lower() not in ("0", "nee", "no", "false", "uit"),
        "translate_books": (os.environ.get("WEBSHOP_TRANSLATE_BOOKS") or "").strip().lower() in ("1", "ja", "yes", "true", "aan"),
        "data_key": os.environ.get("WEBSHOP_DATA_KEY") or "",
        "mollie_key": os.environ.get("WEBSHOP_MOLLIE_API_KEY") or "",
        "mollie_api_base": os.environ.get("WEBSHOP_MOLLIE_API_BASE") or "https://api.mollie.com/v2",
        "webhook_base": (os.environ.get("WEBSHOP_WEBHOOK_BASE") or base).rstrip("/"),
        "owner_name": os.environ.get("WEBSHOP_OWNER_NAME") or "",
        "address": os.environ.get("WEBSHOP_ADDRESS") or "",
        "kvk": os.environ.get("WEBSHOP_KVK") or "",
        "vat": os.environ.get("WEBSHOP_VAT") or "",
        "iban": os.environ.get("WEBSHOP_IBAN") or "",
        "smtp_host": (os.environ.get("WEBSHOP_SMTP_HOST") or "").strip(),
        "smtp_port": int(os.environ.get("WEBSHOP_SMTP_PORT") or 0) or 587,
        "smtp_user": (os.environ.get("WEBSHOP_SMTP_USER") or "").strip(),
        "smtp_pass": os.environ.get("WEBSHOP_SMTP_PASS") or "",
        "smtp_from": (os.environ.get("WEBSHOP_SMTP_FROM") or "").strip(),
        "phone": os.environ.get("WEBSHOP_PHONE") or "",
        "intime_key": (os.environ.get("WEBSHOP_INTIME_KEY") or "").strip(),
    }

"""
webshop/build.py — maakt van de boekendatabase een complete statische webshop in een map (standaard webshop_dist/).

Wat eruit komt:
  - HTML voor home, categorieën (met paginering), een pagina per boek, zoeken, winkelwagen, afrekenen, bedankt, 404
  - afbeeldingen als WebP (alleen de nieuwe; reeds geüploade worden herkend aan hun bestandsnaam)
  - d-<sleutel>/catalog.sqlite: de boekenlijst voor search.php en checkout.php
  - PHP-scripts, config.php (met geheimen uit de omgeving), norm.php, .htaccess, robots.txt, sitemap.xml

Twee modi:
  - "full"    : alles hierboven.
  - "catalog" : alleen catalog.sqlite (voorraad/prijzen actueel houden, snel en goedkoop), beperkt tot boeken
                waarvan de pagina al online staat.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import i18n
from .authors import build_authors
from .common import NORM_MAP, eur, norm, shop_config, slugify
from .pages import Site

HERE = Path(__file__).parent
PER_PAGE = 48
MAX_IMAGES = 3
DETAIL_PX = 640
THUMB_PX = 240
LANG_CODES = {
    "nederlands": "nl", "engels": "en", "duits": "de", "frans": "fr", "spaans": "es", "italiaans": "it",
    "latijn": "la", "fries": "fy", "portugees": "pt", "zweeds": "sv", "deens": "da", "noors": "no",
}


# ---------------------------------------------------------------- gegevens laden

def _categories_module():
    path = HERE.parent / "dashboard" / "categories.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("shop_categories", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def category_labels():
    """({c1_value: label}, {c1_value: {c2_value: label}}) uit het dashboard (zelfde indeling als bij Boekwinkeltjes)."""
    mod = _categories_module()
    if not mod:
        return {}, {}
    c1 = {v: l for v, l in getattr(mod, "CATEGORY1_OPTIONS", [])}
    c2 = {k: {v: l for v, l in opts} for k, opts in getattr(mod, "CATEGORY2_OPTIONS", {}).items()}
    return c1, c2


def load_from_postgres(conn):
    """
    Leest alle boeken die te koop zijn plus hun afbeeldingen (zonder de bytes) en de al verwerkte webshop-orders.
    Geeft (books, imported_order_ids, fetch_image) terug.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, author, COALESCE(NULLIF(publisher_name, ''), publisher) AS publisher, ean, language,
                   short_description, long_description, price, shipping_cost, amount, category1, category2, listing_date, location
            FROM books
            WHERE COALESCE(amount, 0) > 0 AND COALESCE(pending_create, FALSE) = FALSE AND COALESCE(queued, FALSE) = FALSE
              AND price IS NOT NULL AND price > 0 AND COALESCE(title, '') <> ''
            """
        )
        rows = cur.fetchall()
        # Boeken die de laatste 90 dagen verkocht zijn (op welk platform ook) houden hun pagina, met "verkocht" erop,
        # zodat het adres bij Google blijft bestaan en bezoekers vergelijkbare boeken zien.
        try:
            cur.execute(
                """
                SELECT id, title, author, COALESCE(NULLIF(publisher_name, ''), publisher) AS publisher, ean, language,
                       short_description, long_description, price, shipping_cost, amount, category1, category2, listing_date, location
                FROM books
                WHERE COALESCE(amount, 0) <= 0 AND COALESCE(pending_create, FALSE) = FALSE AND COALESCE(queued, FALSE) = FALSE
                  AND price IS NOT NULL AND price > 0 AND COALESCE(title, '') <> ''
                  AND id IN (SELECT book_id FROM orders WHERE book_id IS NOT NULL AND order_date::timestamp >= now() - interval '90 days')
                """
            )
            sold_rows = cur.fetchall()
        except Exception:   # verkochte boeken zijn een extraatje: een fout hier mag de bouw nooit stoppen
            conn.rollback()
            sold_rows = []
        cur.execute(
            """
            SELECT book_id, image_id, position, url_large, url_medium, (image_large_data IS NOT NULL) AS has_data
            FROM book_images WHERE image_id <> -1 ORDER BY book_id, position, image_id
            """
        )
        images = {}
        for r in cur.fetchall():
            images.setdefault(r["book_id"], []).append(r)
        cur.execute("SELECT id FROM orders WHERE platform = 'Webshop' AND COALESCE(stock_adjusted, FALSE) = TRUE")
        imported = {(-int(r["id"]) - 3_000_000_000) // 1000 for r in cur.fetchall()}
    books = []
    for r, is_sold in [(r, False) for r in rows] + [(r, True) for r in sold_rows]:
        books.append({"sold": is_sold, **{
            "id": int(r["id"]), "title": r["title"], "author": r["author"], "publisher": r["publisher"], "isbn": r["ean"],
            "language": r["language"], "short": r["short_description"], "long": r["long_description"],
            "price": float(r["price"]), "shipping": float(r["shipping_cost"]) if r["shipping_cost"] is not None else None,
            "amount": int(r["amount"]), "category1": r["category1"], "category2": r["category2"],
            "listing_date": r["listing_date"],
            "images": [{"image_id": i["image_id"], "url_large": i["url_large"], "url_medium": i["url_medium"],
                        "has_data": bool(i["has_data"])} for i in images.get(int(r["id"]), [])],
        }})

    import threading
    lock = threading.Lock()

    def fetch_image(book, img):
        if img.get("has_data"):
            with lock, conn.cursor() as cur:
                cur.execute("SELECT image_large_data FROM book_images WHERE book_id = %s AND image_id = %s",
                            (book["id"], img["image_id"]))
                row = cur.fetchone()
            if row and row["image_large_data"]:
                return bytes(row["image_large_data"])
        for key in ("url_large", "url_medium"):
            url = img.get(key)
            if not url:
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "boekbeheer-webshop-build"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read(15_000_000)
            except Exception:
                continue
        return None

    return books, imported, fetch_image


# ---------------------------------------------------------------- afbeeldingen

def _hash8(book_id, img):
    return hashlib.sha1(f"{book_id}|{img.get('image_id')}|{img.get('url_large') or ''}".encode()).hexdigest()[:8]


def _index_known(known_images):
    """'i/12-0-abcd1234-d-480x640.webp' -> {(12, 0, 'abcd1234'): {'d': (pad, w, h), 't': (pad, w, h)}}"""
    index = {}
    for p in known_images:
        name = str(p).strip("/").split("/")[-1]
        if not name.endswith(".webp"):
            continue
        try:
            book, n, h8, kind, dims = name[:-5].split("-")
            w, h = dims.split("x")
            index.setdefault((int(book), int(n), h8), {})[kind] = ("/i/" + name, int(w), int(h))
        except ValueError:
            continue
    return index


def _make_webp(raw, max_px, quality):
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((max_px, max_px), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue(), im.width, im.height


def prepare_images(books, out_dir, known_images, fetch_image, process, log, deadline=None):
    """Zet b['images'] en b['thumb'] per boek. Geeft (aantal nieuw, aantal mislukt, set van alle gebruikte paden)."""
    index = _index_known(known_images)
    used, new, failed = set(), [0], [0]
    (out_dir / "i").mkdir(parents=True, exist_ok=True)

    def work(b):
        result = []
        for n, img in enumerate(b["images"][:MAX_IMAGES]):
            key = (b["id"], n, _hash8(b["id"], img))
            have = index.get(key, {})
            d = have.get("d")
            t = have.get("t") if n == 0 else True
            if d and t:
                result.append((d, have.get("t") if n == 0 else None, []))
                continue
            if not process or (deadline and time.time() > deadline):
                continue
            raw = fetch_image(b, img)
            if not raw:
                failed[0] += 1
                continue
            try:
                files = []
                data, w, h = _make_webp(raw, DETAIL_PX, 72)
                d = (f"/i/{b['id']}-{n}-{key[2]}-d-{w}x{h}.webp", w, h)
                files.append((d[0], data))
                t = None
                if n == 0:
                    data, w, h = _make_webp(raw, THUMB_PX, 70)
                    t = (f"/i/{b['id']}-{n}-{key[2]}-t-{w}x{h}.webp", w, h)
                    files.append((t[0], data))
                for rel, blob in files:
                    (out_dir / rel.lstrip("/")).write_bytes(blob)
                result.append((d, t, [f[0] for f in files]))
            except Exception:
                failed[0] += 1
        return b, result

    with ThreadPoolExecutor(max_workers=6) as pool:
        for b, result in pool.map(work, books):
            imgs = []
            for d, t, created in result:
                used.add(d[0])
                if t:
                    used.add(t[0])
                new[0] += len(created)
                imgs.append({"detail": d, "thumb": t})
            b["imgs"] = imgs
            b["thumb"] = imgs[0]["thumb"] if imgs and imgs[0]["thumb"] else None
            b["images_final"] = [
                {"detail": i["detail"], "thumb": i["thumb"] or (i["detail"][0], 120, max(1, round(120 * i["detail"][2] / i["detail"][1])))}
                for i in imgs
            ]
            # Voor extra foto's is een kleine weergave van de detailfoto genoeg (geen aparte thumbnail-bestanden).
            for i in b["images_final"][1:]:
                i["thumb"] = (i["detail"][0], 120, max(1, round(120 * i["detail"][2] / i["detail"][1])))
    return new[0], failed[0], used


# ---------------------------------------------------------------- hoofdfunctie

def _prepare_books(raw_books, cfg, c1_labels, c2_labels):
    books = []
    for r in raw_books:
        price = int(round(r["price"] * 100))
        own = int(round(r["shipping"] * 100)) if r.get("shipping") and r["shipping"] > 0 else cfg["default_shipping_cents"]
        big = 1 if own > cfg["small_max_cents"] else 0   # groot pakje als de eigen verzendprijs van het boek boven de grens ligt
        ship = cfg["shipping_rates"]["NL"][big]           # getoonde prijs = tarief voor Nederland
        title = " ".join((r.get("title") or "").split())
        if not title or price <= 0 or (r["amount"] <= 0 and not r.get("sold")):
            continue
        c1 = r.get("category1") or "overig diverse"
        c1_label = c1_labels.get(c1, c1)
        c2 = r.get("category2") or None
        c2_label = c2_labels.get(c1, {}).get(c2, c2) if c2 else None
        isbn = "".join(ch for ch in (r.get("isbn") or "") if ch.isdigit() or ch in "xX")
        lang = (r.get("language") or "").strip()
        b = dict(r)
        b.update({
            "title": title, "author": " ".join((r.get("author") or "").split()), "price_cents": price, "ship_cents": ship, "big": big,
            "isbn": isbn, "language": lang, "lang_code": LANG_CODES.get(lang.lower()) or (lang.lower() if len(lang) in (2, 3) and lang.isalpha() else None),
            "path": f"/boek/{slugify(title)}-{r['id']}/",
            "c1": c1, "c1_label": c1_label, "c1_slug": slugify(c1_label), "c2": c2, "c2_label": c2_label,
            "c2_slug": slugify(c2_label) if c2_label else None,
            "category_label": c1_label + (f" › {c2_label}" if c2_label else ""),
        })
        books.append(b)
    # Nieuwste eerst: op id (hoogste bovenaan), zoals ook het dashboard doet. Het id verandert na aanmaak nooit meer;
    # de listing_date ontbreekt juist bij net ingevoerde boeken en kan bij bewerken verschuiven.
    books.sort(key=lambda x: x["id"], reverse=True)
    for i, b in enumerate(books):
        b["seq"] = i
    return books


def _write(out_dir, rel, content):
    p = out_dir / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


def _page_dir(path):
    return path.strip("/") + "/index.html" if path != "/" else "index.html"


_FILL_TEXT = {  # per taal: (KvK, btw, telefoon, gratis, extra per boek, tarieven-zin, klein, groot, overige landen)
    "nl": ("KvK: {}", "btw-nummer: {}", "Telefoon: {}", "Vanaf {} bestelwaarde is de verzending binnen Nederland gratis. ",
           ", plus {} per extra boek", "Verzendkosten per bestelling ({klein} / {groot}): {lijst}. ", "klein pakje", "groot pakje", "overige landen"),
    "en": ("Chamber of Commerce (KvK): {}", "VAT no.: {}", "Phone: {}", "From {} order value, shipping within the Netherlands is free. ",
           ", plus {} per additional book", "Shipping costs per order ({klein} / {groot}): {lijst}. ", "small parcel", "large parcel", "other countries"),
    "de": ("Handelsregister (KvK): {}", "USt-IdNr.: {}", "Telefon: {}", "Ab {} Bestellwert ist der Versand innerhalb der Niederlande kostenlos. ",
           ", zuzüglich {} je weiterem Buch", "Versandkosten pro Bestellung ({klein} / {groot}): {lijst}. ", "kleines Paket", "großes Paket", "übrige Länder"),
    "fr": ("Chambre de commerce (KvK) : {}", "N° de TVA : {}", "Téléphone : {}", "À partir de {} d'achats, l'envoi aux Pays-Bas est gratuit. ",
           ", plus {} par livre supplémentaire", "Frais de port par commande ({klein} / {groot}) : {lijst}. ", "petit colis", "grand colis", "autres pays"),
}


def _fill(template, cfg, lang="nl"):
    kvk_t, vat_t, tel_t, free_t, extra_t, abroad_t, small_t, large_t, other_t = _FILL_TEXT[lang]

    def line(text):
        return text + "<br>" if text else ""
    free = free_t.format(eur(cfg["free_shipping_from_cents"], lang)) if cfg["free_shipping_from_cents"] else ""
    extra = extra_t.format(eur(cfg["extra_per_additional_book_cents"], lang)) if cfg["extra_per_additional_book_cents"] else ""
    rates = cfg["shipping_rates"]
    items = []
    for c in cfg["countries"]:
        if c in rates:
            items.append(f"{i18n.country_name(c, lang)}: {eur(rates[c][0], lang)} / {eur(rates[c][1], lang)}")
    if "OTHER" in rates and any(c not in rates for c in cfg["countries"]):
        others = ", ".join(i18n.country_name(c, lang) for c in cfg["countries"] if c not in rates)
        items.append(f"{other_t} ({others}): {eur(rates['OTHER'][0], lang)} / {eur(rates['OTHER'][1], lang)}")
    abroad = abroad_t.replace("{klein}", small_t).replace("{groot}", large_t).replace("{lijst}", "; ".join(items)) if items else ""
    values = {
        "naam": cfg["name"] + "<br>", "eigenaar": line(cfg["owner_name"] or cfg["name"]),   # eenmanszaak: alleen de naam van de eigenaar
        "iban": line("IBAN: " + cfg["iban"]) if cfg["iban"] else "", "adres": line(cfg["address"]),
        "kvk": line(kvk_t.format(cfg["kvk"])) if cfg["kvk"] else "", "btw": line(vat_t.format(cfg["vat"])) if cfg["vat"] else "",
        "telefoon": line(tel_t.format(cfg["phone"])) if cfg["phone"] else "", "email": cfg["contact_email"] or "info@example.invalid",
        "landen": ", ".join(i18n.country_name(c, lang) for c in cfg["countries"]), "toeslag": extra, "gratis": free,
        "buitenland": abroad,
    }
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def _content_template(slug, lang):
    name = f"{slug}.html" if lang == "nl" else f"{slug}.{lang}.html"
    path = HERE / "content" / name
    if not path.exists():
        path = HERE / "content" / f"{slug}.html"
    return path.read_text(encoding="utf-8")


def _book_loc(b, lang, tr, cat_label):
    """De teksten van een boek in `lang`; ontbreekt de vertaling, dan blijft het Nederlands (fb = terugval)."""
    if lang == "nl":
        cat = cat_label(b["c1_label"], "nl") + ((" › " + cat_label(b["c2_label"], "nl")) if b["c2_label"] else "")
        return {"short": b.get("short"), "long": b.get("long"), "short_fb": False, "long_fb": False,
                "category_label": cat, "language": i18n.book_language(b.get("language"), "nl")}
    out = {"short_fb": False, "long_fb": False}
    for key in ("short", "long"):
        src = b.get(key) or ""
        got = tr.get(src, lang) if (tr and src.strip()) else None
        out[key] = got or src
        out[key + "_fb"] = bool(src.strip()) and got is None
    cat = cat_label(b["c1_label"], lang)
    if b["c2_label"]:
        cat += " › " + cat_label(b["c2_label"], lang)
    out["category_label"] = cat
    out["language"] = i18n.book_language(b.get("language"), lang)
    return out


def norm_php():
    items = ",".join(f"'{k}'=>'{v}'" for k, v in sorted(NORM_MAP.items()) if k != "'" and k != "\\")
    return ("<?php\n// Gegenereerd door build.py — niet handmatig wijzigen.\nfunction norm($s){\n"
            f"  static $m=[{items}];\n"
            "  $s=strtr((string)$s,$m);\n  $s=strtolower($s);\n"
            "  return trim(preg_replace('/[^a-z0-9]+/',' ',$s));\n}\n")


def config_php(cfg):
    data = {
        "base_url": cfg["base_url"], "webhook_base": cfg["webhook_base"], "name": cfg["name"], "owner": cfg["owner_name"], "email": cfg["contact_email"],
        "data_dir": "d-" + cfg["data_key"], "mollie_key": cfg["mollie_key"], "mollie_api_base": cfg["mollie_api_base"],
        "default_shipping_cents": cfg["default_shipping_cents"], "extra_cents": cfg["extra_per_additional_book_cents"],
        "free_from_cents": cfg["free_shipping_from_cents"], "countries": cfg["countries"], "reserve_seconds": 1800,
        "rates": cfg["shipping_rates"],
        "smtp": {"host": cfg["smtp_host"], "port": cfg["smtp_port"], "user": cfg["smtp_user"], "pass": cfg["smtp_pass"], "from": cfg["smtp_from"]},
        "intime_key": cfg["intime_key"],
    }
    text = json.dumps(data, ensure_ascii=False)
    return "<?php\n// Gegenereerd door build.py (bevat geheimen: niet delen).\nreturn json_decode(<<<'JSON'\n" + text + "\nJSON\n, true);\n"


def i18n_php(cfg):
    text = json.dumps(i18n.php_data(cfg["langs"], cfg), ensure_ascii=False)
    return "<?php\n// Gegenereerd door build.py uit webshop/i18n.py — niet handmatig wijzigen.\nreturn json_decode(<<<'JSON'\n" + text + "\nJSON\n, true);\n"


def _merchant_feed(books, cfg, book_tr, cat_label):
    """Tab-gescheiden bestand voor Google Merchant Center (alleen boeken met foto; Google eist die)."""
    cols = ["id", "title", "description", "link", "image_link", "additional_image_link", "availability", "price", "condition",
            "gtin", "identifier_exists", "author", "product_type", "google_product_category", "shipping"]

    def cell(v):
        return re.sub(r"[\t\r\n]+", " ", str(v if v is not None else "")).strip()
    base = cfg["base_url"]
    lines = ["\t".join(cols)]
    for b in books:
        imgs = b.get("images") or []
        if not imgs:
            continue
        loc = _book_loc(b, "nl", book_tr, cat_label)
        desc = re.sub(r"\s+", " ", (loc.get("long") or loc.get("short") or b["title"])).strip()[:4900]
        isbn = re.sub(r"\D", "", b.get("isbn") or "")
        gtin = isbn if len(isbn) == 13 else ""
        row = {
            "id": b["id"], "title": b["title"][:150], "description": desc, "link": base + b["path"],
            "image_link": base + imgs[0]["detail"][0],
            "additional_image_link": ",".join(base + i["detail"][0] for i in imgs[1:10]),
            "availability": "in_stock", "price": f"{b['price_cents'] / 100:.2f} EUR", "condition": "used",
            "gtin": gtin, "identifier_exists": "yes" if gtin else "no", "author": b.get("author") or "",
            "product_type": b["category_label"].replace(" › ", " > "), "google_product_category": "784",
            "shipping": f"NL:::{b['ship_cents'] / 100:.2f} EUR",
        }
        lines.append("\t".join(cell(row[c]) for c in cols))
    return "\n".join(lines) + "\n"


def _load_cat_intros():
    path = HERE / "content" / "categorie_teksten.json"
    try:
        return {k.strip().lower(): v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if v}
    except Exception:
        return {}


CAT_INTROS = _load_cat_intros()


def _load_cat_refs():
    """Handmatige verwijzingen tussen categorieën (content/categorie_verwijzingen.json): 'Kijk ook in …'."""
    path = HERE / "content" / "categorie_verwijzingen.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k.strip().lower(): [x.strip().lower() for x in v] for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        return {}


CAT_REFS = _load_cat_refs()

HTACCESS = """\
# Gegenereerd door build.py
DirectoryIndex index.html
ErrorDocument 404 /404.html
Options -Indexes
AddDefaultCharset UTF-8
<FilesMatch "^\\.">
  Require all denied
</FilesMatch>
<FilesMatch "^(config|lib|norm|i18n)\\.php$">
  Require all denied
</FilesMatch>
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/css application/javascript application/json application/xml image/svg+xml
</IfModule>
<IfModule mod_expires.c>
  ExpiresActive On
  ExpiresByType image/webp "access plus 1 year"
  ExpiresByType text/css "access plus 1 year"
  ExpiresByType application/javascript "access plus 1 year"
  ExpiresByType text/html "access plus 10 minutes"
</IfModule>
<IfModule mod_headers.c>
  Header always set X-Content-Type-Options "nosniff"
  Header always set Referrer-Policy "strict-origin-when-cross-origin"
  Header always set X-Frame-Options "SAMEORIGIN"
  <FilesMatch "\\.(webp|css|js)$">
    Header set Cache-Control "public, max-age=31536000, immutable"
  </FilesMatch>
</IfModule>
"""

DATA_HTACCESS = """\
Require all denied
<IfModule mod_authz_compat.c>
  Deny from all
</IfModule>
"""


def write_catalog(path, books, imported_ids, cfg, built_at):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author TEXT, isbn TEXT, price_cents INTEGER,
                ship_cents INTEGER, big INTEGER, amount INTEGER, path TEXT, img TEXT, iw INTEGER, ih INTEGER, hay TEXT, tnorm TEXT, seq INTEGER, location TEXT);
            CREATE INDEX books_seq ON books(seq);
            CREATE TABLE imported_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE discount_codes (code TEXT PRIMARY KEY, kind TEXT NOT NULL, pct INTEGER NOT NULL DEFAULT 0, until TEXT NOT NULL DEFAULT '');
            """
        )
        con.executemany("INSERT INTO discount_codes VALUES (?,?,?,?)",
                        [(d["code"], d["kind"], d["pct"], d["until"]) for d in (cfg.get("discount_codes") or [])])
        rows = []
        for b in books:
            th = b.get("thumb")
            hay = " " + norm(" ".join(filter(None, [b["title"], b["author"], b.get("publisher"), b.get("c1_label"), b.get("c2_label")]))) + " " + (b["isbn"] or "").lower() + " "
            rows.append((b["id"], b["title"], b["author"], b["isbn"], b["price_cents"], b["ship_cents"], b["big"], b["amount"], b["path"],
                         th[0] if th else None, th[1] if th else None, th[2] if th else None, hay, norm(b["title"]), b["seq"], b.get("location")))
        con.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO imported_orders VALUES (?)", [(int(i),) for i in sorted(imported_ids)])
        con.executemany("INSERT INTO meta VALUES (?,?)", [("built_at", built_at), ("count", str(len(rows)))])
        con.commit()
    finally:
        con.close()


def build_site(raw_books, out_dir, cfg=None, *, mode="full", imported_order_ids=(), known_images=(), known_pages=None,
               fetch_image=None, process_images=True, image_deadline=None, translator=None, log=print):
    cfg = cfg or shop_config()
    if not cfg["data_key"] or len(cfg["data_key"]) < 12 or not all(c.isalnum() for c in cfg["data_key"]):
        raise RuntimeError("WEBSHOP_DATA_KEY ontbreekt of is te kort (minimaal 12 letters/cijfers, geen andere tekens).")
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    t0 = time.time()
    c1_labels, c2_labels = category_labels()
    books = _prepare_books(raw_books, cfg, c1_labels, c2_labels)
    sold = [b for b in books if b.get("sold")]      # recent verkocht: eigen pagina met "verkocht", verder nergens in de winkel
    books = [b for b in books if not b.get("sold")]
    for i, b in enumerate(books):
        b["seq"] = i
    built_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    data_dir = "d-" + cfg["data_key"]
    stats = {"mode": mode, "books": len(books)}
    if cfg.get("countries_skipped"):
        log("Let op: geen verzending naar " + ", ".join(cfg["countries_skipped"]) +
            " (zet in WEBSHOP_COUNTRY_SURCHARGE een toeslag per land, bijvoorbeeld BE=3.00,DE=4.00,FR=5.00).")

    if mode == "catalog":
        pages = set(known_pages or [])
        books = [b for b in books if b["path"] in pages]
        index = _index_known(known_images)
        for b in books:
            b["thumb"] = None
            for n, img in enumerate(b["images"][:1]):
                have = index.get((b["id"], n, _hash8(b["id"], img)), {})
                b["thumb"] = have.get("t")
        write_catalog(out_dir / data_dir / "catalog.sqlite", books, imported_order_ids, cfg, built_at)
        stats.update(books=len(books), keep=[], seconds=round(time.time() - t0, 1))
        return stats

    # ---- afbeeldingen
    new_imgs, failed_imgs, used_images = prepare_images(books + sold, out_dir, known_images, fetch_image, process_images, log, image_deadline)
    for b in books + sold:
        b["images"] = b.pop("images_final")
    log(f"Afbeeldingen: {new_imgs} nieuw gemaakt, {failed_imgs} mislukt, {len(used_images)} in gebruik.")

    # ---- statische bestanden
    css_src = (HERE / "assets" / "s.css").read_bytes()
    langs = cfg["langs"]
    foreign = [l for l in langs if l != "nl"]
    js_text = (HERE / "assets" / "c.js").read_text(encoding="utf-8")
    if "/*I18N*/{}" not in js_text:
        raise RuntimeError("c.js mist de plaats voor de teksten (/*I18N*/{}).")
    js_src = js_text.replace("/*I18N*/{}", json.dumps({l: i18n.js_strings(l) for l in langs}, ensure_ascii=False,
                                                       separators=(",", ":"))).encode("utf-8")
    css_name = f"s.{hashlib.sha1(css_src).hexdigest()[:8]}.css"
    js_name = f"c.{hashlib.sha1(js_src).hexdigest()[:8]}.js"
    _write(out_dir, css_name, css_src)
    _write(out_dir, js_name, js_src)
    # favicon (Google toont dit icoon naast de zoekresultaten): vaste adressen in de hoofdmap
    icon_file = HERE / "assets" / "icon.png"
    if icon_file.exists():
        import io
        from PIL import Image
        with Image.open(icon_file) as _im:
            _im = _im.convert("RGBA")
            for _size, _name in ((48, "icon-48.png"), (192, "icon-192.png"), (180, "apple-touch-icon.png")):
                _buf = io.BytesIO()
                _im.resize((_size, _size), Image.LANCZOS).save(_buf, "PNG", optimize=True)
                _write(out_dir, _name, _buf.getvalue())
            _buf = io.BytesIO()
            _im.save(_buf, "ICO", sizes=[(16, 16), (32, 32), (48, 48)])
            _write(out_dir, "favicon.ico", _buf.getvalue())
    err_img = None   # foto voor de 404-pagina
    err_file = HERE / "assets" / "404.jpg"
    if err_file.exists():
        import io
        from PIL import Image
        with Image.open(err_file) as _im:
            _im = _im.convert("RGB")
            if _im.size[0] > 1200:
                _im = _im.resize((1200, round(_im.size[1] * 1200 / _im.size[0])), Image.LANCZOS)
            _buf = io.BytesIO()
            _im.save(_buf, "WEBP", quality=80, method=6)
            _data = _buf.getvalue()
            _name = f"404.{hashlib.sha1(_data).hexdigest()[:8]}.webp"
            _write(out_dir, _name, _data)
            err_img = (_name, _im.size[0], _im.size[1])
    logo = None
    logo_file = HERE / "assets" / "logo.png"
    if logo_file.exists():
        from PIL import Image
        lsrc = logo_file.read_bytes()
        lname = f"logo.{hashlib.sha1(lsrc).hexdigest()[:8]}.png"
        _write(out_dir, lname, lsrc)
        with Image.open(logo_file) as im:
            logo = (lname, im.size[0], im.size[1])

    # ---- vertalen (DeepL, met geheugen); nieuwste boeken eerst, zodat een beperkt budget daar het meest oplevert
    tr = translator
    if tr and foreign:
        labels = sorted({b["c1_label"] for b in books + sold} | {b["c2_label"] for b in books + sold if b["c2_label"]})
        _lab = {x.strip().lower() for x in labels}
        intros = sorted({v for k, v in CAT_INTROS.items() if k.split(" > ")[-1] in _lab})
        tr.ensure(labels + intros + ([cfg["tagline"]] if cfg["tagline_custom"] else []), foreign)
        keys = (("short",) if cfg["translate_short"] else ()) + (("long",) if cfg["translate_books"] else ())
        if keys:
            _tb = books + sold
            for i in range(0, len(_tb), 40):
                if tr.stopped:
                    rest = [b.get(k) for b in _tb[i:] for k in keys if (b.get(k) or "").strip()]
                    tr.missing += len(rest) * len(foreign)
                    tr.missing_chars += sum(len(x) for x in rest) * len(foreign)
                    break
                tr.ensure([x for b in _tb[i:i + 40] for x in (b.get(k) for k in keys) if x and x.strip()], foreign)
        if not cfg["translate_books"]:
            log("Alleen de Bijzonderheden en de categorienamen gaan via DeepL; de lange beschrijvingen blijven Nederlands "
                "(WEBSHOP_TRANSLATE_BOOKS=ja om die ook te vertalen).")
        log(tr.summary())
        stats["translation"] = tr.summary()
    book_tr = tr if (cfg["translate_books"] or cfg["translate_short"]) else None

    def cat_label(label, lang):
        if lang != "nl" and tr:
            label = tr.get(label, lang) or label
        label = (label or "").strip()
        return label[:1].upper() + label[1:]   # altijd met hoofdletter, ook als de bron dat niet heeft

    # ---- categorieën
    tree = {}
    for b in books:
        c = tree.setdefault(b["c1_slug"], {"label": b["c1_label"], "slug": b["c1_slug"], "books": [], "subs": {}})
        c["books"].append(b)
        if b["c2_slug"]:
            s = c["subs"].setdefault(b["c2_slug"], {"label": b["c2_label"], "slug": b["c2_slug"], "books": []})
            s["books"].append(b)
    top = sorted(tree.values(), key=lambda c: c["label"].lower())
    # opzoektabel voor 'Kijk ook in': 'hoofd' of 'hoofd > sub' (kleine letters) -> (hoofdlabel, sublabel of None, pad)
    cat_ref = {}
    for c in top:
        ck = (c["label"] or "").strip().lower()
        cat_ref[ck] = (c["label"], None, f"/categorie/{c['slug']}/")
        for sc in c["subs"].values():
            cat_ref[f"{ck} > {(sc['label'] or '').strip().lower()}"] = (c["label"], sc["label"], f"/categorie/{c['slug']}/{sc['slug']}/")

    urls = []
    count_pages = 0
    author_more, author_pages = build_authors(books)   # 'Meer van deze auteur' + auteurspagina's (>= 3 boeken), een keer berekend voor alle talen
    for lang in langs:
        # Knoppen onder de zoekbalk: heeft een hoofdcategorie subcategorieën, dan tonen we die (bijv. Fotografie) en niet
        # de hoofdcategorie zelf (Hobby). Boeken zonder subcategorie houden een knop met de hoofdcategorie.
        buttons = []
        for c in top:
            if not c["subs"]:
                buttons.append({"label": cat_label(c["label"], lang), "slug": c["slug"], "count": len(c["books"]),
                                "other": c["label"].strip().lower() == "overig"})
                continue
            for s in c["subs"].values():
                buttons.append({"label": cat_label(s["label"], lang), "slug": s["slug"], "count": len(s["books"]),
                                "href": f"/categorie/{c['slug']}/{s['slug']}/"})
            rest = sum(1 for b in c["books"] if not b["c2_slug"])
            if rest:
                buttons.append({"label": cat_label(c['label'], lang), "slug": c["slug"], "count": rest, "has_subs": True,
                                "other": c["label"].strip().lower() == "overig"})
        # Alleen de 25 grootste (naar aantal boeken), grootste eerst.
        buttons.sort(key=lambda x: (-x["count"], x["label"].lower()))
        # "Overig" krijgt geen eigen knop: het zit in de knop "Overige categorieën" (en op die pagina, bovenaan).
        others = [x for x in buttons if x.get("other")]
        buttons = [x for x in buttons if not x.get("other")]
        rest_buttons = others + buttons[25:]
        buttons = buttons[:25]
        tagline = None
        if cfg["tagline_custom"]:
            tagline = cfg["tagline"] if lang == "nl" else ((tr.get(cfg["tagline"], lang) if tr else None) or cfg["tagline"])
        site = Site(cfg, css_name, js_name, buttons, logo, lang, tagline, more_cats=sum(x['count'] for x in rest_buttons))
        site.err_img = err_img

        def emit(path, html, site=site):
            nonlocal count_pages
            _write(out_dir, _page_dir(site.L(path)), html)
            count_pages += 1

        emit("/", site.home(books[:24]))
        urls.append(site.L("/"))

        def intro_for(raw_label, label, parent_label, raw_parent=None, site=site, lang=lang):
            """Uitlegzin bovenaan een categorie: eigen tekst (content/categorie_teksten.json) voor hoofdcategorieën, anders een nette standaardzin."""
            key = (raw_label or "").strip().lower()
            if raw_parent:
                key = f"{raw_parent.strip().lower()} > {key}"
            text = CAT_INTROS.get(key)
            if text:
                if lang == "nl":
                    return text
                if tr and tr.get(text, lang):
                    return tr.get(text, lang)
            if parent_label:
                return i18n.t("p_cat_intro_sub", lang, label=label, parent=parent_label)
            return i18n.t("p_cat_intro_top", lang, label=label, shop=cfg["name"])

        def see_also_for(raw_label, raw_parent=None, lang=lang):
            key = (raw_label or "").strip().lower()
            if raw_parent:
                key = f"{raw_parent.strip().lower()} > {key}"
            out = []
            for target in CAT_REFS.get(key, []):
                hit = cat_ref.get(target)
                if not hit or target == key:
                    continue
                c1, c2, path = hit
                name = cat_label(c1, lang) if not c2 else f"{cat_label(c1, lang)} › {cat_label(c2, lang)}"
                out.append((name, path))
            return out

        def listing(label, parent, base, items, intro="", see_also=None, subs=None, all_total=None, site=site):
            pages = max(1, -(-len(items) // PER_PAGE))
            for page in range(1, pages + 1):
                chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
                html = site.category(label=label, parent=parent, base=base, books=chunk, page=page, pages=pages, total=len(items), intro=intro,
                                     see_also=see_also, subs=subs, all_total=all_total)
                emit(base if page == 1 else f"{base}pagina/{page}/", html)
            urls.append(site.L(base))

        topo_order = {slugify(c1_labels.get("topografie", "topografie")): {}}
        for _i, _l in enumerate((c2_labels.get("topografie") or {}).values()):
            if _l and slugify(_l) not in topo_order[slugify(c1_labels.get("topografie", "topografie"))]:
                topo_order[slugify(c1_labels.get("topografie", "topografie"))][slugify(_l)] = _i
        for c in top:
            base = f"/categorie/{c['slug']}/"
            if c["subs"]:
                # Knoppen voor alle subcategorieën (op alfabet) en daaronder alleen de boeken zonder subcategorie.
                sub_buttons = sorted(((cat_label(s["label"], lang), f"{base}{s['slug']}/", len(s["books"]), s["slug"]) for s in c["subs"].values()),
                                     key=lambda x: x[0].lower())
                # Uitzondering: Topografie staat niet op alfabet maar van klein naar groot (provincies, landen, werelddelen, wereld),
                # in de volgorde van het dashboard. Onbekende subcategorieën komen daarna, op alfabet.
                if c["slug"] in topo_order:
                    sub_buttons.sort(key=lambda x: topo_order[c["slug"]].get(x[3], 10 ** 6))
                sub_buttons = [x[:3] for x in sub_buttons]
                own_books = [b for b in c["books"] if not b["c2_slug"]]
                listing(cat_label(c["label"], lang), None, base, own_books, intro_for(c["label"], cat_label(c["label"], lang), None),
                        see_also=see_also_for(c["label"]), subs=sub_buttons, all_total=len(c["books"]))
            else:
                listing(cat_label(c["label"], lang), None, base, c["books"], intro_for(c["label"], cat_label(c["label"], lang), None),
                        see_also=see_also_for(c["label"]))
            for s in sorted(c["subs"].values(), key=lambda s: s["label"].lower()):
                listing(cat_label(s["label"], lang), {"label": cat_label(c["label"], lang), "slug": c["slug"]},
                        f"{base}{s['slug']}/", s["books"], intro_for(s["label"], cat_label(s["label"], lang), cat_label(c["label"], lang), c["label"]),
                        see_also=see_also_for(s["label"], c["label"]))

        for b in books:
            chain = [(cat_label(b["c1_label"], lang), f"/categorie/{b['c1_slug']}/")]
            if b["c2_slug"]:
                chain.append((cat_label(b["c2_label"], lang), f"/categorie/{b['c1_slug']}/{b['c2_slug']}/"))
            emit(b["path"], site.book(b, _book_loc(b, lang, book_tr, cat_label), chain, more_by=author_more.get(b["id"])))
            urls.append(site.L(b["path"]))

        for ap in author_pages:
            base = f"/auteur/{ap['slug']}/"
            n_pages = max(1, -(-len(ap["books"]) // PER_PAGE))
            for page in range(1, n_pages + 1):
                chunk = ap["books"][(page - 1) * PER_PAGE: page * PER_PAGE]
                emit(base if page == 1 else f"{base}pagina/{page}/",
                     site.author_page(name=ap["name"], slug=ap["slug"], books=chunk, page=page, pages=n_pages, total=len(ap["books"])))
            urls.append(site.L(base))

        for b in sold:
            # Verwijs naar de categorie als die nog boeken heeft, anders naar de beginpagina; toon maximaal 4 vergelijkbare boeken.
            c1 = tree.get(b["c1_slug"])
            sub = c1["subs"].get(b["c2_slug"]) if c1 and b["c2_slug"] else None
            chain = []
            if c1:
                chain.append((cat_label(b["c1_label"], lang), f"/categorie/{b['c1_slug']}/"))
            if sub:
                chain.append((cat_label(b["c2_label"], lang), f"/categorie/{b['c1_slug']}/{b['c2_slug']}/"))
            pool = (sub or c1 or {"books": books})["books"][:4]
            emit(b["path"], site.book(b, _book_loc(b, lang, book_tr, cat_label), chain, sold=True, similar=pool))

        if rest_buttons:
            all_main = sorted(({"label": cat_label(c["label"], lang), "slug": c["slug"], "count": len(c["books"]),
                                "has_subs": bool(c["subs"])} for c in top),
                              key=lambda x: x["label"].lower())
            emit("/overige-categorieen/", site.more_categories_page(all_main))
            urls.append(site.L("/overige-categorieen/"))
        emit("/zoeken/", site.search_page())
        emit("/winkelwagen/", site.cart_page())
        emit("/afrekenen/", site.checkout_page())
        emit("/bedankt/", site.thanks_page())
        for slug, key in (("voorwaarden", "p_terms"), ("privacy", "p_privacy")):
            tpl = _content_template(slug, lang)
            emit(f"/{slug}/", site.content_page(i18n.t(key, lang), f"/{slug}/", _fill(tpl, cfg, lang)))
            urls.append(site.L(f"/{slug}/"))
        if lang == "nl":
            _write(out_dir, "404.html", site.not_found())

    # ---- productfeed voor Google Merchant Center (Nederlandse pagina's, verzending naar Nederland)
    _write(out_dir, "feed.txt", _merchant_feed(books, cfg, book_tr, cat_label))

    # ---- sitemap, robots, htaccess
    base_url = cfg["base_url"]
    # Een sitemap mag maximaal 50.000 adressen bevatten: daarboven wordt het een index met deelbestanden.
    _hdr = '<?xml version="1.0" encoding="UTF-8"?>'
    _ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    _chunks = [urls[i:i + 45000] for i in range(0, len(urls), 45000)] or [[]]
    if len(_chunks) == 1:
        _write(out_dir, "sitemap.xml", f"{_hdr}<urlset {_ns}>" + "".join(f"<url><loc>{base_url}{u}</loc></url>" for u in urls) + "</urlset>")
    else:
        for n, ch in enumerate(_chunks, 1):
            _write(out_dir, f"sitemap-{n}.xml", f"{_hdr}<urlset {_ns}>" + "".join(f"<url><loc>{base_url}{u}</loc></url>" for u in ch) + "</urlset>")
        _write(out_dir, "sitemap.xml", f"{_hdr}<sitemapindex {_ns}>" + "".join(f"<sitemap><loc>{base_url}/sitemap-{n}.xml</loc></sitemap>" for n in range(1, len(_chunks) + 1)) + "</sitemapindex>")
    disallow = "".join(f"Disallow: {i18n.localize(p, l)}\n" for l in langs for p in ("/zoeken/", "/winkelwagen/", "/afrekenen/", "/bedankt/"))
    _write(out_dir, "robots.txt", "User-agent: *\n" + disallow + "Disallow: /*.php\n" + f"Sitemap: {base_url}/sitemap.xml\n")
    _write(out_dir, ".htaccess", HTACCESS)
    if cfg.get("indexnow") and cfg.get("indexnow_key"):
        _write(out_dir, f"{cfg['indexnow_key']}.txt", cfg["indexnow_key"])   # IndexNow: bewijs dat de site van ons is

    # ---- PHP en data
    for name in ("lib.php", "checkout.php", "code.php", "search.php", "webhook.php", "status.php", "mailtest.php"):
        src = HERE / "php" / name
        if not src.exists():
            raise RuntimeError(f"PHP-bestand ontbreekt: {src}")
        _write(out_dir, name, src.read_bytes())
    _write(out_dir, "norm.php", norm_php())
    _write(out_dir, "i18n.php", i18n_php(cfg))
    _write(out_dir, "config.php", config_php(cfg))
    _write(out_dir, f"{data_dir}/.htaccess", DATA_HTACCESS)
    write_catalog(out_dir / data_dir / "catalog.sqlite", books, imported_order_ids, cfg, built_at)

    stats.update(pages=count_pages, new_images=new_imgs, failed_images=failed_imgs, keep=sorted(used_images),
                 seconds=round(time.time() - t0, 1))
    return stats


def main_build(out_dir="webshop_dist", mode="full", known_images=(), known_pages=None, process_images=True):
    """Bouwt vanuit Postgres. Wordt aangeroepen door main.py / publish."""
    from db import get_connection
    conn = get_connection()
    try:
        books, imported, fetch_image = load_from_postgres(conn)
        stats = build_site(books, out_dir, mode=mode, imported_order_ids=imported, known_images=known_images,
                           known_pages=known_pages, fetch_image=fetch_image, process_images=process_images)
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    print(main_build(*(sys.argv[1:2] or ["webshop_dist"])))

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
import shutil
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import i18n
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
                   short_description, long_description, price, shipping_cost, amount, category1, category2, listing_date
            FROM books
            WHERE COALESCE(amount, 0) > 0 AND COALESCE(pending_create, FALSE) = FALSE AND COALESCE(queued, FALSE) = FALSE
              AND price IS NOT NULL AND price > 0 AND COALESCE(title, '') <> ''
            """
        )
        rows = cur.fetchall()
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
    for r in rows:
        books.append({
            "id": int(r["id"]), "title": r["title"], "author": r["author"], "publisher": r["publisher"], "isbn": r["ean"],
            "language": r["language"], "short": r["short_description"], "long": r["long_description"],
            "price": float(r["price"]), "shipping": float(r["shipping_cost"]) if r["shipping_cost"] is not None else None,
            "amount": int(r["amount"]), "category1": r["category1"], "category2": r["category2"],
            "listing_date": r["listing_date"],
            "images": [{"image_id": i["image_id"], "url_large": i["url_large"], "url_medium": i["url_medium"],
                        "has_data": bool(i["has_data"])} for i in images.get(int(r["id"]), [])],
        })

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
        ship = int(round(r["shipping"] * 100)) if r.get("shipping") and r["shipping"] > 0 else cfg["default_shipping_cents"]
        title = " ".join((r.get("title") or "").split())
        if not title or price <= 0 or r["amount"] <= 0:
            continue
        c1 = r.get("category1") or "overig diverse"
        c1_label = c1_labels.get(c1, c1)
        c2 = r.get("category2") or None
        c2_label = c2_labels.get(c1, {}).get(c2, c2) if c2 else None
        isbn = "".join(ch for ch in (r.get("isbn") or "") if ch.isdigit() or ch in "xX")
        lang = (r.get("language") or "").strip()
        b = dict(r)
        b.update({
            "title": title, "author": " ".join((r.get("author") or "").split()), "price_cents": price, "ship_cents": ship,
            "isbn": isbn, "language": lang, "lang_code": LANG_CODES.get(lang.lower()) or (lang.lower() if len(lang) in (2, 3) and lang.isalpha() else None),
            "path": f"/boek/{slugify(title)}-{r['id']}/",
            "c1": c1, "c1_label": c1_label, "c1_slug": slugify(c1_label), "c2": c2, "c2_label": c2_label,
            "c2_slug": slugify(c2_label) if c2_label else None,
            "category_label": c1_label + (f" › {c2_label}" if c2_label else ""),
        })
        books.append(b)
    # Nieuwste eerst: listing_date (tekst, ISO-achtig) aflopend, dan id.
    books.sort(key=lambda x: (str(x.get("listing_date") or ""), x["id"]), reverse=True)
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


_FILL_TEXT = {  # per taal: (KvK, btw, telefoon, gratis, extra per boek, buitenland)
    "nl": ("KvK: {}", "btw-nummer: {}", "Telefoon: {}", " en vanaf {} bestelwaarde is de verzending binnen Nederland gratis",
           ", plus {} per extra boek", "Voor verzending buiten Nederland komt er een toeslag per bestelling bij: {}. "),
    "en": ("Chamber of Commerce (KvK): {}", "VAT no.: {}", "Phone: {}", " and from {} order value shipping within the Netherlands is free",
           ", plus {} per additional book", "For shipping outside the Netherlands a surcharge per order applies: {}. "),
    "de": ("Handelsregister (KvK): {}", "USt-IdNr.: {}", "Telefon: {}", " und ab {} Bestellwert ist der Versand innerhalb der Niederlande kostenlos",
           ", zuzüglich {} je weiterem Buch", "Für den Versand außerhalb der Niederlande fällt pro Bestellung ein Aufschlag an: {}. "),
    "fr": ("Chambre de commerce (KvK) : {}", "N° de TVA : {}", "Téléphone : {}", " et à partir de {} d'achats l'envoi aux Pays-Bas est gratuit",
           ", plus {} par livre supplémentaire", "Pour une expédition hors des Pays-Bas, un supplément par commande s'applique : {}. "),
}


def _fill(template, cfg, lang="nl"):
    kvk_t, vat_t, tel_t, free_t, extra_t, abroad_t = _FILL_TEXT[lang]

    def line(text):
        return text + "<br>" if text else ""
    free = free_t.format(eur(cfg["free_shipping_from_cents"], lang)) if cfg["free_shipping_from_cents"] else ""
    extra = extra_t.format(eur(cfg["extra_per_additional_book_cents"], lang)) if cfg["extra_per_additional_book_cents"] else ""
    sur = cfg["country_surcharge_cents"]
    abroad = abroad_t.format("; ".join(f"{i18n.country_name(c, lang)}: {eur(v, lang)}" for c, v in sur.items())) if sur else ""
    values = {
        "naam": cfg["name"] + "<br>", "eigenaar": line(cfg["owner_name"]), "adres": line(cfg["address"]),
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
        return {"short": b.get("short"), "long": b.get("long"), "short_fb": False, "long_fb": False,
                "category_label": b["category_label"], "language": b.get("language")}
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
        "base_url": cfg["base_url"], "webhook_base": cfg["webhook_base"], "name": cfg["name"], "email": cfg["contact_email"],
        "data_dir": "d-" + cfg["data_key"], "mollie_key": cfg["mollie_key"], "mollie_api_base": cfg["mollie_api_base"],
        "default_shipping_cents": cfg["default_shipping_cents"], "extra_cents": cfg["extra_per_additional_book_cents"],
        "free_from_cents": cfg["free_shipping_from_cents"], "countries": cfg["countries"], "reserve_seconds": 1800,
        "country_surcharge": cfg["country_surcharge_cents"],
    }
    text = json.dumps(data, ensure_ascii=False)
    return "<?php\n// Gegenereerd door build.py (bevat geheimen: niet delen).\nreturn json_decode(<<<'JSON'\n" + text + "\nJSON\n, true);\n"


def i18n_php(cfg):
    text = json.dumps(i18n.php_data(cfg["langs"], cfg), ensure_ascii=False)
    return "<?php\n// Gegenereerd door build.py uit webshop/i18n.py — niet handmatig wijzigen.\nreturn json_decode(<<<'JSON'\n" + text + "\nJSON\n, true);\n"


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
                ship_cents INTEGER, amount INTEGER, path TEXT, img TEXT, iw INTEGER, ih INTEGER, hay TEXT, tnorm TEXT, seq INTEGER);
            CREATE INDEX books_seq ON books(seq);
            CREATE TABLE imported_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        rows = []
        for b in books:
            th = b.get("thumb")
            hay = " " + norm(" ".join(filter(None, [b["title"], b["author"], b.get("publisher")]))) + " " + (b["isbn"] or "").lower() + " "
            rows.append((b["id"], b["title"], b["author"], b["isbn"], b["price_cents"], b["ship_cents"], b["amount"], b["path"],
                         th[0] if th else None, th[1] if th else None, th[2] if th else None, hay, norm(b["title"]), b["seq"]))
        con.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
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
    new_imgs, failed_imgs, used_images = prepare_images(books, out_dir, known_images, fetch_image, process_images, log, image_deadline)
    for b in books:
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
        labels = sorted({b["c1_label"] for b in books} | {b["c2_label"] for b in books if b["c2_label"]})
        tr.ensure(labels + ([cfg["tagline"]] if cfg["tagline_custom"] else []), foreign)
        if cfg["translate_books"]:
            for i in range(0, len(books), 40):
                if tr.stopped:
                    rest = [b.get(k) for b in books[i:] for k in ("short", "long") if (b.get(k) or "").strip()]
                    tr.missing += len(rest) * len(foreign)
                    tr.missing_chars += sum(len(x) for x in rest) * len(foreign)
                    break
                tr.ensure([x for b in books[i:i + 40] for x in (b.get("short"), b.get("long")) if x and x.strip()], foreign)
        else:
            log("Boekbeschrijvingen worden niet vertaald (WEBSHOP_TRANSLATE_BOOKS staat niet aan); alleen de categorienamen gaan via DeepL.")
        log(tr.summary())
        stats["translation"] = tr.summary()
    book_tr = tr if cfg["translate_books"] else None

    def cat_label(label, lang):
        if lang == "nl" or not tr:
            return label
        return tr.get(label, lang) or label

    # ---- categorieën
    tree = {}
    for b in books:
        c = tree.setdefault(b["c1_slug"], {"label": b["c1_label"], "slug": b["c1_slug"], "books": [], "subs": {}})
        c["books"].append(b)
        if b["c2_slug"]:
            s = c["subs"].setdefault(b["c2_slug"], {"label": b["c2_label"], "slug": b["c2_slug"], "books": []})
            s["books"].append(b)
    top = sorted(tree.values(), key=lambda c: c["label"].lower())

    urls = []
    count_pages = 0
    for lang in langs:
        # Knoppen onder de zoekbalk: heeft een hoofdcategorie subcategorieën, dan tonen we die (bijv. Fotografie) en niet
        # de hoofdcategorie zelf (Hobby). Boeken zonder subcategorie houden een knop met de hoofdcategorie.
        buttons = []
        for c in top:
            if not c["subs"]:
                buttons.append({"label": cat_label(c["label"], lang), "slug": c["slug"], "count": len(c["books"])})
                continue
            for s in c["subs"].values():
                buttons.append({"label": cat_label(s["label"], lang), "slug": s["slug"], "count": len(s["books"]),
                                "href": f"/categorie/{c['slug']}/{s['slug']}/"})
            rest = sum(1 for b in c["books"] if not b["c2_slug"])
            if rest:
                buttons.append({"label": f"{cat_label(c['label'], lang)} ({i18n.t('p_other', lang)})", "slug": c["slug"], "count": rest})
        # Alleen de 25 grootste (naar aantal boeken), grootste eerst.
        buttons.sort(key=lambda x: (-x["count"], x["label"].lower()))
        buttons = buttons[:25]
        tagline = None
        if cfg["tagline_custom"]:
            tagline = cfg["tagline"] if lang == "nl" else ((tr.get(cfg["tagline"], lang) if tr else None) or cfg["tagline"])
        site = Site(cfg, css_name, js_name, buttons, logo, lang, tagline)

        def emit(path, html, site=site):
            nonlocal count_pages
            _write(out_dir, _page_dir(site.L(path)), html)
            count_pages += 1

        emit("/", site.home(books[:24]))
        urls.append(site.L("/"))

        def listing(label, parent, base, items, site=site):
            pages = max(1, -(-len(items) // PER_PAGE))
            for page in range(1, pages + 1):
                chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
                html = site.category(label=label, parent=parent, base=base, books=chunk, page=page, pages=pages, total=len(items))
                emit(base if page == 1 else f"{base}pagina/{page}/", html)
            urls.append(site.L(base))

        for c in top:
            base = f"/categorie/{c['slug']}/"
            listing(cat_label(c["label"], lang), None, base, c["books"])
            for s in sorted(c["subs"].values(), key=lambda s: s["label"].lower()):
                listing(cat_label(s["label"], lang), {"label": cat_label(c["label"], lang), "slug": c["slug"]},
                        f"{base}{s['slug']}/", s["books"])

        for b in books:
            chain = [(cat_label(b["c1_label"], lang), f"/categorie/{b['c1_slug']}/")]
            if b["c2_slug"]:
                chain.append((cat_label(b["c2_label"], lang), f"/categorie/{b['c1_slug']}/{b['c2_slug']}/"))
            emit(b["path"], site.book(b, _book_loc(b, lang, book_tr, cat_label), chain))
            urls.append(site.L(b["path"]))

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

    # ---- sitemap, robots, htaccess
    base_url = cfg["base_url"]
    sm = ['<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"<url><loc>{base_url}{u}</loc></url>" for u in urls]
    sm.append("</urlset>")
    _write(out_dir, "sitemap.xml", "".join(sm))
    disallow = "".join(f"Disallow: {i18n.localize(p, l)}\n" for l in langs for p in ("/zoeken/", "/winkelwagen/", "/afrekenen/", "/bedankt/"))
    _write(out_dir, "robots.txt", "User-agent: *\n" + disallow + "Disallow: /*.php\n" + f"Sitemap: {base_url}/sitemap.xml\n")
    _write(out_dir, ".htaccess", HTACCESS)

    # ---- PHP en data
    for name in ("lib.php", "checkout.php", "search.php", "webhook.php", "status.php"):
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

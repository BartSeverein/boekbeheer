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


def _fill(template, cfg):
    def line(text):
        return text + "<br>" if text else ""
    free = f" en vanaf {eur(cfg['free_shipping_from_cents'])} bestelwaarde is de verzending gratis" if cfg["free_shipping_from_cents"] else ""
    extra = f", plus {eur(cfg['extra_per_additional_book_cents'])} per extra boek" if cfg["extra_per_additional_book_cents"] else ""
    names = {"NL": "Nederland", "BE": "België", "DE": "Duitsland"}
    values = {
        "naam": cfg["name"] + "<br>", "eigenaar": line(cfg["owner_name"]), "adres": line(cfg["address"]),
        "kvk": line(f"KvK: {cfg['kvk']}") if cfg["kvk"] else "", "btw": line(f"btw-nummer: {cfg['vat']}") if cfg["vat"] else "",
        "telefoon": line(f"Telefoon: {cfg['phone']}") if cfg["phone"] else "", "email": cfg["contact_email"] or "info@example.invalid",
        "landen": ", ".join(names.get(c, c) for c in cfg["countries"]), "toeslag": extra, "gratis": free,
    }
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


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
    }
    text = json.dumps(data, ensure_ascii=False)
    return "<?php\n// Gegenereerd door build.py (bevat geheimen: niet delen).\nreturn json_decode(<<<'JSON'\n" + text + "\nJSON\n, true);\n"


HTACCESS = """\
# Gegenereerd door build.py
DirectoryIndex index.html
ErrorDocument 404 /404.html
Options -Indexes
AddDefaultCharset UTF-8
<FilesMatch "^\\.">
  Require all denied
</FilesMatch>
<FilesMatch "^(config|lib|norm)\\.php$">
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
               fetch_image=None, process_images=True, image_deadline=None, log=print):
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
    css_src, js_src = (HERE / "assets" / "s.css").read_bytes(), (HERE / "assets" / "c.js").read_bytes()
    css_name = f"s.{hashlib.sha1(css_src).hexdigest()[:8]}.css"
    js_name = f"c.{hashlib.sha1(js_src).hexdigest()[:8]}.js"
    _write(out_dir, css_name, css_src)
    _write(out_dir, js_name, js_src)

    # ---- categorieën
    tree = {}
    for b in books:
        c = tree.setdefault(b["c1_slug"], {"label": b["c1_label"], "slug": b["c1_slug"], "books": [], "subs": {}})
        c["books"].append(b)
        if b["c2_slug"]:
            s = c["subs"].setdefault(b["c2_slug"], {"label": b["c2_label"], "slug": b["c2_slug"], "books": []})
            s["books"].append(b)
    top = sorted(tree.values(), key=lambda c: c["label"].lower())
    site = Site(cfg, css_name, js_name, [{"label": c["label"], "slug": c["slug"], "count": len(c["books"])} for c in top])

    urls = ["/"]
    count_pages = 0

    def emit(path, html):
        nonlocal count_pages
        _write(out_dir, _page_dir(path), html)
        count_pages += 1

    emit("/", site.home(books[:24]))

    def listing(label, parent, base, items):
        pages = max(1, -(-len(items) // PER_PAGE))
        for page in range(1, pages + 1):
            chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
            html = site.category(label=label, parent=parent, base=base, books=chunk, page=page, pages=pages, total=len(items))
            emit(base if page == 1 else f"{base}pagina/{page}/", html)
        urls.append(base)

    for c in top:
        base = f"/categorie/{c['slug']}/"
        listing(c["label"], None, base, c["books"])
        for s in sorted(c["subs"].values(), key=lambda s: s["label"].lower()):
            listing(s["label"], {"label": c["label"], "slug": c["slug"]}, f"{base}{s['slug']}/", s["books"])

    # sub-categorie krijgt bij het boek ook een kruimelpad
    for b in books:
        chain = [(b["c1_label"], f"/categorie/{b['c1_slug']}/")]
        if b["c2_slug"]:
            chain.append((b["c2_label"], f"/categorie/{b['c1_slug']}/{b['c2_slug']}/"))
        emit(b["path"], site.book(b, chain))
        urls.append(b["path"])

    emit("/zoeken/", site.search_page())
    emit("/winkelwagen/", site.cart_page())
    emit("/afrekenen/", site.checkout_page())
    emit("/bedankt/", site.thanks_page())
    for slug, title in (("voorwaarden", "Voorwaarden en verzending"), ("privacy", "Privacy")):
        tpl = (HERE / "content" / f"{slug}.html").read_text(encoding="utf-8")
        emit(f"/{slug}/", site.content_page(title, f"/{slug}/", _fill(tpl, cfg)))
        urls.append(f"/{slug}/")
    _write(out_dir, "404.html", site.not_found())

    # ---- sitemap, robots, htaccess
    base_url = cfg["base_url"]
    sm = ['<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    sm += [f"<url><loc>{base_url}{u}</loc></url>" for u in urls]
    sm.append("</urlset>")
    _write(out_dir, "sitemap.xml", "".join(sm))
    _write(out_dir, "robots.txt", "User-agent: *\nDisallow: /zoeken/\nDisallow: /winkelwagen/\nDisallow: /afrekenen/\nDisallow: /bedankt/\n"
                                  "Disallow: /*.php\n"
                                  f"Sitemap: {base_url}/sitemap.xml\n")
    _write(out_dir, ".htaccess", HTACCESS)

    # ---- PHP en data
    for name in ("lib.php", "checkout.php", "search.php", "webhook.php", "status.php"):
        src = HERE / "php" / name
        if not src.exists():
            raise RuntimeError(f"PHP-bestand ontbreekt: {src}")
        _write(out_dir, name, src.read_bytes())
    _write(out_dir, "norm.php", norm_php())
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

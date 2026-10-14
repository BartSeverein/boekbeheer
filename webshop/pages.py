"""
webshop/pages.py — de HTML van alle pagina's. Bewust kaal: geen frameworks, geen inline scripts (alleen JSON-LD),
één klein stijlbestand en één klein scriptbestand met een versie in de bestandsnaam (jaar lang te cachen).
"""
import html
import json
import re

from .common import eur


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def trim(text, limit):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:- ")
    return cut + "…"


def jsonld(data):
    """JSON-LD veilig in een <script>-blok (geen '</script>' of '<!--' uit de data)."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


class Site:
    def __init__(self, cfg, css_name, js_name, categories):
        self.cfg = cfg
        self.css = css_name
        self.js = js_name
        self.categories = categories  # lijst van dicts: label, slug, count

    def url(self, path):
        return self.cfg["base_url"] + path

    # ---- gedeeld skelet ----
    def layout(self, *, title, desc, path, body, robots=None, image=None, og_type="website", ld=None, scripts=True,
               show_cats=False):
        cfg = self.cfg
        full_title = title if title.endswith(cfg["name"]) else f"{title} | {cfg['name']}"
        head = [
            '<!doctype html><html lang="nl"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{esc(full_title)}</title>",
            f'<meta name="description" content="{esc(trim(desc, 158))}">',
            f'<link rel="canonical" href="{esc(self.url(path))}">',
        ]
        if robots:
            head.append(f'<meta name="robots" content="{robots}">')
        head += [
            f'<meta property="og:title" content="{esc(trim(title, 90))}">',
            f'<meta property="og:description" content="{esc(trim(desc, 158))}">',
            f'<meta property="og:type" content="{og_type}">',
            f'<meta property="og:url" content="{esc(self.url(path))}">',
            f'<meta property="og:site_name" content="{esc(cfg["name"])}">',
            '<meta property="og:locale" content="nl_NL">',
        ]
        if image:
            head.append(f'<meta property="og:image" content="{esc(self.url(image))}">')
        head.append(f'<link rel="stylesheet" href="/{self.css}">')
        for block in ld or []:
            head.append(f'<script type="application/ld+json">{jsonld(block)}</script>')
        head.append("</head>")
        data = (f'data-extra="{cfg["extra_per_additional_book_cents"]}" data-free="{cfg["free_shipping_from_cents"]}" '
                f'data-def="{cfg["default_shipping_cents"]}"')
        cats = ""
        if show_cats and self.categories:
            cats = '<nav class="cats" aria-label="Categorieën"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">' + "".join(
                f'<li><a href="/categorie/{c["slug"]}/">{esc(c["label"])} ({c["count"]})</a></li>' for c in self.categories
            ) + "</ul></nav>"
        header = (
            f'<body {data}><header><a class="brand" href="/">{esc(cfg["name"])}</a>'
            '<form action="/zoeken/" method="get" role="search">'
            '<input type="search" name="q" placeholder="Zoek op titel, auteur of ISBN" aria-label="Zoeken" minlength="2" required>'
            '<button type="submit">Zoek</button></form>'
            '<a href="/winkelwagen/" id="cartlink" rel="nofollow">Winkelwagen</a></header><main>'
        )
        footer = (
            '</main><footer><a href="/voorwaarden/">Voorwaarden en verzending</a><a href="/privacy/">Privacy</a>'
            + (f'<a href="mailto:{esc(cfg["contact_email"])}">{esc(cfg["contact_email"])}</a>' if cfg["contact_email"] else "")
            + "</footer>"
        )
        script = f'<script src="/{self.js}" defer></script>' if scripts else ""
        return "".join(head) + header + cats + body + footer + script + "</body></html>"

    # ---- kaartjes en paginering ----
    def card(self, b):
        img = ""
        if b.get("thumb"):
            p, w, h = b["thumb"]
            img = f'<img src="{p}" width="{w}" height="{h}" alt="" loading="lazy" decoding="async">'
        author = f"<p>{esc(b['author'])}</p>" if b.get("author") else ""
        return (f'<li class="card"><a href="{b["path"]}"><div class="ph">{img}</div><h3>{esc(b["title"])}</h3>'
                f'{author}<p class="pr">{eur(b["price_cents"])}</p></a></li>')

    def grid(self, books):
        return '<ul class="grid">' + "".join(self.card(b) for b in books) + "</ul>"

    @staticmethod
    def pager(base, page, pages):
        if pages <= 1:
            return ""
        prev = (f'<a rel="prev" href="{base if page == 2 else f"{base}pagina/{page - 1}/"}">← Vorige</a>' if page > 1 else "")
        nxt = f'<a rel="next" href="{base}pagina/{page + 1}/">Volgende →</a>' if page < pages else ""
        return f'<nav class="pager" aria-label="Paginering">{prev}<span>Pagina {page} van {pages}</span>{nxt}</nav>'

    # ---- pagina's ----
    def home(self, newest):
        cfg = self.cfg
        body = (f"<h1>{esc(cfg['name'])}</h1><p>{esc(cfg['tagline'])}</p><h2>Nieuw binnen</h2>" + self.grid(newest))
        ld = [{
            "@context": "https://schema.org", "@type": "BookStore", "name": cfg["name"], "url": self.url("/"),
            "description": cfg["tagline"],
        }, {
            "@context": "https://schema.org", "@type": "WebSite", "name": cfg["name"], "url": self.url("/"),
        }]
        return self.layout(title=cfg["name"] + " - tweedehands boeken", desc=cfg["tagline"], path="/", body=body, ld=ld,
                           show_cats=True, scripts=False)

    def category(self, *, label, parent, base, books, page, pages, total, desc_extra=""):
        crumbs = [("Home", "/")]
        if parent:
            crumbs.append((parent["label"], f"/categorie/{parent['slug']}/"))
        crumbs.append((label, base))
        nav = '<nav class="crumb" aria-label="Kruimelpad">' + " › ".join(
            f'<a href="{u}">{esc(t)}</a>' if i < len(crumbs) - 1 else esc(t) for i, (t, u) in enumerate(crumbs)) + "</nav>"
        suffix = f" (pagina {page})" if page > 1 else ""
        path = base if page == 1 else f"{base}pagina/{page}/"
        body = nav + f"<h1>{esc(label)}{suffix}</h1><p>{total} tweedehands boeken in deze categorie.</p>" + \
            self.grid(books) + self.pager(base, page, pages)
        ld = [{
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(u)} for i, (t, u) in enumerate(crumbs)],
        }, {
            "@context": "https://schema.org", "@type": "CollectionPage", "name": f"{label}{suffix}", "url": self.url(path),
            "isPartOf": {"@type": "WebSite", "name": self.cfg["name"], "url": self.url("/")},
        }]
        desc = f"{label}: {total} tweedehands boeken{suffix}. Bekijk het aanbod van {self.cfg['name']}."
        return self.layout(title=f"{label}{suffix} - tweedehands boeken", desc=desc, path=path, body=body, ld=ld, scripts=False)

    def book(self, b, category_chain):
        cfg = self.cfg
        path = b["path"]
        crumbs = [("Home", "/")] + category_chain + [(b["title"], path)]
        nav = '<nav class="crumb" aria-label="Kruimelpad">' + " › ".join(
            f'<a href="{u}">{esc(t)}</a>' if i < len(crumbs) - 1 else esc(trim(t, 60)) for i, (t, u) in enumerate(crumbs)) + "</nav>"
        imgs = b.get("images") or []
        gallery = ""
        if imgs:
            p, w, h = imgs[0]["detail"]
            gallery = (f'<img src="{p}" width="{w}" height="{h}" alt="{esc(b["title"])}" fetchpriority="high" decoding="async">')
            if len(imgs) > 1:
                gallery += '<div class="thumbs">' + "".join(
                    f'<a href="{i["detail"][0]}"><img src="{i["thumb"][0]}" width="{i["thumb"][1]}" height="{i["thumb"][2]}" alt="" loading="lazy" decoding="async"></a>'
                    for i in imgs[1:]) + "</div>"
        rows = []
        for label, value in (("Auteur", b.get("author")), ("Uitgever", b.get("publisher")), ("ISBN", b.get("isbn")),
                             ("Taal", b.get("language")), ("Bijzonderheden", b.get("short")), ("Categorie", b.get("category_label"))):
            if value:
                rows.append(f"<dt>{label}</dt><dd>{esc(value)}</dd>")
        desc_html = "".join(f"<p>{esc(p)}</p>" for p in re.split(r"\n\s*\n|\r\n\s*\r\n", (b.get("long") or "").strip()) if p.strip())
        ship = eur(b["ship_cents"])
        stock_note = f" ({b['amount']} exemplaren)" if b["amount"] > 1 else ""
        buy = (
            f'<p class="price">{eur(b["price_cents"])}</p><p>Op voorraad{stock_note}. Verzendkosten {ship} (bij meer boeken: hoogste tarief{" plus toeslag" if cfg["extra_per_additional_book_cents"] else ""}).</p>'
            f'<p><button type="button" class="add" data-id="{b["id"]}" data-title="{esc(b["title"])}" data-price="{b["price_cents"]}" '
            f'data-ship="{b["ship_cents"]}" data-max="{b["amount"]}" data-url="{path}">In winkelwagen leggen</button></p>'
            '<p id="added" hidden><a href="/winkelwagen/">Bekijk winkelwagen</a></p>'
        )
        body = nav + f"<article class=\"book\"><div>{gallery}</div><div><h1>{esc(b['title'])}</h1>" + \
            (f"<p>{esc(b['author'])}</p>" if b.get("author") else "") + buy + (f"<dl>{''.join(rows)}</dl>" if rows else "") + \
            (f"<h2>Over dit boek</h2>{desc_html}" if desc_html else "") + "</div></article>"

        image_urls = [self.url(i["detail"][0]) for i in imgs]
        product = {
            "@context": "https://schema.org", "@type": ["Product", "Book"], "@id": self.url(path) + "#boek",
            "name": b["title"], "url": self.url(path), "sku": str(b["id"]),
            "description": trim(b.get("long") or b.get("short") or b["title"], 300),
            "inLanguage": b.get("lang_code") or None,
            "offers": {
                "@type": "Offer", "url": self.url(path), "priceCurrency": "EUR", "price": f"{b['price_cents'] / 100:.2f}",
                "availability": "https://schema.org/InStock", "itemCondition": "https://schema.org/UsedCondition",
                "seller": {"@type": "Organization", "name": cfg["name"], "url": self.url("/")},
                "shippingDetails": {
                    "@type": "OfferShippingDetails",
                    "shippingRate": {"@type": "MonetaryAmount", "value": f"{b['ship_cents'] / 100:.2f}", "currency": "EUR"},
                    "shippingDestination": {"@type": "DefinedRegion", "addressCountry": cfg["countries"][0] if cfg["countries"] else "NL"},
                },
            },
        }
        if image_urls:
            product["image"] = image_urls
        if b.get("author"):
            product["author"] = {"@type": "Person", "name": b["author"]}
        if b.get("publisher"):
            product["publisher"] = {"@type": "Organization", "name": b["publisher"]}
        isbn = re.sub(r"\D", "", b.get("isbn") or "")
        if len(isbn) == 13:
            product["isbn"] = isbn
            product["gtin13"] = isbn
        elif len(isbn) == 10:
            product["isbn"] = isbn
        product = {k: v for k, v in product.items() if v}
        crumbs_ld = {
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(u)} for i, (t, u) in enumerate(crumbs)],
        }
        by = f" van {b['author']}" if b.get("author") else ""
        short = (b.get("short") or "").strip()
        short = (short if short[-1:] in ".!?" else short + ".") if short else ""
        desc = trim(f"{b['title']}{by}. {short} Tweedehands, {eur(b['price_cents'])}. Snel verzonden.", 158)
        return self.layout(title=trim(f"{b['title']}{by}", 70), desc=desc, path=path, body=body, ld=[product, crumbs_ld],
                           image=image_urls[0].replace(cfg["base_url"], "") if image_urls else None, og_type="product")

    def search_page(self):
        body = ('<h1>Zoeken</h1><p id="info" role="status"></p><ul class="grid" id="results"></ul>'
                '<p><button type="button" id="more" hidden>Meer resultaten</button></p>')
        return self.layout(title="Zoeken", desc="Zoek in ons aanbod van tweedehands boeken.", path="/zoeken/", body=body,
                           robots="noindex,follow")

    def cart_page(self):
        body = '<h1>Winkelwagen</h1><div id="cart"><noscript><p>Voor de winkelwagen is JavaScript nodig.</p></noscript></div>'
        return self.layout(title="Winkelwagen", desc="Je winkelwagen.", path="/winkelwagen/", body=body, robots="noindex,nofollow")

    def checkout_page(self):
        countries = "".join(f'<option value="{esc(c)}">{esc({"NL": "Nederland", "BE": "België", "DE": "Duitsland"}.get(c, c))}</option>'
                            for c in (self.cfg["countries"] or ["NL"]))
        body = (
            '<h1>Afrekenen</h1><p id="sum"></p>'
            '<form id="checkout" class="f" method="post" action="#">'
            '<label>Naam<input name="name" required maxlength="100" autocomplete="name"></label>'
            '<label>E-mailadres<input name="email" type="email" required maxlength="150" autocomplete="email"></label>'
            '<label>Straat en huisnummer<input name="street" required maxlength="120" autocomplete="street-address"></label>'
            '<div class="row"><label>Postcode<input name="postcode" required maxlength="10" autocomplete="postal-code"></label>'
            '<label>Plaats<input name="city" required maxlength="80" autocomplete="address-level2"></label></div>'
            f'<label>Land<select name="country">{countries}</select></label>'
            '<label>Opmerking (optioneel)<textarea name="note" rows="2" maxlength="300"></textarea></label>'
            '<div class="hp" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>'
            '<p>Door te betalen ga je akkoord met de <a href="/voorwaarden/">voorwaarden</a>. Je rekent veilig af via Mollie (o.a. iDEAL).</p>'
            '<button type="submit">Betalen</button><p id="msg" class="msg" hidden role="status"></p></form>'
        )
        return self.layout(title="Afrekenen", desc="Afrekenen.", path="/afrekenen/", body=body, robots="noindex,nofollow")

    def thanks_page(self):
        body = '<h1>Bestelling</h1><p id="thanks" role="status">Even geduld...</p><p><a href="/">Verder winkelen</a></p>'
        return self.layout(title="Bestelling", desc="Status van je bestelling.", path="/bedankt/", body=body, robots="noindex,nofollow")

    def content_page(self, title, path, inner):
        return self.layout(title=title, desc=title, path=path, body=f"<h1>{esc(title)}</h1>{inner}", scripts=True)

    def not_found(self):
        body = ('<h1>Pagina niet gevonden</h1><p>Dit boek is mogelijk verkocht of de pagina bestaat niet meer. '
                'Zoek hierboven verder of kies een categorie.</p>')
        return self.layout(title="Pagina niet gevonden", desc="Pagina niet gevonden.", path="/404.html", body=body,
                           robots="noindex", show_cats=True)

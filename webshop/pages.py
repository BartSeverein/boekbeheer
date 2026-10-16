"""
webshop/pages.py — de HTML van alle pagina's, per taal. Bewust kaal: geen frameworks, geen inline scripts (alleen JSON-LD),
één klein stijlbestand en één klein scriptbestand met een versie in de bestandsnaam (jaar lang te cachen).

Alle paden die hier binnenkomen zijn Nederlandse paden (/boek/x-1/); `Site.L()` zet ze om naar het pad in de taal van de site.
"""
import html
import json
import re
from urllib.parse import quote

from . import i18n
from .common import eur
from .icons import icon


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
    def __init__(self, cfg, css_name, js_name, categories, logo=None, lang="nl", tagline=None, more_cats=0):
        self.cfg = cfg
        self.logo = logo  # (bestandsnaam, breedte, hoogte) of None
        self.css = css_name
        self.js = js_name
        self.categories = categories  # lijst van dicts: label, slug, count, (href: Nederlands pad)
        self.lang = lang
        self.langs = cfg["langs"]
        self.tagline = tagline or i18n.t("p_tagline", lang)
        self.more_cats = more_cats  # aantal categorieën buiten de wolk (0 = geen knop "Overige categorieën")

    # ---- hulp
    def t(self, key, **kw):
        return i18n.t(key, self.lang, **kw)

    def L(self, path):
        return i18n.localize(path, self.lang)

    def url(self, path):
        return self.cfg["base_url"] + path

    def eur(self, cents):
        return eur(cents, self.lang)

    # ---- gedeeld skelet
    def layout(self, *, title, desc, path, body, robots=None, image=None, og_type="website", ld=None, scripts=True,
               show_cats=False):
        """`path` is het Nederlandse pad van deze pagina; canonical en hreflang worden daaruit afgeleid."""
        cfg = self.cfg
        lang = self.lang
        my_path = self.L(path)
        full_title = title if title.endswith(cfg["name"]) else f"{title} | {cfg['name']}"
        head = [
            f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="color-scheme" content="light">',
            f"<title>{esc(full_title)}</title>",
            f'<meta name="description" content="{esc(trim(desc, 158))}">',
            f'<link rel="canonical" href="{esc(self.url(my_path))}">',
        ]
        if robots:
            head.append(f'<meta name="robots" content="{robots}">')
        elif len(self.langs) > 1 and path != "/404.html":
            for l in self.langs:
                head.append(f'<link rel="alternate" hreflang="{l}" href="{esc(self.url(i18n.localize(path, l)))}">')
            head.append(f'<link rel="alternate" hreflang="x-default" href="{esc(self.url(path))}">')
        head += [
            f'<meta property="og:title" content="{esc(trim(title, 90))}">',
            f'<meta property="og:description" content="{esc(trim(desc, 158))}">',
            f'<meta property="og:type" content="{og_type}">',
            f'<meta property="og:url" content="{esc(self.url(my_path))}">',
            f'<meta property="og:site_name" content="{esc(cfg["name"])}">',
            f'<meta property="og:locale" content="{i18n.LOCALE[lang]}">',
        ]
        if image:
            head.append(f'<meta property="og:image" content="{esc(self.url(image))}">')
        head.append(f'<link rel="stylesheet" href="/{self.css}">')
        for block in ld or []:
            head.append(f'<script type="application/ld+json">{jsonld(block)}</script>')
        head.append("</head>")
        paths = json.dumps({"book": self.L("/boek/"), "checkout": self.L("/afrekenen/"), "home": self.L("/")}, separators=(",", ":"))
        cs = json.dumps(cfg["shipping_rates"], separators=(",", ":"))
        data = (f'data-extra="{cfg["extra_per_additional_book_cents"]}" data-free="{cfg["free_shipping_from_cents"]}" '
                f'data-def="{cfg["default_shipping_cents"]}" data-p="{esc(paths)}" data-rt="{esc(cs)}"')
        cats = ""
        if show_cats and self.categories:
            cats = f'<nav class="cats" aria-label="{esc(self.t("p_cats"))}"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">' + "".join(
                f'<li><a href="{self.L(c.get("href") or "/categorie/" + c["slug"] + "/")}">{esc(c["label"])} ({c["count"]})</a></li>'
                for c in self.categories
            ) + (f'<li><a class="more il" href="{self.L("/overige-categorieen/")}">{icon("list")}{esc(self.t("p_more_cats"))} ({self.more_cats})</a></li>'
                 if self.more_cats else "") + "</ul></nav>"
            # Op mobiel achter een knop (alleen CSS: selectievakje + label, werkt zonder JavaScript); op een computer staat de wolk gewoon open.
            cats = (f'<div class="catbox"><input type="checkbox" id="catt" class="sr"><label for="catt" class="il">{icon("list")}'
                    f'{esc(self.t("p_browse_cats"))}</label>{cats}</div>')
        if self.logo:
            brand = f'<img src="/{self.logo[0]}" width="{self.logo[1]}" height="{self.logo[2]}" alt="{esc(cfg["name"])}">'
        else:
            brand = esc(cfg["name"])
        switch = ""
        if len(self.langs) > 1:
            items = []
            for l in self.langs:
                href = i18n.localize("/" if path == "/404.html" else path, l)
                cur = ' aria-current="true"' if l == lang else ""
                items.append(f'<a href="{href}" hreflang="{l}" lang="{l}" title="{esc(i18n.LANG_NAME[l])}"{cur}>{i18n.LANG_LABEL[l]}</a>')
            switch = f'<nav class="lang" aria-label="{esc(self.t("p_lang"))}">{"".join(items)}</nav>'
        header = (
            f'<body {data}>{switch}<header><a class="brand" href="{self.L("/")}">{brand}</a>'
            f'<a href="{self.L("/zoeken/")}" id="sbtn" class="sbtn" aria-label="{esc(self.t("p_search"))}" aria-controls="sform" aria-expanded="false">{icon("search")}</a>'
            f'<form id="sform" action="{self.L("/zoeken/")}" method="get" role="search">'
            f'<input type="search" name="q" placeholder="{esc(self.t("p_search_ph"))}" aria-label="{esc(self.t("p_search"))}" minlength="2" required>'
            f'<button type="submit" aria-label="{esc(self.t("p_search_btn"))}" title="{esc(self.t("p_search_btn"))}">{icon("search")}</button>'
            f'<button type="button" class="sclose ghost" aria-label="{esc(self.t("p_close"))}">{icon("close")}</button></form>'
            f'<a href="{self.L("/winkelwagen/")}" id="cartlink" class="il" rel="nofollow">{icon("cart")}<span id="cartlabel">{esc(self.t("p_cart"))}</span><span id="cartn" class="cn"></span></a></header><main>'
        )
        footer = (
            f'</main><footer><a href="{self.L("/voorwaarden/")}" class="il">{icon("info")}{esc(self.t("p_terms"))}</a>'
            f'<a href="{self.L("/privacy/")}" class="il">{icon("privacy")}{esc(self.t("p_privacy"))}</a>'
            + (f'<a href="mailto:{esc(cfg["contact_email"])}" class="il">{icon("mail")}{esc(cfg["contact_email"])}</a>' if cfg["contact_email"] else "")
            + f'<span class="il">{icon("copyright")}2001-{cfg["year"]}, {esc(cfg["copyright_name"])}</span>'
            + "</footer>"
        )
        script = f'<script src="/{self.js}" defer></script>' if scripts else ""
        if "<!--CATS-->" in body:   # de beginpagina zet de categorieën tussen "Nieuw binnen" en "Over mij"
            body, cats = body.replace("<!--CATS-->", cats), ""
        return "".join(head) + header + cats + body + footer + script + "</body></html>"

    # ---- kaartjes en paginering ----
    def card(self, b):
        img = ""
        if b.get("thumb"):
            p, w, h = b["thumb"]
            img = f'<img src="{p}" width="{w}" height="{h}" alt="" loading="lazy" decoding="async">'
        author = f"<p>{esc(b['author'])}</p>" if b.get("author") else ""
        return (f'<li class="card"><a href="{self.L(b["path"])}"><div class="ph">{img}</div><h3>{esc(b["title"])}</h3>'
                f'{author}<p class="pr">{self.eur(b["price_cents"])}</p></a></li>')

    def grid(self, books):
        return '<ul class="grid">' + "".join(self.card(b) for b in books) + "</ul>"

    def pager(self, base, page, pages):
        if pages <= 1:
            return ""
        seg = f"{base}pagina/"
        prev_href = self.L(base if page == 2 else f"{seg}{page - 1}/")
        prev = f'<a rel="prev" class="il" href="{prev_href}">{icon("prev")}{esc(self.t("p_prev"))}</a>' if page > 1 else ""
        nxt = f'<a rel="next" class="il" href="{self.L(f"{seg}{page + 1}/")}">{esc(self.t("p_next"))}{icon("next")}</a>' if page < pages else ""
        return (f'<nav class="pager" aria-label="{esc(self.t("p_paging"))}">{prev}'
                f'<span>{esc(self.t("p_page_of", p=page, n=pages))}</span>{nxt}</nav>')

    def _crumbs(self, crumbs):
        last = len(crumbs) - 1
        items = []
        for i, (t, u) in enumerate(crumbs):
            if i < last:
                items.append(f'<a href="{self.L(u)}" class="il">{icon("home") if i == 0 else ""}{esc(t)}</a>')
            else:
                items.append(f'<span>{esc(trim(t, 60))}</span>')
        sep = '<span class="sep" aria-hidden="true">›</span>'
        return f'<nav class="crumb" aria-label="{esc(self.t("p_crumb"))}">' + sep.join(items) + "</nav>"

    # ---- pagina's ----
    def home(self, newest):
        cfg = self.cfg
        body = (f'<div class="hc"><h1 class="sr">{esc(cfg["name"])}</h1><h2>{esc(self.t("p_newest"))}</h2>' + self.grid(newest)
                + "<!--CATS-->" + f'<section class="aboutme"><h2>{esc(self.t("p_about_me"))}</h2><p>{esc(self.t("p_about_me_text"))}</p></section></div>')
        ld = [{
            "@context": "https://schema.org", "@type": "BookStore", "name": cfg["name"], "url": self.url(self.L("/")),
            "description": self.tagline,
            **({"logo": self.url("/" + self.logo[0])} if self.logo else {}),
        }, {
            "@context": "https://schema.org", "@type": "WebSite", "name": cfg["name"], "url": self.url(self.L("/")),
            "inLanguage": self.lang,
        }]
        return self.layout(title=f"{cfg['name']} - {self.t('p_title_suffix')}", desc=self.tagline, path="/", body=body, ld=ld,
                           show_cats=True)

    def more_categories_page(self, rest):
        """Alle categorieën die niet in de wolk op de beginpagina staan (de kleinste), gesorteerd op aantal boeken."""
        title = self.t("p_more_cats")
        nav = self._crumbs([(self.t("p_home"), "/"), (title, "/overige-categorieen/")])
        items = "".join(
            f'<li><a href="{self.L(c.get("href") or "/categorie/" + c["slug"] + "/")}">{esc(c["label"])} ({c["count"]})</a></li>' for c in rest)
        body = nav + f'<h1>{esc(title)}</h1><p>{esc(self.t("p_more_cats_intro"))}</p><nav class="cats" aria-label="{esc(title)}"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">{items}</ul></nav>'
        return self.layout(title=title, desc=self.t("p_more_cats_intro"), path="/overige-categorieen/", body=body)

    def category(self, *, label, parent, base, books, page, pages, total):
        crumbs = [(self.t("p_home"), "/")]
        if parent:
            crumbs.append((parent["label"], f"/categorie/{parent['slug']}/"))
        crumbs.append((label, base))
        nav = self._crumbs(crumbs)
        suffix = " " + self.t("p_page_n", n=page) if page > 1 else ""
        path = base if page == 1 else f"{base}pagina/{page}/"
        body = nav + f"<h1>{esc(label)}{esc(suffix)}</h1><p>{esc(self.t('p_in_cat', total=total))}</p>" + \
            self.grid(books) + self.pager(base, page, pages)
        ld = [{
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(self.L(u))} for i, (t, u) in enumerate(crumbs)],
        }, {
            "@context": "https://schema.org", "@type": "CollectionPage", "name": f"{label}{suffix}", "url": self.url(self.L(path)),
            "inLanguage": self.lang,
            "isPartOf": {"@type": "WebSite", "name": self.cfg["name"], "url": self.url(self.L("/"))},
        }]
        desc = self.t("p_cat_desc", label=label, total=total, suffix=suffix, shop=self.cfg["name"])
        return self.layout(title=f"{label}{suffix} - {self.t('p_title_suffix')}", desc=desc, path=path, body=body, ld=ld)

    def book(self, b, loc, category_chain):
        """b: boek; loc: teksten in deze taal {short, long, short_fb, long_fb, category_label, language}; fb = Nederlandse terugval."""
        cfg = self.cfg
        path = b["path"]
        crumbs = [(self.t("p_home"), "/")] + category_chain + [(b["title"], path)]
        nav = self._crumbs(crumbs)
        imgs = b.get("images") or []
        gallery = ""
        if imgs:
            p, w, h = imgs[0]["detail"]
            # Klik op een klein plaatje: het wisselt van plek met de grote foto (c.js). Zonder script opent het plaatje gewoon groot.
            gallery = (f'<img class="mi" src="{p}" width="{w}" height="{h}" data-w="{w}" data-h="{h}" alt="{esc(b["title"])}" fetchpriority="high" decoding="async">')
            if len(imgs) > 1:
                gallery += '<div class="thumbs">' + "".join(
                    f'<a href="{i["detail"][0]}" aria-label="{esc(self.t("p_show_photo", n=n))}"><img src="{i["detail"][0]}" width="{i["thumb"][1]}" height="{i["thumb"][2]}" '
                    f'data-w="{i["detail"][1]}" data-h="{i["detail"][2]}" alt="" loading="lazy" decoding="async"></a>'
                    for n, i in enumerate(imgs[1:], 2)) + "</div>"
        nl_attr = ' lang="nl"'
        rows = []
        isbn = b.get("isbn")
        if isbn and not isbn.strip("0"):   # "0" (of alleen nullen) betekent: geen ISBN
            isbn = self.t("p_none")
        for key, value, fb in (("l_author", b.get("author"), False), ("l_publisher", b.get("publisher"), False),
                               ("l_isbn", isbn, False), ("l_lang", loc.get("language"), False),
                               ("l_notes", loc.get("short"), loc.get("short_fb")), ("l_cat", loc.get("category_label"), False)):
            if value:
                rows.append(f"<dt>{esc(self.t(key))}</dt><dd{nl_attr if fb and self.lang != 'nl' else ''}>{esc(value)}</dd>")
        desc_html = "".join(f"<p>{esc(p)}</p>" for p in re.split(r"\n\s*\n|\r\n\s*\r\n", (loc.get("long") or "").strip()) if p.strip())
        if desc_html and loc.get("long_fb") and self.lang != "nl":
            desc_html = f'<div{nl_attr}>{desc_html}</div>'
        ship = self.eur(b["ship_cents"])
        stock_note = self.t("p_copies", n=b["amount"]) if b["amount"] > 1 else ""
        plus = self.t("p_plus") if cfg["extra_per_additional_book_cents"] else ""
        abroad = f' {esc(self.t("p_abroad"))}' if len(cfg["countries"]) > 1 else ""
        buy = (
            f'<p class="price">{self.eur(b["price_cents"])}</p><p class="ship il">{icon("truck")}<span>{esc(self.t("p_in_stock"))}{esc(stock_note)}. '
            f'{esc(self.t("p_ship_line", ship=ship, plus=plus))}{abroad}</span></p>'
            f'<p><button type="button" class="add" data-id="{b["id"]}" data-title="{esc(b["title"])}" data-price="{b["price_cents"]}" '
            f'data-ship="{b["ship_cents"]}" data-big="{b["big"]}" data-max="{b["amount"]}" data-url="{path}">{icon("cart_plus")}<span>{esc(self.t("p_add"))}</span></button></p>'
            f'<p id="added" hidden><a href="{self.L("/winkelwagen/")}" class="il">{icon("cart")}{esc(self.t("p_view_cart"))}</a></p>'
        )
        my_url = self.url(self.L(path))
        share_text = quote(self.t("p_share_msg") + my_url, safe="")
        share = (
            f'<p class="share">{icon("share")} {esc(self.t("p_share_lead"))} '
            f'<a href="https://wa.me/?text={share_text}" target="_blank" rel="noopener">{icon("whatsapp")} {esc(self.t("p_share_wa"))}</a> '
            f'<a href="mailto:?subject={quote(b["title"], safe="")}&amp;body={share_text}">{icon("mail_open")} {esc(self.t("p_share_mail"))}</a> '
            f'<a href="{esc(my_url)}" data-copy="{esc(self.t("p_share_msg") + my_url)}">{icon("link")} <span>{esc(self.t("p_share_link"))}</span></a></p>'
        )
        body = nav + f"<article class=\"book\"><div>{gallery}</div><div><h1>{esc(b['title'])}</h1>" + \
            (f"<p>{esc(b['author'])}</p>" if b.get("author") else "") + buy + (f"<dl>{''.join(rows)}</dl>" if rows else "") + \
            (f"<h2>{esc(self.t('p_about_nl' if loc.get('long_fb') and self.lang != 'nl' else 'p_about'))}</h2>{desc_html}" if desc_html else "") + share + "</div></article>"

        image_urls = [self.url(i["detail"][0]) for i in imgs]
        product = {
            "@context": "https://schema.org", "@type": ["Product", "Book"], "@id": my_url + "#boek",
            "name": b["title"], "url": my_url, "sku": str(b["id"]),
            "description": trim(loc.get("long") or loc.get("short") or b["title"], 300),
            "inLanguage": b.get("lang_code") or None,
            "offers": {
                "@type": "Offer", "url": my_url, "priceCurrency": "EUR", "price": f"{b['price_cents'] / 100:.2f}",
                "availability": "https://schema.org/InStock", "itemCondition": "https://schema.org/UsedCondition",
                "seller": {"@type": "Organization", "name": cfg["name"], "url": self.url(self.L("/"))},
                "shippingDetails": {
                    "@type": "OfferShippingDetails",
                    "shippingRate": {"@type": "MonetaryAmount", "value": f"{b['ship_cents'] / 100:.2f}", "currency": "EUR"},
                    "shippingDestination": {"@type": "DefinedRegion", "addressCountry": "NL"},
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
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(self.L(u))} for i, (t, u) in enumerate(crumbs)],
        }
        by = self.t("p_by", author=b["author"]) if b.get("author") else ""
        short = (loc.get("short") or "").strip()
        short = (short if short[-1:] in ".!?" else short + ".") if short else ""
        desc = trim(f"{b['title']}{by}. {short} {self.t('p_meta_book', price=self.eur(b['price_cents']))}", 158)
        return self.layout(title=trim(f"{b['title']}{by}", 70), desc=desc, path=path, body=body, ld=[product, crumbs_ld],
                           image=image_urls[0].replace(cfg["base_url"], "") if image_urls else None, og_type="product")

    def search_page(self):
        body = (f'<h1>{esc(self.t("p_search"))}</h1><p id="info" role="status"></p><ul class="grid" id="results"></ul>'
                f'<p><button type="button" id="more" hidden>{esc(self.t("p_more"))}</button></p>')
        return self.layout(title=self.t("p_search"), desc=self.t("p_search_desc"), path="/zoeken/", body=body, robots="noindex,follow")

    def cart_page(self):
        body = (f'<h1>{esc(self.t("p_cart"))}</h1><div id="cart"><noscript><p>{esc(self.t("p_nojs"))}</p></noscript></div>')
        return self.layout(title=self.t("p_cart"), desc=self.t("p_cart_desc"), path="/winkelwagen/", body=body, robots="noindex,nofollow")

    def checkout_page(self):
        countries = "".join(f'<option value="{esc(c)}">{esc(i18n.country_name(c, self.lang))}</option>'
                            for c in (self.cfg["countries"] or ["NL"]))
        agree = esc(self.t("p_agree")).replace("{a}", f'<a href="{self.L("/voorwaarden/")}">').replace("{/a}", "</a>")
        t = self.t
        body = (
            f'<h1>{esc(t("p_checkout"))}</h1><p id="sum"></p>'
            '<form id="checkout" class="f" method="post" action="#">'
            f'<label>{esc(t("f_name"))}<input name="name" required maxlength="100" autocomplete="name"></label>'
            f'<label>{esc(t("f_email"))}<input name="email" type="email" required maxlength="150" autocomplete="email"></label>'
            f'<label>{esc(t("f_street"))}<input name="street" required maxlength="120" autocomplete="street-address"></label>'
            f'<div class="row"><label>{esc(t("f_postcode"))}<input name="postcode" required maxlength="10" autocomplete="postal-code"></label>'
            f'<label>{esc(t("f_city"))}<input name="city" required maxlength="80" autocomplete="address-level2"></label></div>'
            f'<label>{esc(t("f_country"))}<select name="country">{countries}</select></label>'
            f'<label>{esc(t("f_note"))}<textarea name="note" rows="2" maxlength="300"></textarea></label>'
            f'<div class="hp" aria-hidden="true"><label>{esc(t("f_website"))}<input name="website" tabindex="-1" autocomplete="off"></label></div>'
            f'<p>{agree}</p>'
            f'<button type="submit">{icon("lock")}{esc(t("f_pay"))}</button><p id="msg" class="msg" hidden role="status"></p></form>'
        )
        return self.layout(title=t("p_checkout"), desc=t("p_checkout"), path="/afrekenen/", body=body, robots="noindex,nofollow")

    def thanks_page(self):
        body = (f'<h1>{esc(self.t("p_order"))}</h1><p id="thanks" role="status">{esc(self.t("js_wait"))}</p>'
                f'<p><a href="{self.L("/")}" class="il">{icon("back")}{esc(self.t("p_keep_shopping"))}</a></p>')
        return self.layout(title=self.t("p_order"), desc=self.t("p_order_desc"), path="/bedankt/", body=body, robots="noindex,nofollow")

    def content_page(self, title, path, inner):
        return self.layout(title=title, desc=title, path=path, body=f"<h1>{esc(title)}</h1>{inner}", scripts=True)

    def not_found(self):
        """Eén 404-pagina voor alle talen (de server kent maar één ErrorDocument)."""
        body = f"<h1>{esc(i18n.t('p_404_title', 'nl'))}</h1>" + "".join(
            f'<p lang="{l}">{esc(i18n.t("p_404_text", l))}</p>' for l in i18n.LANGS if l in self.langs)
        return self.layout(title=i18n.t("p_404_title", "nl"), desc=i18n.t("p_404_title", "nl"), path="/404.html", body=body,
                           robots="noindex", show_cats=True)

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
        self.err_img = None  # foto voor de 404-pagina: (bestandsnaam, breedte, hoogte)
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

    def free_amount(self):
        """Grens voor gratis verzending als tekst zonder ',00' (€ 50); None als die uit staat of Nederland niet in de lijst staat."""
        n = int(self.cfg.get("free_shipping_from_cents") or 0)
        if self.lang != "nl":   # alleen op de Nederlandse pagina's (de regel geldt alleen voor Nederland)
            return None
        if n <= 0 or "NL" not in (self.cfg.get("countries") or ["NL"]):
            return None
        t = self.eur(n)
        for tail in (",00", ".00"):
            t = t.replace(tail, "")
        return t

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
        head += [
            f'<meta name="twitter:card" content="{"summary_large_image" if image and og_type == "product" else "summary"}">',
            f'<meta name="twitter:title" content="{esc(trim(title, 70))}">',
            f'<meta name="twitter:description" content="{esc(trim(desc, 200))}">',
        ]
        if image:
            head.append(f'<meta name="twitter:image" content="{esc(self.url(image))}">')
        head += ['<link rel="icon" href="/favicon.ico" sizes="48x48">',
                 '<link rel="icon" href="/icon-192.png" type="image/png" sizes="192x192">',
                 '<link rel="apple-touch-icon" href="/apple-touch-icon.png">']
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
            cats = self.cats_nav()
            # Op mobiel achter een knop (alleen CSS: selectievakje + label, werkt zonder JavaScript); op een computer staat de wolk gewoon open.
            cats = (f'<div class="catbox"><input type="checkbox" id="catt" class="sr"><label for="catt" class="il">{icon("list")}'
                    f'{esc(self.t("p_browse_cats"))}</label>{cats}</div>')
        if self.logo:
            brand = f'<img src="/{self.logo[0]}" width="{self.logo[1]}" height="{self.logo[2]}" alt="{esc(cfg["name"])}">'
        else:
            brand = esc(cfg["name"])
        free = self.free_amount()
        promo = f'<div class="promo">{esc(self.t("p_free_bar", amount=free))}</div>' if free else ""
        switch = ""
        if len(self.langs) > 1:
            items = []
            for l in self.langs:
                href = i18n.localize("/" if path == "/404.html" else path, l)
                cur = ' aria-current="true"' if l == lang else ""
                items.append(f'<a href="{href}" hreflang="{l}" lang="{l}" title="{esc(i18n.LANG_NAME[l])}"{cur}>{i18n.LANG_LABEL[l]}</a>')
            switch = f'<nav class="lang" aria-label="{esc(self.t("p_lang"))}">{"".join(items)}</nav>'
        header = (
            f'<body {data}>{promo}{switch}<header><a class="brand" href="{self.L("/")}">{brand}</a>'
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
    def card(self, b, eager=False):
        img = ""
        if b.get("thumb"):
            p, w, h = b["thumb"]
            # De eerste kaartjes staan direct in beeld: die laden meteen (anders wachten ze op de browser en verschijnen de foto's een voor een).
            load = 'fetchpriority="high" decoding="async"' if eager else 'loading="lazy" decoding="async"'
            img = f'<img src="{p}" width="{w}" height="{h}" alt="" {load}>'
        author = f"<p>{esc(b['author'])}</p>" if b.get("author") else ""
        return (f'<li class="card"><a href="{self.L(b["path"])}"><div class="ph">{img}</div><h3>{esc(b["title"])}</h3>'
                f'{author}<p class="pr">{self.eur(b["price_cents"])}</p></a></li>')

    def grid(self, books, nolazy=False):
        return '<ul class="grid">' + "".join(self.card(b, eager=(i < 4 and not nolazy)) for i, b in enumerate(books)) + "</ul>"

    def pager(self, base, page, pages):
        if pages <= 1:
            return ""
        seg = f"{base}pagina/"
        prev_href = self.L(base if page == 2 else f"{seg}{page - 1}/")
        prev = f'<a class="il" href="{prev_href}">{icon("prev")}{esc(self.t("p_prev"))}</a>' if page > 1 else ""
        nxt = f'<a class="il" href="{self.L(f"{seg}{page + 1}/")}">{esc(self.t("p_next"))}{icon("next")}</a>' if page < pages else ""
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
            "@context": "https://schema.org", "@type": "BookStore", "name": cfg["name"], "alternateName": "Severein", "url": self.url(self.L("/")),
            "description": self.tagline,
            **({"logo": self.url("/" + self.logo[0])} if self.logo else {}),
        }, {
            "@context": "https://schema.org", "@type": "WebSite", "name": cfg["name"], "alternateName": "Severein", "url": self.url(self.L("/")),
            "inLanguage": self.lang,
        }]
        return self.layout(title=self.t("p_home_title"), desc=self.tagline, path="/", body=body, ld=ld,
                           show_cats=True, image=("/" + self.logo[0]) if self.logo else None)

    def cats_nav(self):
        """De categorieknoppen (in de taal van deze pagina) als losse navigatie."""
        return (f'<nav class="cats" aria-label="{esc(self.t("p_cats"))}"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">' + "".join(
            self._cat_li(c) for c in self.categories
        ) + (f'<li><a class="more il" href="{self.L("/overige-categorieen/")}">{icon("list")}{esc(self.t("p_more_cats"))}</a></li>'
             if self.more_cats else "") + "</ul></nav>")

    def _cat_li(self, c):
        """Knop voor een categorie; leidt hij naar subcategorieën, dan staat het lijst-icoon ervoor."""
        href = self.L(c.get("href") or "/categorie/" + c["slug"] + "/")
        if c.get("has_subs"):
            return f'<li><a class="il subs" href="{href}">{icon("list")}{esc(c["label"])} ({c["count"]})</a></li>'
        return f'<li><a href="{href}">{esc(c["label"])} ({c["count"]})</a></li>'

    def more_categories_page(self, rest):
        """Alle hoofdcategorieën op alfabet (rest = lijst met label, slug, count); een categorie met subcategorieën opent een eigen pagina met knoppen."""
        title = self.t("p_more_cats")
        nav = self._crumbs([(self.t("p_home"), "/"), (title, "/overige-categorieen/")])
        items = "".join(
            self._cat_li(c) for c in rest)
        body = nav + f'<h1>{esc(title)}</h1><p>{esc(self.t("p_more_cats_intro"))}</p><nav class="cats" aria-label="{esc(title)}"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">{items}</ul></nav>'
        return self.layout(title=title, desc=self.t("p_more_cats_intro"), path="/overige-categorieen/", body=body)

    def category(self, *, label, parent, base, books, page, pages, total, intro="", see_also=None, subs=None, all_total=None):
        crumbs = [(self.t("p_home"), "/")]
        if parent:
            crumbs.append((parent["label"], f"/categorie/{parent['slug']}/"))
        crumbs.append((label, base))
        nav = self._crumbs(crumbs)
        suffix = " " + self.t("p_page_n", n=page) if page > 1 else ""
        path = base if page == 1 else f"{base}pagina/{page}/"
        extra = ""
        if page == 1:
            if parent:
                extra += f'<p><a href="{self.L("/categorie/" + parent["slug"] + "/")}" class="il">{icon("back")}{esc(self.t("p_cat_parent", parent=parent["label"]))}</a></p>'
            if see_also:
                links = ", ".join(f'<a href="{self.L(u)}">{esc(t)}</a>' for t, u in see_also)
                extra += f'<p>{self.t("p_see_also", list=links)}</p>'
        if subs:
            # Hoofdcategorie met subcategorieën: eerst de knoppen (op alfabet), daaronder alleen de boeken zonder subcategorie.
            sub_nav = (f'<nav class="cats" aria-label="{esc(self.t("p_cats"))}"><ul class="cats" style="display:contents;list-style:none;padding:0;margin:0">'
                       + "".join(f'<li><a href="{self.L(u)}">{esc(t)} ({n})</a></li>' for t, u, n in subs) + "</ul></nav>")
            own = (f'<h2>{esc(self.t("p_own_books", label=label, total=total))}</h2>' + self.grid(books) + self.pager(base, page, pages)) if total else ""
            body = nav + f"<h1>{esc(label)}{esc(suffix)}</h1><p>{(esc(intro) + ' ') if intro and page == 1 else ''}</p>" + extra + sub_nav + own
        else:
            body = nav + f"<h1>{esc(label)}{esc(suffix)}</h1><p>{(esc(intro) + ' ') if intro and page == 1 else ''}{esc(self.t('p_in_cat_one') if total == 1 else self.t('p_in_cat', total=total))}</p>" + extra + \
                self.grid(books) + self.pager(base, page, pages)
        total = all_total if all_total is not None else total   # titel en beschrijving noemen het totaal van de hele categorie
        ld = [{
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(self.L(u))} for i, (t, u) in enumerate(crumbs)],
        }, {
            "@context": "https://schema.org", "@type": "CollectionPage", "name": f"{label}{suffix}", "url": self.url(self.L(path)),
            "inLanguage": self.lang,
            "isPartOf": {"@type": "WebSite", "name": self.cfg["name"], "url": self.url(self.L("/"))},
        }]
        desc = trim(f"{intro} {suffix}".strip(), 158) if intro else self.t("p_cat_desc", label=label, total=total, suffix=suffix, shop=self.cfg["name"])
        return self.layout(title=self.t("p_cat_title", label=f"{label} {suffix}".strip(), total=total),
                           desc=desc, path=path, body=body, ld=ld)

    def author_page(self, *, name, slug, books, page, pages, total, intro=""):
        base = f"/auteur/{slug}/"
        if page > 1:
            intro = ""   # de tekst over de auteur staat alleen op de eerste pagina
        crumbs = [(self.t("p_home"), "/"), (name, base)]
        nav = self._crumbs(crumbs)
        suffix = " " + self.t("p_page_n", n=page) if page > 1 else ""
        path = base if page == 1 else f"{base}pagina/{page}/"
        h1 = self.t("p_author_h1", author=name)
        body = nav + f"<h1>{esc(h1)}{esc(suffix)}</h1><p>{esc((intro + ' ') if intro else '')}{esc(self.t('p_author_intro', total=total, author=name))}</p>" + \
            self.grid(books) + self.pager(base, page, pages)
        ld = [{
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": t, "item": self.url(self.L(u))} for i, (t, u) in enumerate(crumbs)],
        }, {
            "@context": "https://schema.org", "@type": "CollectionPage", "name": f"{h1}{suffix}", "url": self.url(self.L(path)),
            "inLanguage": self.lang, "about": ({"@type": "Person", "name": name, "description": intro} if intro else {"@type": "Person", "name": name}),
            "isPartOf": {"@type": "WebSite", "name": self.cfg["name"], "url": self.url(self.L("/"))},
        }]
        desc = self.t("p_author_desc", author=name, total=total, suffix=suffix, shop=self.cfg["name"])
        if intro:
            desc = trim(f"{intro} {desc}", 158)
        return self.layout(title=self.t("p_author_title", author=name + suffix, total=total),
                           desc=desc,
                           path=path, body=body, ld=ld)

    def book(self, b, loc, category_chain, sold=False, similar=None, more_by=None):
        """b: boek; loc: teksten in deze taal {short, long, short_fb, long_fb, category_label, language}; fb = Nederlandse terugval."""
        cfg = self.cfg
        path = b["path"]
        crumbs = [(self.t("p_home"), "/")] + category_chain + [(b["title"], path)]
        nav = self._crumbs(crumbs)
        imgs = b.get("images") or []
        alt_text = b["title"] + (self.t("p_by", author=b["author"]) if b.get("author") else "")
        gallery = ""
        if imgs:
            p, w, h = imgs[0]["detail"]
            # Klik op een klein plaatje: het wisselt van plek met de grote foto (c.js). Zonder script opent het plaatje gewoon groot.
            gallery = (f'<img class="mi" src="{p}" width="{w}" height="{h}" data-w="{w}" data-h="{h}" alt="{esc(alt_text)}" fetchpriority="high" decoding="async">')
            if len(imgs) > 1:
                gallery += '<div class="thumbs">' + "".join(
                    f'<a href="{i["detail"][0]}" aria-label="{esc(self.t("p_show_photo", n=n))}"><img src="{i["detail"][0]}" width="{i["thumb"][1]}" height="{i["thumb"][2]}" '
                    f'data-w="{i["detail"][1]}" data-h="{i["detail"][2]}" alt="{esc(self.t("p_photo_n", n=n, title=alt_text))}" loading="lazy" decoding="async"></a>'
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
        th = b.get("thumb")
        thumb_attr = f' data-img="{esc(th[0])}" data-ir="{th[1] / th[2]:.3f}"' if th and th[2] else ""
        ship = self.eur(b["ship_cents"])
        stock_note = self.t("p_copies", n=b["amount"]) if b["amount"] > 1 else ""
        free = self.free_amount()
        free_line = f'<p class="small">{esc(self.t("p_free_book", amount=free))}</p>' if free else ""
        buy = (
            f'<p class="price">{self.eur(b["price_cents"])}</p><p class="ship il">{icon("truck")}<span>{esc(self.t("p_in_stock"))}{esc(stock_note)}. '
            f'{esc(self.t("p_ship_line" if len(cfg["countries"]) > 1 else "p_ship_line_one", ship=ship))}</span></p>'
            f'{free_line}'
            f'<p><button type="button" class="add" data-id="{b["id"]}" data-title="{esc(b["title"])}" data-price="{b["price_cents"]}" '
            f'data-ship="{b["ship_cents"]}" data-big="{b["big"]}" data-max="{b["amount"]}" data-url="{path}"{thumb_attr}>{icon("cart_plus")}<span>{esc(self.t("p_add"))}</span></button></p>'
            f'<p id="added" hidden><a href="{self.L("/winkelwagen/")}" class="il">{icon("cart")}{esc(self.t("p_view_cart"))}</a></p>'
        )
        cat_href, cat_name = (self.L(category_chain[-1][1]), category_chain[-1][0]) if category_chain else (self.L("/"), cfg["name"])
        sold_html = (f'<p class="ship il">{icon("truck")}<span><strong>{esc(self.t("p_sold"))}</strong></span></p>'
                     f'<p><a href="{cat_href}" class="il">{icon("list")}{esc(self.t("p_sold_more", cat=cat_name))}</a></p>')
        if sold:
            buy = sold_html
        else:
            # Zo lang de pagina nog niet opnieuw is gebouwd, vraagt c.js bij het openen de actuele voorraad op (stock.php)
            # en wisselt bij een verkocht boek meteen naar de verkocht-tekst.
            buy = f'<div id="buy">{buy}</div><div id="soldmsg" hidden>{sold_html}</div>'
        my_url = self.url(self.L(path))
        share_text = quote(self.t("p_share_msg") + my_url, safe="")
        share = (
            f'<p class="share">{icon("share")} {esc(self.t("p_share_lead"))} '
            f'<a href="https://wa.me/?text={share_text}" target="_blank" rel="noopener">{icon("whatsapp")} {esc(self.t("p_share_wa"))}</a>, '
            f'<a href="mailto:?subject={quote(b["title"], safe="")}&amp;body={share_text}">{icon("mail_open")} {esc(self.t("p_share_mail"))}</a> '
            f'{esc(self.t("p_share_of"))} '
            f'<a href="{esc(my_url)}" data-copy="{esc(self.t("p_share_msg") + my_url)}">{icon("link")} <span>{esc(self.t("p_share_link"))}</span></a>.</p>'
        )
        body = nav + f"<article class=\"book\"><div>{gallery}</div><div><h1>{esc(b['title'])}</h1>" + \
            (f"<p>{esc(b['author'])}</p>" if b.get("author") else "") + buy + (f"<dl>{''.join(rows)}</dl>" if rows else "") + \
            (f"<h2>{esc(self.t('p_about_nl' if loc.get('long_fb') and self.lang != 'nl' else 'p_about'))}</h2>{desc_html}" if desc_html else "") + ("" if sold else share) + "</div></article>" + \
            (f'<section class="similar"><h2>{esc(self.t("p_similar"))}</h2>{self.grid(similar, nolazy=True)}</section>' if similar else "") + \
            (f'<section class="similar"><h2>{esc(self.t("p_more_by", author=more_by["name"]))}</h2>{self.grid(more_by["others"])}'
             + (f'<p><a href="{self.L("/auteur/" + more_by["slug"] + "/")}" class="il">{icon("author")}{esc(self.t("p_more_all", total=more_by["total"], author=more_by["name"]))}</a></p>' if more_by.get("slug") else "")
             + '</section>' if more_by and not sold else "")

        image_urls = [self.url(i["detail"][0]) for i in imgs]
        # Verzendkosten per land (zelfde regel als bij het afrekenen: eigen tarief van het land, anders OTHER) en verwerkingstijd.
        rates = cfg["shipping_rates"]
        ship_details = []
        for cc in cfg["countries"]:
            if cc == "NL":
                cents = b["ship_cents"]
            else:
                rate = rates.get(cc) or rates.get("OTHER") or rates.get("NL")
                cents = rate[1 if b["big"] else 0] if rate else b["ship_cents"]
            ship_details.append({
                "@type": "OfferShippingDetails",
                "shippingRate": {"@type": "MonetaryAmount", "value": f"{cents / 100:.2f}", "currency": "EUR"},
                "shippingDestination": {"@type": "DefinedRegion", "addressCountry": cc},
                "deliveryTime": {"@type": "ShippingDeliveryTime",
                                 "handlingTime": {"@type": "QuantitativeValue", "minValue": 0, "maxValue": 1, "unitCode": "DAY"},
                                 # Bezorgduur na verzending: Nederland 1-2 dagen, andere landen 3-5 dagen.
                                 "transitTime": {"@type": "QuantitativeValue", "minValue": 1 if cc == "NL" else 3,
                                                 "maxValue": 2 if cc == "NL" else 5, "unitCode": "DAY"}},
            })
        product = {
            "@context": "https://schema.org", "@type": ["Product", "Book"], "@id": my_url + "#boek",
            "name": b["title"], "url": my_url, "sku": str(b["id"]),
            "description": trim(loc.get("long") or loc.get("short") or b["title"], 300),
            "inLanguage": b.get("lang_code") or None,
            "offers": {
                "@type": "Offer", "url": my_url, "priceCurrency": "EUR", "price": f"{b['price_cents'] / 100:.2f}",
                "availability": "https://schema.org/OutOfStock" if sold else "https://schema.org/InStock", "itemCondition": "https://schema.org/UsedCondition",
                "seller": {"@type": "Organization", "name": cfg["name"], "url": self.url(self.L("/"))},
                "shippingDetails": ship_details,
                "hasMerchantReturnPolicy": {
                    "@type": "MerchantReturnPolicy", "applicableCountry": list(cfg["countries"]),
                    "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow", "merchantReturnDays": 14,
                    "returnMethod": "https://schema.org/ReturnByMail", "returnFees": "https://schema.org/ReturnShippingFees",
                    "merchantReturnLink": self.url(self.L("/voorwaarden/")),
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
        head_txt = f"{b['title']}{by}."
        if sold:
            desc = trim(f"{head_txt} {self.t('p_sold_meta', shop=cfg['name'])}", 158)
        else:
            tail = self.t("p_meta_book", price=self.eur(b["price_cents"]))
            # Middenstuk: de bijzonderheden, anders het begin van de beschrijving; het past precies in wat er van de 158 tekens overblijft.
            mid = short
            if not mid:
                first = re.split(r"\n\s*\n", (loc.get("long") or "").strip())[0]
                mid = re.sub(r"\s+", " ", first).strip()
            room = 158 - len(head_txt) - len(tail) - 2
            mid = trim(mid, room) if mid and room >= 25 else ""
            desc = trim(" ".join(x for x in (head_txt, mid, tail) if x), 158)
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
            f'<div class="code"><label>{esc(t("f_code"))}<input name="code" maxlength="30" autocomplete="off" autocapitalize="characters" spellcheck="false"></label>'
            f'<button type="button" id="codebtn" class="ghost">{esc(t("f_code_apply"))}</button></div><p id="codemsg" class="msg" hidden role="status"></p>'
            f'<label>{esc(t("f_note"))}<textarea name="note" rows="2" maxlength="300"></textarea></label>'
            f'<div class="hp" aria-hidden="true"><label>{esc(t("f_website"))}<input name="website" tabindex="-1" autocomplete="off"></label></div>'
            f'<p>{agree}</p>'
            f'<button type="submit">{icon("cart_heart")}{esc(t("f_pay"))}</button><p id="msg" class="msg" hidden role="status"></p></form>'
        )
        return self.layout(title=t("p_checkout"), desc=t("p_checkout"), path="/afrekenen/", body=body, robots="noindex,nofollow")

    def thanks_page(self):
        body = (f'<h1>{esc(self.t("p_order"))}</h1><p id="thanks" role="status">{esc(self.t("js_wait"))}</p>'
                f'<p><a href="{self.L("/")}" class="il">{icon("back")}{esc(self.t("p_keep_shopping"))}</a></p>')
        return self.layout(title=self.t("p_order"), desc=self.t("p_order_desc"), path="/bedankt/", body=body, robots="noindex,nofollow")

    def content_page(self, title, path, inner):
        return self.layout(title=title, desc=title, path=path, body=f"<h1>{esc(title)}</h1>{inner}", scripts=True)

    def not_found(self, sites=None):
        """Eén 404-pagina voor alle talen (de server kent maar één ErrorDocument): per taal de uitleg met daaronder de categorieën in die taal."""
        sites = sites or {self.lang: self}
        photo = ""
        if self.err_img:
            n, w, h = self.err_img
            photo = f'<p><img class="err" src="/{n}" width="{w}" height="{h}" alt="{esc(i18n.t("p_404_alt", "nl"))}"></p>'
        parts = []
        for l in i18n.LANGS:
            if l not in self.langs:
                continue
            nav = sites[l].cats_nav() if l in sites and sites[l].categories else ""
            flag = {"nl": "🇳🇱", "en": "🇬🇧", "de": "🇩🇪", "fr": "🇫🇷"}.get(l, "")
            parts.append(f'<section lang="{l}"><p>{flag + " " if flag else ""}{esc(i18n.t("p_404_text", l))}</p>{nav}</section>')
        body = photo + f"<h1>{esc(i18n.t('p_404_title', 'nl'))}</h1>" + "".join(parts)
        return self.layout(title=i18n.t("p_404_title", "nl"), desc=i18n.t("p_404_title", "nl"), path="/404.html", body=body,
                           robots="noindex", show_cats=False)

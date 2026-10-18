"""
webshop/authors.py — groepeert boeken per auteur, ondanks wisselende schrijfwijzen ("Voorbeeld, Jan", "Jan Voorbeeld",
"J. Voorbeeld", "Voorbeeld"). Alleen bedoeld voor het blokje "Meer van deze auteur" op de boekpagina's; de gegevens zelf
(in de database en bij Boekwinkeltjes) blijven ongewijzigd.

Voorzichtig samenvoegen: een schrijfwijze met alleen initialen of alleen een achternaam wordt alleen aan een auteur met
een volledige voornaam gekoppeld als er precies ÉÉN zo'n auteur met die achternaam (en die beginletter) bestaat.
Bij twijfel (bijv. vijf verschillende "de Jong") blijft het apart; dan komt er liever geen blokje dan een verkeerd boek.
"""
import re
import unicodedata
from collections import Counter, defaultdict

from .common import slugify

PREFIXES = {"van", "de", "der", "den", "ter", "ten", "te", "het", "von", "vom", "zu", "du", "la", "le", "les",
            "di", "da", "del", "della", "dos", "das", "el", "al", "bin", "ibn", "af", "av", "op", "in"}
UNKNOWN = {"", "geen", "onbekend", "diversen", "anoniem", "anonymous", "div", "e.a.", "ea", "e a", "divers", "n.n.", "nn"}
MAX_SHOWN = 8


def _plain(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()


def _tokens(s):
    """Woorden zonder punten en tussenvoegsels; initialen ('N.D.') worden losse letters."""
    s = _plain(s)
    s = re.sub(r"['’][ts]\b", " ", s)          # 't en 's als tussenvoegsel
    s = re.sub(r"\b[ld]['’]", " ", s)          # l' en d' ("Louis l'Amour")
    s = s.replace("'", "").replace("’", "")     # O'Brien -> obrien
    s = re.sub(r"(?<=\b\w)\.", " ", s).replace(".", " ")
    return [t for t in re.split(r"[\s]+", s) if t and t not in PREFIXES]


def _parse_person(part):
    """'Heiden, Bas van der' / 'Mart Engelen' / 'Straaten' -> (achternaam_sleutel, voornaamdelen, weergavenaam) of None."""
    part = part.strip().strip(";").strip()
    if not part:
        return None
    if part.count(",") > 1:
        return None
    if "," in part:
        last, first = [x.strip() for x in part.split(",", 1)]
        display = f"{first} {last}".strip()
    else:
        words = part.split()
        last = words[-1] if len(words) > 1 else part
        first = " ".join(words[:-1]) if len(words) > 1 else ""
        display = part
    lt = _tokens(last)
    if not lt:
        return None
    return " ".join(lt), _tokens(first), display


def parse_authors(raw):
    """Lijst personen uit een auteursveld ('A / B' = twee personen); lege lijst bij onbekend, 'e.a.' of een kommalijst."""
    raw = (raw or "").strip()
    if _plain(raw).strip() in UNKNOWN or "e.a" in _plain(raw):
        return []
    people = []
    for part in re.split(r"\s*[/;]\s*", raw):
        p = _parse_person(part)
        if p is None:
            return []
        people.append(p)
    return people[:3]


def _is_initial(tokens):
    return bool(tokens) and len(tokens[0]) == 1


def _group(books):
    persons = {}                 # boek-id -> [(last, first_tokens, display)]
    full = defaultdict(set)      # last -> {voornaam (eerste woord)} van auteurs met een volledige voornaam
    for b in books:
        ps = parse_authors(b.get("author"))
        persons[b["id"]] = ps
        for last, first, _ in ps:
            if first and not _is_initial(first):
                full[last].add(first[0])

    def canon(last, first):
        if first and not _is_initial(first):
            return (last, first[0])
        candidates = sorted(f for f in full.get(last, ()) if not first or f.startswith(first[0][0]))
        if len(candidates) == 1:
            return (last, candidates[0])
        return (last, "".join(t[0] for t in first)) if first else (last, "")

    groups = defaultdict(list)
    names = defaultdict(Counter)
    for b in books:
        seen = set()
        for last, first, display in persons[b["id"]]:
            key = canon(last, first)
            if key in seen:
                continue
            seen.add(key)
            groups[key].append(b)
            if first and not _is_initial(first):
                names[key][display] += 1
    return groups, names, (persons, canon)


def build_author_groups(books):
    """Geeft {boek-id: (weergavenaam, [andere boeken, nieuwste eerst])} voor boeken met minstens één ander boek van dezelfde auteur."""
    groups, names, (persons, canon) = _group(books)

    def usable(key):
        """Een groep zonder volledige voornaam (alleen initialen of alleen achternaam) is te onzeker, tenzij er minstens twee initialen zijn."""
        return key in names or len(key[1]) >= 2

    result = {}
    for b in books:
        for last, first, display in persons[b["id"]]:
            key = canon(last, first)
            if not usable(key):
                continue
            others = [x for x in groups[key] if x["id"] != b["id"]]
            if not others:
                continue
            name = names[key].most_common(1)[0][0] if names[key] else display
            others.sort(key=lambda x: x["id"], reverse=True)
            result[b["id"]] = (name, others[:MAX_SHOWN])
            break
    return result


def build_authors(books, min_page=3):
    """
    Geeft (more_by, pages):
      more_by[boek-id] = {"name", "others" (max. 8, nieuwste eerst), "total", "slug" (None als er geen auteurspagina is)}
      pages = [{"slug", "name", "books"}] voor auteurs met minstens 'min_page' boeken (alle boeken, nieuwste eerst).
    """
    groups, names, (persons, canon) = _group(books)

    def usable(key):
        """Een groep zonder volledige voornaam (alleen initialen of alleen achternaam) is te onzeker, tenzij er minstens twee initialen zijn."""
        return key in names or len(key[1]) >= 2

    def name_of(key, fallback):
        return names[key].most_common(1)[0][0] if names[key] else fallback

    slugs, pages, used = {}, [], set()
    for key, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(items) >= min_page and usable(key):
            base = slugify(name_of(key, " ".join(key)))
            slug, n = base, 2
            while slug in used:
                slug, n = f"{base}-{n}", n + 1
            used.add(slug)
            slugs[key] = slug
            pages.append({"slug": slug, "name": name_of(key, " ".join(key)),
                          "books": sorted(items, key=lambda x: x["id"], reverse=True)})
    more_by = {}
    for b in books:
        for last, first, display in persons[b["id"]]:
            key = canon(last, first)
            if not usable(key):
                continue
            others = [x for x in groups[key] if x["id"] != b["id"]]
            if not others:
                continue
            others.sort(key=lambda x: x["id"], reverse=True)
            more_by[b["id"]] = {"name": name_of(key, display), "others": others[:MAX_SHOWN],
                                "total": len(groups[key]), "slug": slugs.get(key)}
            break
    return more_by, pages

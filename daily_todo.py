"""
daily_todo.py — De dagelijkse e-mail "Vandaag te doen in de boekwinkel" (stok achter de deur).

Elke ochtend (om 7:15, via cron-job.org en de workflow 'Dagelijkse to-do-mail') komt er een e-mail met wat er vandaag in ieder geval te
doen is, met actuele aantallen uit de database. Regels waar niets te doen is (aantal 0) worden weggelaten. Ook ruimt de mail dagelijks de uitgeverslijst op (zie clean_publishers). Per dag van de week komen er
vaste punten bij (zie DAY_ITEMS hieronder). De ontvanger staat in TODO_MAIL_TO (standaard bart@severein.nl).
"""

import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
SUBJECT = "Vandaag te doen in de boekwinkel"
DEFAULT_TO = "bart@severein.nl"
MARK = "✔"
WEBSHOP_ID_BASE = 3_000_000_000   # zoals in webshop/orders.py: orders.id = -(BASE + bestelnummer * 1000 + eenheid_nr)
WEBSHOP_FIRST_REAL_ORDER = 2050   # lagere webshopnummers waren tests

# Vaste punten per dag van de week (0 = maandag ... 6 = zondag), woordelijk zoals ze in de mail komen.
PRIMERA_DHL = "Breng de pakjes naar de Primera (voor 17u) of Spar (voor 18u). Zit er iets voor DHL bij: voor 17:00 naar DHL Doormanstraat."
CHECK_BW = "Controleer de nieuwe boeken in Boekwinkeltjes op prijzen en gegevens"
CHECK_BOL = "Controleer de nieuwe boeken in Bol op prijzen en gegevens"
DAY_ITEMS = {
    5: [  # zaterdag
        PRIMERA_DHL,
        CHECK_BW,
        "Voer vandaag minimaal 50 boeken in. Als dat niet kan, schuif wat je overhoudt naar morgen.",
    ],
    6: [  # zondag
        "Breng de pakjes naar de Gamma Alexanderpolder (voor 17u) of Spar (voor 18u). Zit er iets voor DHL bij: morgenochtend naar de Diejo of mee naar DHL Doormanstraat.",
        CHECK_BOL,
        "Voer vandaag minimaal 50 boeken in. Als dat niet kan, schuif wat je overhoudt naar de komende week (verdeeld over de dagen).",
        "Maak een lijstje wat je de komende week moet vervangen en aanvullen in de boekenopslag",
    ],
    0: [  # maandag
        PRIMERA_DHL,
        CHECK_BW,
        "Voer vandaag minimaal 15 boeken in.",
        "Doe in de avond max een uur boekhouding, waaronder de potjes.",
    ],
    1: [  # dinsdag
        PRIMERA_DHL,
        CHECK_BOL,
        "Voer vandaag minimaal 15 boeken in",
    ],
    2: [  # woensdag
        PRIMERA_DHL,
        CHECK_BW,
        "Voer vandaag minimaal 15 boeken in",
        "Check bij Jules of morgen kratjeswissel kan.",
    ],
    3: [  # donderdag
        PRIMERA_DHL,
        CHECK_BOL,
        "Voer vandaag minimaal 15 boeken in",
        "Vanavond kratjeswissel bij Jules?",
    ],
    4: [  # vrijdag
        PRIMERA_DHL,
        CHECK_BOL,
        "Voer vandaag minimaal 15 boeken in.",
        "Doe tussen fysio en avondeten max anderhalf uur boekhouding. Drie kwartier 2025, Drie kwartier 2026.",
    ],
}


def _join_nl(parts):
    """['a', 'b', 'c'] -> 'a, b en c'."""
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " en " + parts[-1]


def build_items(counts, weekday):
    """
    Geeft (lijst_punten, lijst_tijd_over) terug als tekstregels zonder vinkje. 'counts' is een dict; een ontbrekende of None-waarde
    betekent 'niet bekend' en laat die regel weg (behalve waar hieronder anders staat).
    """
    items, spare = [], []

    n = counts.get("queued")
    if n:
        items.append(
            f"Er staan nog {n} boeken klaar voor controle in de bulkwachtrij, controleer ze en zet ze klaar voor de verkoop."
            if n != 1 else
            "Er staat nog 1 boek klaar voor controle in de bulkwachtrij, controleer het en zet het klaar voor de verkoop."
        )

    n = counts.get("bol_invoice_requests")
    if n is None:
        items.append("Zijn er bij Bol verzoeken om een factuur? Maak die dan en verstuur ze via Bol.")
    elif n:
        items.append(
            f"Er zijn bij Bol {n} verzoeken om een factuur. Maak die en verstuur ze via Bol." if n != 1
            else "Er is bij Bol 1 verzoek om een factuur. Maak die en verstuur hem via Bol."
        )

    n = counts.get("authors_without_text")
    if n:
        items.append(
            f"Er zijn {n} auteurspagina's die een goede omschrijving van de auteur nodig hebben." if n != 1
            else "Er is 1 auteurspagina die een goede omschrijving van de auteur nodig heeft."
        )

    items.append("Zijn alle e-mails beantwoord?")

    n = counts.get("bw_new_orders")
    if n:
        items.append(
            f'Er staan {n} boeken op "Nieuwe bestelling" bij Boekwinkeltjes. Controleer of er een betaallink is gestuurd.' if n != 1
            else 'Er staat 1 boek op "Nieuwe bestelling" bij Boekwinkeltjes. Controleer of er een betaallink is gestuurd.'
        )

    bol, bw, shop = counts.get("ship_bol") or 0, counts.get("ship_bw") or 0, counts.get("ship_webshop") or 0
    total = bol + bw + shop
    if total:
        where = _join_nl([f"{bol} op Bol" if bol else "", f"{bw} op Boekwinkeltjes" if bw else "", f"{shop} in de webshop" if shop else ""])
        items.append(
            (f"Er staan {total} boeken klaar om ingepakt en verzonden te worden ({where})." if total != 1
             else f"Er staat 1 boek klaar om ingepakt en verzonden te worden ({where}).")
            + " Begin daar uiterlijk 15:00 aan."
        )

    items.extend(DAY_ITEMS.get(weekday, []))
    items.extend(e["text"] for e in (counts.get("extras") or []))   # 'stokken achter de deur' uit Gebruikersbeheer

    n = counts.get("slow_movers")
    if n:
        spare.append(
            f"Er staan {n} boeken in winkeldochters die al een jaar op voorraad zijn. Kijk of je de prijs moet aanpassen of het boek af moet voeren."
            if n != 1 else
            "Er staat 1 boek in winkeldochters dat al een jaar op voorraad is. Kijk of je de prijs moet aanpassen of het boek af moet voeren."
        )
    return items, spare


WEEKDAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _end(text):
    """Elk punt eindigt met leesteken, zodat mailprogramma's er geen regels van samenvoegen."""
    text = text.rstrip()
    return text if text[-1:] in ".?!" else text + "."


def build_body(counts, weekday):
    items, spare = build_items(counts, weekday)
    lines = [
        "Goedemorgen Mijzelf!",
        "",
        f"Dit is je dagelijkse stok achter de deur. Het is {WEEKDAYS_NL[weekday]}. Wat is er vandaag in ieder geval te doen in de boekwinkel?",
        "",
    ]
    lines += [f"{MARK} {_end(t)}" for t in items]
    if spare:
        lines += ["", "Als je tijd over hebt:", ""]
        lines += [f"{MARK} {_end(t)}" for t in spare]
    lines += ["", "Succes vandaag!", "", "Groet,", "", "Ik", ""]
    return "\n".join(lines)


# ---------- aantallen uit de database ----------

def _parse_date(text):
    """'Te koop sinds' als datum: 2025-03-14 (ook met tijd erachter) of 14-03-2025 / 14/03/2025. Onleesbaar: None."""
    text = str(text or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _count(cur, sql, params=None):
    cur.execute(sql, params or {})
    row = cur.fetchone()
    return int(row["n"]) if row else 0


def collect_counts(conn, today=None, log=print):
    """Alle aantallen voor de mail. Een tel die mislukt wordt None (regel valt weg) en komt in het log."""
    today = today or dt.datetime.now(TZ).date()
    counts = {"bol_invoice_requests": None}   # None = niet bekend (Bol niet te bereiken): dan een vaste vraag in de mail

    def attempt(key, fn):
        try:
            counts[key] = fn()
        except Exception as exc:
            conn.rollback()
            counts[key] = None
            log(f"To-do-mail: {key} niet te tellen ({exc}).")

    def bol_requests():
        import bol_client
        return len(bol_client.get_open_invoice_requests())
    attempt("bol_invoice_requests", bol_requests)

    with conn.cursor() as cur:
        attempt("queued", lambda: _count(cur, "SELECT count(*) AS n FROM books WHERE COALESCE(queued, FALSE) = TRUE"))
        # Te verzenden: Boekwinkeltjes (wait_for_shipping), Bol (OPEN) en webshop (wait_for_shipping, zonder de tests van voor #2050).
        attempt("ship_bw", lambda: _count(
            cur, "SELECT count(*) AS n FROM orders WHERE status = 'wait_for_shipping' AND COALESCE(platform, 'BW') = 'BW'"))
        attempt("bw_new_orders", lambda: _count(
            cur, "SELECT count(*) AS n FROM orders WHERE status = 'new_order' AND COALESCE(platform, 'BW') = 'BW'"))
        attempt("ship_bol", lambda: _count(cur, "SELECT count(*) AS n FROM orders WHERE status = 'OPEN' AND platform = 'Bol'"))
        attempt("ship_webshop", lambda: _count(
            cur,
            "SELECT count(*) AS n FROM orders WHERE status = 'wait_for_shipping' AND platform = 'Webshop' "
            "AND ((-id - %(base)s) / 1000) >= %(first)s",
            {"base": WEBSHOP_ID_BASE, "first": WEBSHOP_FIRST_REAL_ORDER}))

        def slow():
            cur.execute(
                "SELECT listing_date FROM books WHERE id > 0 AND COALESCE(queued, FALSE) = FALSE AND COALESCE(amount, 0) > 0 "
                "AND COALESCE(listing_date, '') <> ''"
            )
            cutoff = today - dt.timedelta(days=365)
            parsed = (_parse_date(r["listing_date"]) for r in cur.fetchall())
            return sum(1 for d in parsed if d is not None and d <= cutoff)
        attempt("slow_movers", slow)

        def authors():
            from webshop.authors import build_authors
            cur.execute(
                "SELECT id, author FROM books WHERE COALESCE(amount, 0) > 0 AND COALESCE(pending_create, FALSE) = FALSE "
                "AND COALESCE(queued, FALSE) = FALSE AND price IS NOT NULL AND price > 0 AND COALESCE(title, '') <> ''"
            )
            books = [{"id": int(r["id"]), "author": r["author"]} for r in cur.fetchall()]
            _more, pages = build_authors(books)
            try:
                cur.execute("SELECT slug FROM author_texts WHERE COALESCE(text, '') <> ''")
                have = {r["slug"] for r in cur.fetchall()}
            except Exception:   # tabel bestaat nog niet: dan heeft nog niemand een tekst
                conn.rollback()
                have = set()
            return sum(1 for p in pages if p["slug"] not in have)
        attempt("authors_without_text", authors)

        def extras():
            # Handmatige taken uit Gebruikersbeheer die voor vandaag (of eerder, mocht een mail zijn gemist) bedoeld zijn.
            cur.execute(TODO_EXTRAS_DDL)
            cur.execute("SELECT id, text FROM todo_extras WHERE due_date <= %(d)s ORDER BY due_date, id", {"d": today})
            rows = [{"id": int(r["id"]), "text": r["text"]} for r in cur.fetchall()]
            conn.commit()
            return rows
        attempt("extras", extras)
    return counts


def clean_publishers(conn, log=print):
    """Verwijdert uit de uitgeverslijst alle uitgevers zonder puntkomma. Geeft het aantal terug (None bij een fout)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM known_publishers WHERE COALESCE(value, '') <> '' AND value NOT LIKE '%%;%%' RETURNING value"
            )
            gone = [r["value"] for r in cur.fetchall()]
        conn.commit()
        log(f"Uitgevers opgeruimd: {len(gone)}" + (f" ({'; '.join(gone[:20])}{' …' if len(gone) > 20 else ''})" if gone else ""))
        return len(gone)
    except Exception as exc:
        conn.rollback()
        log(f"Uitgevers opruimen mislukt ({exc}).")
        return None


TODO_EXTRAS_DDL = """
CREATE TABLE IF NOT EXISTS todo_extras (
    id SERIAL PRIMARY KEY,
    due_date DATE NOT NULL,
    text TEXT NOT NULL,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def delete_sent_extras(ids, log=print):
    """Haalt de taken die in de mail hebben gestaan uit de lijst."""
    if not ids:
        return
    from db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM todo_extras WHERE id = ANY(%(ids)s)", {"ids": list(ids)})
        conn.commit()
        log(f"To-do-mail: {len(ids)} 'stok(ken) achter de deur' uit de lijst gehaald.")
    except Exception as exc:
        conn.rollback()
        log(f"To-do-mail: stokken achter de deur verwijderen mislukt ({exc}).")
    finally:
        conn.close()


def run(log=print):
    """Telt, bouwt de mail en verstuurt hem. Geeft de tekst terug (handig bij testen)."""
    import notifications
    from db import get_connection

    now = dt.datetime.now(TZ)
    conn = get_connection()
    try:
        clean_publishers(conn, log=log)
        counts = collect_counts(conn, today=now.date(), log=log)
    finally:
        conn.close()
    body = build_body(counts, now.weekday())
    to = os.environ.get("TODO_MAIL_TO") or DEFAULT_TO
    notifications.send_email(SUBJECT, body, to=to)
    delete_sent_extras([e["id"] for e in (counts.get("extras") or [])], log=log)   # pas na het versturen
    log(f"To-do-mail verstuurd naar {to}: {counts}")
    return body

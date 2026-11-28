"""
bol_paginas_aanvullen.py — vult het aantal pagina's aan uit de Bol-catalogus (alleen voor de Google-feed van de webshop).

Het schrijft ALLEEN naar de kolom books.pages (draai eerst boeken_paginas.sql). Bijzonderheden, Boekwinkeltjes en Bol
blijven ongemoeid. Boeken waar het aantal pagina's al in Bijzonderheden staat of al is aangevuld, worden overgeslagen.
Boeken die Bol niet kent worden gemarkeerd met pages = 0 en niet opnieuw geprobeerd.

Gebruik:
    python bol_paginas_aanvullen.py --dry-run --limit 20     # proef: toont wat er zou gebeuren
    python bol_paginas_aanvullen.py --limit 500              # echt, maximaal 500 boeken
    python bol_paginas_aanvullen.py                          # alle boeken met voorraad
Opnieuw draaien is veilig: het gaat verder waar het gebleven was.
"""
import argparse
import re
import time

import requests

import bol_client
from db import get_connection

PAGES_IN_TEXT = re.compile(r"\d{1,5}\s*(?:pp|pag|pagina'?s|pagina|p|blz)\.?", re.I)


def _has_pages(short):
    return any(PAGES_IN_TEXT.fullmatch(p.strip()) for p in (short or "").split(","))


def _pages_from_bol(ean):
    """Geeft (aantal, status): status 'ok', 'unknown' (Bol kent het boek of de pagina's niet) of 'error' (later opnieuw)."""
    try:
        resp = requests.get(f"https://api.bol.com/retailer/content/catalog-products/{ean}",
                            headers={"Accept": "application/vnd.retailer.v10+json", "Accept-Language": "nl",
                                     "Authorization": f"Bearer {bol_client._get_access_token()}"}, timeout=20)
    except requests.RequestException:
        return None, "error"
    if resp.status_code == 404:
        return None, "unknown"
    if resp.status_code == 429 or resp.status_code >= 500:
        return None, "error"
    if not resp.ok:
        return None, "unknown"
    try:
        data = resp.json()
    except ValueError:
        return None, "error"
    product = (data.get("products") or [None])[0] if isinstance(data, dict) and "products" in data else data
    for attr in (product or {}).get("attributes") or []:
        attr_id = (attr.get("id") or "").lower()
        if "pagina" in attr_id or "pages" in attr_id:
            for v in attr.get("values") or []:
                m = re.search(r"\d+", str(v.get("value") or ""))
                if m and 0 < int(m.group()) < 20000:
                    return int(m.group()), "ok"
    return None, "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""SELECT id, ean, short_description FROM books
                       WHERE COALESCE(amount, 0) > 0 AND pages IS NULL AND ean ~ '^[0-9]{13}$' ORDER BY id""")
        todo = [r for r in cur.fetchall() if not _has_pages(r["short_description"])]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} boeken te controleren" + (" (proef, er wordt niets opgeslagen)" if args.dry_run else ""))

    found = unknown = errors = 0
    for n, r in enumerate(todo, 1):
        pages, status = _pages_from_bol(r["ean"])
        if status == "error":
            errors += 1
            time.sleep(5)
            if errors >= 10:
                print("Te veel fouten achter elkaar; gestopt. Later opnieuw draaien.")
                break
            continue
        errors = 0
        if status == "ok":
            found += 1
        else:
            unknown += 1
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("UPDATE books SET pages = %s WHERE id = %s", (pages or 0, r["id"]))
            if n % 25 == 0:
                conn.commit()
        elif n <= 20:
            print(r["id"], r["ean"], pages)
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}: {found} gevonden, {unknown} onbekend")
        time.sleep(0.4)   # rustig aan richting Bol
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"Klaar: {found} met pagina's, {unknown} onbekend bij Bol.")


if __name__ == "__main__":
    main()

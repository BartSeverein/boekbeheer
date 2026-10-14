"""
webshop/orders.py — betaalde webshop-bestellingen (uit shop.sqlite op de server) overnemen in de Postgres-database.

Dezelfde aanpak als bij Bol: elke eenheid wordt één rij in 'orders' (platform 'Webshop', negatief id), de voorraad gaat
precies één keer omlaag (stock_adjusted) en de nieuwe voorraad gaat naar Boekwinkeltjes.
"""
import datetime as dt
import sqlite3

WEBSHOP_ID_BASE = 3_000_000_000   # orders.id = -(BASE + shop_order_id * 1000 + eenheid_nr)


def shop_order_id_from(pg_id):
    return (-int(pg_id) - WEBSHOP_ID_BASE) // 1000


def read_paid_orders(shop_db_path):
    con = sqlite3.connect(shop_db_path)
    con.row_factory = sqlite3.Row
    try:
        orders = [dict(r) for r in con.execute("SELECT * FROM orders WHERE status = 'paid' ORDER BY id")]
        for o in orders:
            o["lines"] = [dict(r) for r in con.execute("SELECT * FROM order_lines WHERE order_id = ? ORDER BY rowid", (o["id"],))]
    finally:
        con.close()
    return orders


def import_shop_orders(conn, shop_db_path, execute=True, update_book=None, log=print):
    """
    Geeft (nieuwe_orders, voorraad_verwerkt, aandachtspunten) terug. Met execute=False wordt niets geschreven.
    update_book(book_id, payload): stuurt de nieuwe voorraad naar Boekwinkeltjes (standaard api_client.update_book).
    """
    orders = read_paid_orders(shop_db_path)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM orders WHERE platform = 'Webshop'")
        have = {int(r["id"]) for r in cur.fetchall()}
    new_orders, attention = 0, []
    rows = []
    for o in orders:
        k = 0
        first = True
        unit_ids = []
        for line in o["lines"]:
            for _ in range(int(line["qty"])):
                pid = -(WEBSHOP_ID_BASE + o["id"] * 1000 + k)
                unit_ids.append(pid)
                rows.append((o, line, pid, first))
                first = False
                k += 1
        is_new = bool(unit_ids) and not any(u in have for u in unit_ids)
        if is_new:
            new_orders += 1
        if is_new and o.get("attention"):
            attention.append(f"Webshop-bestelling #{o['id']}: {o['attention']}")
    todo = [r for r in rows if r[2] not in have]
    if not execute:
        return new_orders, 0, attention

    for o, line, pid, first in todo:
        paid = dt.datetime.fromtimestamp(o["paid_at"] or o["created_at"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        note = (o.get("note") or "").strip()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (id, order_date, status, online_payment_status, platform, stock_adjusted, book_id, book_title,
                    book_author, book_price, book_shipping_cost, book_ean, buyer_name, buyer_email, buyer_language, buyer_street,
                    buyer_zip_code, buyer_city, buyer_country, note, last_synced_at)
                VALUES (%(id)s, %(date)s, 'wait_for_shipping', 'paid', 'Webshop', FALSE, %(book_id)s, %(title)s, %(author)s, %(price)s,
                    %(ship)s, %(ean)s, %(name)s, %(email)s, 'nl', %(street)s, %(zip)s, %(city)s, %(country)s, %(note)s, now())
                ON CONFLICT (id) DO NOTHING
                """,
                {
                    "id": pid, "date": paid, "book_id": line["book_id"], "title": line["title"], "author": line["author"],
                    "price": line["price_cents"] / 100, "ship": (o["ship_cents"] / 100) if first else 0, "ean": line["isbn"],
                    "name": o["name"], "email": o["email"], "street": o["street"], "zip": o["postcode"], "city": o["city"],
                    "country": o["country"], "note": ((note + " | ") if note else "") + f"Webshop-bestelling #{o['id']}" +
                    (f" — LET OP: {o['attention']}" if o.get("attention") else ""),
                },
            )
    conn.commit()

    if update_book is None:
        import api_client
        update_book = api_client.update_book

    with conn.cursor() as cur:
        cur.execute("SELECT id, book_id FROM orders WHERE platform = 'Webshop' AND COALESCE(stock_adjusted, FALSE) = FALSE AND book_id IS NOT NULL")
        pending = cur.fetchall()
    done = 0
    for sale in pending:
        with conn.cursor() as cur:
            cur.execute("SELECT amount, price FROM books WHERE id = %(id)s", {"id": sale["book_id"]})
            book = cur.fetchone()
            if book:
                new_amount = max((book["amount"] or 0) - 1, 0)
                cur.execute("UPDATE books SET amount = %(a)s WHERE id = %(id)s", {"a": new_amount, "id": sale["book_id"]})
                try:
                    payload = {"amount": new_amount}
                    if book["price"] is not None:
                        payload["price"] = float(book["price"])
                    update_book(sale["book_id"], payload)
                except Exception:
                    # Lukt het niet, dan pakt de gewone push het later op.
                    cur.execute("UPDATE books SET pending_push = TRUE WHERE id = %(id)s", {"id": sale["book_id"]})
            cur.execute("UPDATE orders SET stock_adjusted = TRUE WHERE id = %(id)s", {"id": sale["id"]})
        conn.commit()
        done += 1
    return new_orders, done, attention

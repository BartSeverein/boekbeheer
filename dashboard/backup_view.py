"""
backup_view.py — Hulpfuncties voor de pagina 'Back-up bekijken': een back-up (zip of losse CSV-bestanden) inlezen en
vergelijken met de huidige database. Bewust zonder Streamlit en zonder databasetoegang, zodat het los te testen is.
"""

import io
import re
import zipfile

import pandas as pd

NUMERIC_BOOK_COLUMNS = ["id", "book_number", "amount", "price", "shipping_cost", "shipping_cost_bol",
                        "shipping_format", "shipping_category", "length_cm", "width_cm", "thickness_cm"]
NUMERIC_ORDER_COLUMNS = ["id", "book_id", "book_price", "book_shipping_cost"]

# Velden die bij het vergelijken van boeken meetellen (geen tijdstempels of interne vlaggen: die geven alleen ruis).
COMPARE_BOOK_FIELDS = ["title", "author", "location", "amount", "price", "shipping_cost", "shipping_cost_bol",
                       "shipping_format", "ean", "category1", "category2", "category3", "language",
                       "short_description", "queued", "push_enabled"]
COMPARE_ORDER_FIELDS = ["status", "online_payment_status", "platform", "book_id", "buyer_name", "book_price",
                        "book_shipping_cost"]
_NUMERIC_COMPARE = {"amount", "price", "shipping_cost", "shipping_cost_bol", "shipping_format", "book_id",
                    "book_price", "book_shipping_cost"}

BACKUP_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class BackupReadError(Exception):
    pass


def _read_csv_bytes(data):
    return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _kind_of(name, frame):
    lower = (name or "").lower()
    if "order" in lower:
        return "orders"
    if "boek" in lower or "book" in lower:
        return "books"
    cols = set(frame.columns)
    if "buyer_email" in cols or "buyer_name" in cols:
        return "orders"
    if "title" in cols and "price" in cols:
        return "books"
    return None


def _to_numeric(frame, columns):
    for col in columns:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def read_backup(files):
    """
    'files' is een lijst (bestandsnaam, bytes). Geaccepteerd: één zip met boeken_*.csv en orders_*.csv, of die twee
    CSV-bestanden los. Geeft (boeken, bestellingen, info) terug; ontbrekende delen zijn None.
    """
    books = orders = None
    dates = []
    for name, data in files:
        parts = []
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for member in zf.namelist():
                        if member.lower().endswith(".csv"):
                            parts.append((member, zf.read(member)))
            except zipfile.BadZipFile:
                raise BackupReadError(f"{name} is geen geldige zip")
        elif name.lower().endswith(".csv"):
            parts.append((name, data))
        else:
            raise BackupReadError(f"{name}: alleen zip- of csv-bestanden")
        for member, content in parts:
            if not content.strip():
                continue
            frame = _read_csv_bytes(content)
            kind = _kind_of(member, frame)
            if kind == "books":
                books = _to_numeric(frame, NUMERIC_BOOK_COLUMNS)
            elif kind == "orders":
                orders = _to_numeric(frame, NUMERIC_ORDER_COLUMNS)
            m = BACKUP_DATE_RE.search(member) or BACKUP_DATE_RE.search(name)
            if m:
                dates.append(m.group(1))
    if books is None and orders is None:
        raise BackupReadError("Geen boeken- of bestellingenbestand gevonden in de back-up")
    return books, orders, {"date": max(dates) if dates else None}


def _norm(value, numeric):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if text == "":
        return ""
    if numeric:
        try:
            return f"{round(float(text), 2):.2f}"
        except ValueError:
            return text
    return text


def _compare_frames(backup, current, fields, key="id"):
    """Algemene vergelijking op sleutel. Geeft (alleen_in_backup, alleen_nu, verschillen) terug."""
    b = backup.copy()
    c = current.copy()
    b[key] = pd.to_numeric(b[key], errors="coerce")
    c[key] = pd.to_numeric(c[key], errors="coerce")
    b = b.dropna(subset=[key]).drop_duplicates(subset=[key]).set_index(key)
    c = c.dropna(subset=[key]).drop_duplicates(subset=[key]).set_index(key)
    only_backup = sorted(int(x) for x in set(b.index) - set(c.index))
    only_now = sorted(int(x) for x in set(c.index) - set(b.index))
    common = sorted(set(b.index) & set(c.index))
    use = [f for f in fields if f in b.columns and f in c.columns]
    rows = []
    for k in common:
        brow = b.loc[k]
        crow = c.loc[k]
        for f in use:
            numeric = f in _NUMERIC_COMPARE
            bv, cv = _norm(brow[f], numeric), _norm(crow[f], numeric)
            if bv != cv:
                rows.append({key: int(k), "veld": f, "back-up": bv, "nu": cv})
    return only_backup, only_now, pd.DataFrame(rows, columns=[key, "veld", "back-up", "nu"])


def compare_books(backup, current):
    """Vergelijkt boeken uit de back-up met de huidige database. Concepten (negatief id) tellen niet mee."""
    return _compare_frames(backup[pd.to_numeric(backup["id"], errors="coerce") > 0],
                           current[pd.to_numeric(current["id"], errors="coerce") > 0], COMPARE_BOOK_FIELDS)


def compare_orders(backup, current):
    return _compare_frames(backup, current, COMPARE_ORDER_FIELDS)


def add_titles(differences, books_backup, books_now):
    """Zet de titel (uit de huidige database, anders uit de back-up) naast elk verschil."""
    if differences.empty:
        out = differences.copy()
        out.insert(1, "titel", [])
        return out
    titles = {}
    for frame in (books_backup, books_now):
        if frame is not None and "title" in frame.columns:
            for i, t in zip(pd.to_numeric(frame["id"], errors="coerce"), frame["title"]):
                if pd.notna(i):
                    titles[int(i)] = t
    out = differences.copy()
    out.insert(1, "titel", [titles.get(int(i), "") for i in out["id"]])
    return out


def search_frame(frame, text, columns):
    """Houdt de rijen over waar 'text' (hoofdletterongevoelig, alle woorden) in één van de kolommen voorkomt."""
    text = (text or "").strip().lower()
    if not text:
        return frame
    cols = [c for c in columns if c in frame.columns]
    if frame.empty or not cols:
        return frame
    rows = frame[cols].fillna("").astype(str).values.tolist()
    haystack = [" ".join(row).lower() for row in rows]
    words = text.split()
    keep = [all(w in h for w in words) for h in haystack]
    return frame[keep]

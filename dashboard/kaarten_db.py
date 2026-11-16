"""
kaarten_db.py — Alles voor de ansichtkaarten (de pagina's Nieuwe kaart, Kaartdetails, Kaartplaatsbeheer en het
kaarten-deel van Uitgeversbeheer). Staat apart van common.py, zodat de boeken er niets van merken.

Tabellen: zie kaarten_1_tabellen.sql (kaarten, kaart_plaatsen, kaart_uitgevers, kaart_themas, kaart_perioden, ...).
Kleur van de kaarten in grafieken en op de kaartenshop: groen, #6fc16f.
"""

import io
import re

import streamlit as st

KAART_GROEN = "#6fc16f"
KLEUREN = ["Zwart/wit", "Ingekleurd", "Kleur"]


def _connect():
    from common import _dict_connect   # pas hier geïmporteerd: common.py is groot en heeft zelf Streamlit nodig
    return _dict_connect()


def _query(sql, params=None):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchall()
    finally:
        conn.close()


def _execute(sql, params=None):
    """Voert één schrijfopdracht uit. Geeft het aantal geraakte rijen terug."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            n = cur.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def tabellen_aanwezig():
    """False zolang kaarten_1_tabellen.sql nog niet is uitgevoerd."""
    try:
        return bool(_query("SELECT to_regclass('public.kaarten') AS t")[0]["t"])
    except Exception:
        return False


def toon_tabellen_melding():
    if not tabellen_aanwezig():
        st.error("De tabellen voor de kaarten bestaan nog niet. Voer kaarten_1_tabellen.sql, kaarten_2_plaatsen_themas.sql en kaarten_3_uitgevers.sql uit in Supabase.")
        st.stop()


# ---------- Keuzelijsten ----------

@st.cache_data(ttl=300, show_spinner=False)
def get_provincies():
    return [r["naam"] for r in _query("SELECT naam FROM kaart_provincies ORDER BY volgorde, naam")]


@st.cache_data(ttl=300, show_spinner=False)
def get_plaatsen(provincie):
    """[(id, naam)] van een provincie, alfabetisch."""
    rows = _query("SELECT id, naam FROM kaart_plaatsen WHERE provincie = %(p)s ORDER BY lower(naam)", {"p": provincie})
    return [(r["id"], r["naam"]) for r in rows]


@st.cache_data(ttl=300, show_spinner=False)
def get_themas():
    return [r["naam"] for r in _query("SELECT naam FROM kaart_themas ORDER BY naam")]


@st.cache_data(ttl=300, show_spinner=False)
def get_perioden():
    return [r["naam"] for r in _query("SELECT naam FROM kaart_perioden ORDER BY volgorde, naam")]


def uitgever_label(naam, plaats):
    return f"{naam}; {plaats}" if plaats else naam


@st.cache_data(ttl=300, show_spinner=False)
def get_uitgevers():
    """[(id, label)] alfabetisch, label = 'Naam; Plaats' (zoals in je eigen lijst)."""
    rows = _query("SELECT id, naam, plaats FROM kaart_uitgevers ORDER BY lower(naam), lower(plaats)")
    return [(r["id"], uitgever_label(r["naam"], r["plaats"])) for r in rows]


def clear_lijsten():
    for f in (get_provincies, get_plaatsen, get_themas, get_perioden, get_uitgevers):
        f.clear()


def periode_voor_jaar(jaar):
    """Welke periode hoort bij een jaartal? None als het jaartal leeg is of in geen enkele periode past."""
    if not jaar:
        return None
    namen = get_perioden()
    for naam in namen:
        m = re.match(r"^Voor (\d{4})$", naam)
        if m and jaar < int(m.group(1)):
            return naam
        m = re.match(r"^(\d{4})-(\d{4})$", naam)
        if m and int(m.group(1)) <= jaar <= int(m.group(2)):
            return naam
        m = re.match(r"^(\d{4}) en later$", naam)
        if m and jaar >= int(m.group(1)):
            return naam
    return None


# ---------- Foto's ----------

KAART_MAX_PX = 1600   # langste zijde van de opgeslagen scan


def verklein_scan(data, max_px=KAART_MAX_PX):
    """Maakt van een scan een JPEG met hoogstens max_px op de langste zijde. Geeft bytes terug."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue()


def kaartnummer_uit_bestandsnaam(naam):
    """'260112_01v.jpg' -> ('260112_01', 'v'). Geeft (None, None) als de naam niet op v of a eindigt."""
    stam = re.sub(r"\.[A-Za-z0-9]+$", "", naam or "").strip()
    m = re.match(r"^(.+?)([vaVA])$", stam)
    if not m:
        return None, None
    return m.group(1), m.group(2).lower()


# ---------- Kaarten ----------

KAART_VELDEN = ["titel", "provincie", "plaats_id", "uitgever_id", "periode", "jaar_uitgave", "beschreven", "gelopen",
                "stempeldoordruk", "kleur", "prijs", "amount", "locatie", "opmerking"]


def kaart_bestaat(kaartnummer):
    return bool(_query("SELECT 1 AS x FROM kaarten WHERE kaartnummer = %(n)s", {"n": kaartnummer}))


def _zet_themas(cur, kaartnummer, themas):
    cur.execute("DELETE FROM kaart_thema_koppeling WHERE kaartnummer = %(n)s", {"n": kaartnummer})
    for i, t in enumerate(themas or []):
        cur.execute(
            "INSERT INTO kaart_thema_koppeling (kaartnummer, thema, is_hoofd) VALUES (%(n)s, %(t)s, %(h)s)",
            {"n": kaartnummer, "t": t, "h": i == 0},
        )


def plaats_id_voor(provincie, naam):
    """
    Het id van een plaats bij een provincie, op naam (hoofdletters maken niet uit; een bekende afwijkende schrijfwijze telt ook).
    Bestaat de plaats nog niet, dan wordt ze hier aan die provincie toegevoegd. Geeft None bij een lege naam of provincie.
    """
    naam = re.sub(r"\s+", " ", (naam or "")).strip()
    if not naam or not provincie:
        return None
    rows = _query("SELECT id FROM kaart_plaatsen WHERE provincie = %(p)s AND lower(naam) = lower(%(n)s)", {"p": provincie, "n": naam})
    if not rows:
        rows = _query(
            """
            SELECT p.id FROM kaart_plaats_alias a JOIN kaart_plaatsen p ON p.provincie = a.provincie AND p.naam = a.plaats
            WHERE a.provincie = %(p)s AND lower(a.alias) = lower(%(n)s)
            """,
            {"p": provincie, "n": naam},
        )
    if rows:
        return rows[0]["id"]
    _execute("INSERT INTO kaart_plaatsen (provincie, naam) VALUES (%(p)s, %(n)s) ON CONFLICT (provincie, naam) DO NOTHING",
             {"p": provincie, "n": naam})
    clear_lijsten()
    return _query("SELECT id FROM kaart_plaatsen WHERE provincie = %(p)s AND naam = %(n)s", {"p": provincie, "n": naam})[0]["id"]


def _los_plaats_op(velden):
    """Een getypte plaatsnaam ('_plaats_naam') wordt een plaats_id; een nieuwe plaats wordt daarbij toegevoegd."""
    velden = dict(velden)
    naam = velden.pop("_plaats_naam", None)
    if naam:
        velden["plaats_id"] = plaats_id_voor(velden.get("provincie"), naam)
    return velden


def maak_kaart(kaartnummer, velden, themas, voorkant, achterkant, door):
    """Slaat een nieuwe kaart op, met altijd twee scans. Gooit ValueError bij een fout die de gebruiker kan oplossen."""
    velden = _los_plaats_op(velden)
    kaartnummer = (kaartnummer or "").strip()
    if not kaartnummer:
        raise ValueError("Vul een kaartnummer in.")
    if not voorkant or not achterkant:
        raise ValueError("Een kaart heeft altijd twee scans: voorkant én achterkant.")
    if kaart_bestaat(kaartnummer):
        raise ValueError(f"Kaartnummer {kaartnummer} bestaat al.")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kaarten (kaartnummer, titel, provincie, plaats_id, uitgever_id, periode, jaar_uitgave, beschreven,
                                     gelopen, stempeldoordruk, kleur, prijs, amount, locatie, opmerking, voorkant, achterkant, created_by)
                VALUES (%(kaartnummer)s, %(titel)s, %(provincie)s, %(plaats_id)s, %(uitgever_id)s, %(periode)s, %(jaar_uitgave)s,
                        %(beschreven)s, %(gelopen)s, %(stempeldoordruk)s, %(kleur)s, %(prijs)s, %(amount)s, %(locatie)s, %(opmerking)s,
                        %(voorkant)s, %(achterkant)s, %(door)s)
                """,
                {**{v: velden.get(v) for v in KAART_VELDEN}, "kaartnummer": kaartnummer,
                 "voorkant": voorkant, "achterkant": achterkant, "door": door},
            )
            _zet_themas(cur, kaartnummer, themas)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wijzig_kaart(kaartnummer, velden, themas, voorkant=None, achterkant=None):
    """Past een kaart aan. Een scan die None is blijft zoals hij was."""
    velden = _los_plaats_op(velden)
    bewerkbaar = [v for v in KAART_VELDEN if v != "locatie"]   # de locatie wordt niet meer gebruikt; een oude waarde blijft staan
    sets = [f"{v} = %({v})s" for v in bewerkbaar]
    params = {v: velden.get(v) for v in bewerkbaar}
    params["n"] = kaartnummer
    if voorkant:
        sets.append("voorkant = %(voorkant)s"); params["voorkant"] = voorkant
    if achterkant:
        sets.append("achterkant = %(achterkant)s"); params["achterkant"] = achterkant
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE kaarten SET {', '.join(sets)} WHERE kaartnummer = %(n)s", params)
            _zet_themas(cur, kaartnummer, themas)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verwijder_kaart(kaartnummer):
    return _execute("DELETE FROM kaarten WHERE kaartnummer = %(n)s", {"n": kaartnummer})


def zoek_kaarten(tekst="", provincie=None, plaats_id=None, thema=None, alleen_voorraad=False, limiet=200):
    """Zoekt op kaartnummer, titel of plaatsnaam. Geeft lichte rijen (zonder scans) terug, nieuwste eerst."""
    where, p = ["TRUE"], {"lim": limiet}
    tekst = (tekst or "").strip()
    if tekst:
        where.append("(k.kaartnummer ILIKE %(t)s OR k.titel ILIKE %(t)s OR pl.naam ILIKE %(t)s OR k.opmerking ILIKE %(t)s)")
        p["t"] = f"%{tekst}%"
    if provincie:
        where.append("k.provincie = %(prov)s"); p["prov"] = provincie
    if plaats_id:
        where.append("k.plaats_id = %(plaats)s"); p["plaats"] = plaats_id
    if thema:
        where.append("EXISTS (SELECT 1 FROM kaart_thema_koppeling t WHERE t.kaartnummer = k.kaartnummer AND t.thema = %(thema)s)")
        p["thema"] = thema
    if alleen_voorraad:
        where.append("k.amount > 0")
    return _query(
        f"""
        SELECT k.kaartnummer, k.titel, k.provincie, pl.naam AS plaats, k.prijs, k.amount, k.locatie, k.created_at
        FROM kaarten k LEFT JOIN kaart_plaatsen pl ON pl.id = k.plaats_id
        WHERE {' AND '.join(where)}
        ORDER BY k.created_at DESC, k.kaartnummer DESC
        LIMIT %(lim)s
        """,
        p,
    )


def get_kaart(kaartnummer):
    rows = _query("SELECT * FROM kaarten WHERE kaartnummer = %(n)s", {"n": kaartnummer})
    if not rows:
        return None
    kaart = dict(rows[0])
    kaart["themas"] = [r["thema"] for r in _query(
        "SELECT thema FROM kaart_thema_koppeling WHERE kaartnummer = %(n)s ORDER BY is_hoofd DESC, thema", {"n": kaartnummer})]
    for veld in ("voorkant", "achterkant"):
        if kaart.get(veld) is not None:
            kaart[veld] = bytes(kaart[veld])
    return kaart


def aantal_kaarten():
    return _query("SELECT count(*) AS n, COALESCE(sum(amount), 0) AS voorraad FROM kaarten")[0]


# ---------- Plaatsbeheer ----------

def plaatsen_met_aantal(provincie):
    return _query(
        """
        SELECT p.id, p.naam, p.eiland, (SELECT count(*) FROM kaarten k WHERE k.plaats_id = p.id) AS kaarten
        FROM kaart_plaatsen p WHERE p.provincie = %(p)s ORDER BY lower(p.naam)
        """,
        {"p": provincie},
    )


def voeg_plaats_toe(provincie, naam):
    naam = (naam or "").strip()
    if not naam:
        raise ValueError("Vul een plaatsnaam in.")
    n = _execute("INSERT INTO kaart_plaatsen (provincie, naam) VALUES (%(p)s, %(n)s) ON CONFLICT (provincie, naam) DO NOTHING",
                 {"p": provincie, "n": naam})
    clear_lijsten()
    if not n:
        raise ValueError(f"{naam} staat al bij {provincie}.")


def hernoem_plaats(plaats_id, nieuwe_naam):
    nieuwe_naam = (nieuwe_naam or "").strip()
    if not nieuwe_naam:
        raise ValueError("De naam mag niet leeg zijn.")
    try:
        _execute("UPDATE kaart_plaatsen SET naam = %(n)s WHERE id = %(i)s", {"n": nieuwe_naam, "i": plaats_id})
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise ValueError(f"{nieuwe_naam} bestaat al in deze provincie. Gebruik 'Samenvoegen'.")
        raise
    clear_lijsten()


def verwijder_plaats(plaats_id):
    gebruikt = _query("SELECT count(*) AS n FROM kaarten WHERE plaats_id = %(i)s", {"i": plaats_id})[0]["n"]
    if gebruikt:
        raise ValueError(f"Er hangen nog {gebruikt} kaarten aan deze plaats. Voeg haar eerst samen met een andere plaats.")
    _execute("DELETE FROM kaart_plaatsen WHERE id = %(i)s", {"i": plaats_id})
    clear_lijsten()


def voeg_plaatsen_samen(van_id, naar_id):
    """Alle kaarten van 'van' gaan naar 'naar'; de oude naam wordt een afwijkende schrijfwijze van 'naar'."""
    if van_id == naar_id:
        raise ValueError("Kies twee verschillende plaatsen.")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT provincie, naam FROM kaart_plaatsen WHERE id = %(i)s", {"i": van_id}); van = cur.fetchone()
            cur.execute("SELECT provincie, naam FROM kaart_plaatsen WHERE id = %(i)s", {"i": naar_id}); naar = cur.fetchone()
            if not van or not naar:
                raise ValueError("Plaats niet gevonden.")
            cur.execute("UPDATE kaarten SET plaats_id = %(n)s, provincie = %(p)s WHERE plaats_id = %(v)s",
                        {"n": naar_id, "p": naar["provincie"], "v": van_id})
            cur.execute("UPDATE kaart_plaats_alias SET plaats = %(np)s, provincie = %(pp)s WHERE provincie = %(vp)s AND plaats = %(vn)s",
                        {"np": naar["naam"], "pp": naar["provincie"], "vp": van["provincie"], "vn": van["naam"]})
            cur.execute("DELETE FROM kaart_plaatsen WHERE id = %(v)s", {"v": van_id})
            if van["provincie"] == naar["provincie"]:
                cur.execute(
                    "INSERT INTO kaart_plaats_alias (provincie, alias, plaats) VALUES (%(p)s, %(a)s, %(n)s) ON CONFLICT DO NOTHING",
                    {"p": naar["provincie"], "a": van["naam"], "n": naar["naam"]},
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    clear_lijsten()


def voeg_provincie_toe(naam):
    naam = (naam or "").strip()
    if not naam:
        raise ValueError("Vul een naam in.")
    volgorde = _query("SELECT COALESCE(max(volgorde), 0) + 1 AS v FROM kaart_provincies")[0]["v"]
    _execute("INSERT INTO kaart_provincies (naam, volgorde) VALUES (%(n)s, %(v)s) ON CONFLICT (naam) DO NOTHING",
             {"n": naam, "v": volgorde})
    clear_lijsten()


# ---------- Thema's ----------

def voeg_thema_toe(naam):
    naam = (naam or "").strip()
    if not naam:
        raise ValueError("Vul een thema in.")
    _execute("INSERT INTO kaart_themas (naam) VALUES (%(n)s) ON CONFLICT (naam) DO NOTHING", {"n": naam})
    clear_lijsten()


# ---------- Uitgevers van ansichtkaarten ----------

def zoek_uitgevers(tekst, limiet=200):
    tekst = (tekst or "").strip()
    return _query(
        """
        SELECT u.id, u.naam, u.plaats, (SELECT count(*) FROM kaarten k WHERE k.uitgever_id = u.id) AS kaarten
        FROM kaart_uitgevers u
        WHERE %(t)s = '' OR u.naam ILIKE %(l)s OR u.plaats ILIKE %(l)s
        ORDER BY lower(u.naam), lower(u.plaats) LIMIT %(lim)s
        """,
        {"t": tekst, "l": f"%{tekst}%", "lim": limiet},
    )


def aantal_uitgevers():
    return _query("SELECT count(*) AS n FROM kaart_uitgevers")[0]["n"]


def voeg_uitgever_toe(naam, plaats=""):
    naam = (naam or "").strip()
    if not naam:
        raise ValueError("Vul een naam in.")
    n = _execute("INSERT INTO kaart_uitgevers (naam, plaats) VALUES (%(n)s, %(p)s) ON CONFLICT (naam, plaats) DO NOTHING",
                 {"n": naam, "p": (plaats or "").strip()})
    clear_lijsten()
    if not n:
        raise ValueError("Deze uitgever staat er al.")


def wijzig_uitgever(uitgever_id, naam, plaats):
    naam = (naam or "").strip()
    if not naam:
        raise ValueError("De naam mag niet leeg zijn.")
    try:
        _execute("UPDATE kaart_uitgevers SET naam = %(n)s, plaats = %(p)s WHERE id = %(i)s",
                 {"n": naam, "p": (plaats or "").strip(), "i": uitgever_id})
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise ValueError(f"{uitgever_label(naam, plaats)} bestaat al.")
        raise
    clear_lijsten()


def verwijder_uitgever(uitgever_id):
    gebruikt = _query("SELECT count(*) AS n FROM kaarten WHERE uitgever_id = %(i)s", {"i": uitgever_id})[0]["n"]
    if gebruikt:
        raise ValueError(f"Er hangen nog {gebruikt} kaarten aan deze uitgever.")
    _execute("DELETE FROM kaart_uitgevers WHERE id = %(i)s", {"i": uitgever_id})
    clear_lijsten()

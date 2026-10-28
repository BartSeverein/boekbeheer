"""
webshop/translate.py — vertaalt boekbeschrijvingen en categorienamen met DeepL, en onthoudt alles.

- Elke vertaling wordt bewaard (in Postgres, tabel shop_translations), met als sleutel de taal plus de hash van de
  Nederlandse brontekst. Een boek wordt dus maar één keer vertaald; pas als je de beschrijving wijzigt, wordt het opnieuw gedaan.
- Per run is er een tekenbudget (WEBSHOP_DEEPL_MAX_CHARS, standaard 100.000) en er is een grens over alle runs samen
  (WEBSHOP_DEEPL_TOTAL_CHARS, standaard 900.000: alles wat ooit vertaald is, opgeteld), zodat een tegoed van 1.000.000 nooit op raakt. Is dat op, dan stopt het vertalen en
  tonen de pagina's tot de volgende run de Nederlandse tekst. Zo loopt een grote achterstand rustig leeg
  en kun je nooit onverwacht een rekening krijgen.
- Zonder sleutel (of in een proefrun) wordt er niets naar DeepL gestuurd; alleen wat al bewaard is wordt gebruikt.
- DeepL geeft bij een afgelopen tegoed of een foute sleutel een foutcode; dan stopt het vertalen voor de rest van de run
  en bouwt de site gewoon verder met de Nederlandse tekst.
"""
import hashlib
import json
import os
import time

DEEPL_TARGET = {"en": "EN-GB", "de": "DE", "fr": "FR"}
BATCH_TEXTS = 40
BATCH_CHARS = 30000


def src_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class JsonStore:
    """Eenvoudige opslag in een JSON-bestand (voor tests)."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [tuple(r) for r in json.load(f)]
        except (OSError, ValueError):
            return []

    def save(self, rows):
        have = self.load()
        have.extend(rows)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(have, f, ensure_ascii=False)


class PgStore:
    """Opslag in Postgres: tabel shop_translations(lang, src_hash, src, tgt)."""

    def __init__(self, conn):
        self.conn = conn
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS shop_translations (
                    lang TEXT NOT NULL, src_hash TEXT NOT NULL, src TEXT NOT NULL, tgt TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (lang, src_hash));
                ALTER TABLE shop_translations ENABLE ROW LEVEL SECURITY;
                """
            )
        conn.commit()

    def load(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT lang, src_hash, tgt, length(src) AS n FROM shop_translations")
            rows = cur.fetchall()
        self.conn.commit()
        return [(r["lang"], r["src_hash"], r["tgt"], int(r["n"])) for r in rows]

    def save(self, rows):
        # rows: (lang, hash, tgt, src)
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO shop_translations (lang, src_hash, tgt, src) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [(l, h, tgt, src) for (l, h, tgt, src) in rows],
            )
        self.conn.commit()


class Translator:
    def __init__(self, api_key="", store=None, max_chars=100_000, readonly=False, url=None, log=print, post=None, total_cap=900_000):
        self.key = (api_key or "").strip()
        self.store = store
        self.max_chars = int(max_chars)
        self.total_cap = int(total_cap)
        self.total = 0         # tekens die ooit via deze bewaarplaats naar DeepL zijn gegaan (alle runs samen)
        self.readonly = readonly
        self.log = log
        self.url = url or os.environ.get("WEBSHOP_DEEPL_URL") or (
            "https://api-free.deepl.com/v2/translate" if self.key.endswith(":fx") else "https://api.deepl.com/v2/translate")
        self._post = post  # voor tests
        self.cache = {}
        self.used = 0          # tekens naar DeepL gestuurd in deze run
        self.new_texts = 0
        self.stopped = ""      # reden waarom het vertalen is gestopt (leeg = nog actief)
        self.missing = 0       # teksten die nog niet vertaald zijn
        self.missing_chars = 0  # en hoeveel tekens dat samen is (per taal geteld, zoals DeepL rekent)
        if store:
            for row in store.load():
                self.cache[(row[0], row[1])] = row[2]
                if len(row) > 3:
                    self.total += row[3] if isinstance(row[3], int) else len(row[3])

    @property
    def active(self):
        return bool(self.key) and not self.readonly and not self.stopped

    def get(self, text, lang):
        """Vertaling uit de bewaarplaats, of None."""
        if not text or not text.strip():
            return None
        return self.cache.get((lang, src_hash(text)))

    def ensure(self, texts, langs):
        """Vertaalt wat nog niet bewaard is, binnen het budget. Geeft het aantal nieuw vertaalde teksten terug."""
        done = 0
        uniq = list(dict.fromkeys(t for t in texts if t and t.strip()))
        for lang in langs:
            if lang not in DEEPL_TARGET:
                continue
            todo = [t for t in uniq if (lang, src_hash(t)) not in self.cache]
            if not todo:
                continue
            if not self.active:
                self.missing += len(todo)
                self.missing_chars += sum(len(x) for x in todo)
                continue
            batch, chars = [], 0
            for t in todo:
                if batch and (len(batch) >= BATCH_TEXTS or chars + len(t) > BATCH_CHARS):
                    done += self._run(batch, lang)
                    batch, chars = [], 0
                batch.append(t)
                chars += len(t)
            if batch:
                done += self._run(batch, lang)
        return done

    # ---- intern
    def _run(self, batch, lang):
        if not self.active:
            self._miss(batch)
            return 0
        need = sum(len(t) for t in batch)
        if self.total + self.used + need > self.total_cap:
            self.stopped = f"de grens van {self.total_cap:,} tekens in totaal (WEBSHOP_DEEPL_TOTAL_CHARS) is bereikt".replace(",", ".")
            self._miss(batch)
            return 0
        if self.used + need > self.max_chars:
            self.stopped = f"tekenbudget van {self.max_chars:,} tekens voor deze run is op".replace(",", ".")
            self._miss(batch)
            return 0
        out = self._call(batch, lang)
        if out is None:
            self._miss(batch)
            return 0
        self.used += need
        rows = []
        for src, tgt in zip(batch, out):
            self.cache[(lang, src_hash(src))] = tgt
            rows.append((lang, src_hash(src), tgt, src))
        if self.store:
            self.store.save(rows)
        self.new_texts += len(rows)
        return len(rows)

    def _miss(self, batch):
        self.missing += len(batch)
        self.missing_chars += sum(len(x) for x in batch)

    def _call(self, batch, lang):
        body = {"text": batch, "source_lang": "NL", "target_lang": DEEPL_TARGET[lang], "preserve_formatting": True}
        headers = {"Authorization": f"DeepL-Auth-Key {self.key}", "Content-Type": "application/json"}
        poster = self._post
        if poster is None:
            import requests
            poster = lambda u, b, h: requests.post(u, json=b, headers=h, timeout=60)  # noqa: E731
        last = ""
        for attempt in range(4):
            try:
                resp = poster(self.url, body, headers)
            except Exception as exc:  # netwerkfout
                last = f"netwerkfout: {exc}"
                time.sleep(2 * (attempt + 1))
                continue
            code = resp.status_code
            if code == 200:
                try:
                    items = resp.json()["translations"]
                    texts = [i["text"] for i in items]
                except Exception:
                    last = "onverwacht antwoord van DeepL"
                    break
                if len(texts) != len(batch):
                    last = "DeepL gaf een ander aantal teksten terug"
                    break
                return texts
            if code == 456:
                self.stopped = "het DeepL-tegoed (tekens) is op"
                self.log(f"DeepL: {self.stopped}; we gaan verder met de Nederlandse tekst.")
                return None
            if code in (401, 403):
                self.stopped = "DeepL weigert de sleutel (WEBSHOP_DEEPL_KEY controleren)"
                self.log(f"DeepL: {self.stopped}.")
                return None
            if code in (429, 500, 502, 503, 504):
                last = f"HTTP {code}"
                time.sleep(3 * (attempt + 1))
                continue
            last = f"HTTP {code}"
            break
        self.log(f"DeepL: vertalen mislukt ({last}).")
        self._fails = getattr(self, "_fails", 0) + 1
        if self._fails >= 3:
            self.stopped = "DeepL gaf drie keer een fout"
        return None

    def summary(self):
        text = f"DeepL: {self.new_texts} nieuwe teksten vertaald ({self.used} tekens)"
        if self.missing:
            text += ", " + f"{self.missing} nog te vertalen (samen {self.missing_chars:,} tekens)".replace(",", ".")
        if self.stopped:
            text += f" — gestopt: {self.stopped}"
        return text + "."

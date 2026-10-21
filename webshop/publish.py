"""
webshop/publish.py — het hele traject in één commando: orders overnemen → site bouwen → naar de server zetten.

Volgorde is belangrijk: eerst de betaalde webshop-bestellingen verwerken in de voorraad, dan pas bouwen, zodat de nieuwe
catalogus (voorraad) en de lijst 'al verwerkte bestellingen' bij elkaar passen. Zie ook de uitleg in de README.
"""
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from pathlib import Path

from .build import build_site, load_from_postgres
from .common import shop_config
from .deploy import (DeployError, connect_from_env, deploy, manifest_views, read_manifest)
from .orders import import_shop_orders, sync_intime_status


def _download_shop_db(remote, cfg, log):
    rel = f"d-{cfg['data_key']}/shop.sqlite"
    for attempt in range(3):
        raw = remote.read_bytes(rel)
        if raw is None:
            return None
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        Path(path).write_bytes(raw)
        try:
            con = sqlite3.connect(path)
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            con.close()
        except sqlite3.Error:
            ok = False
        if ok:
            return path
        os.unlink(path)
        time.sleep(2)   # mogelijk midden in een schrijfactie gelezen: opnieuw
    raise DeployError("shop.sqlite op de server is niet te lezen (corrupt of steeds in gebruik); er is niets gewijzigd.")


def publish(mode="full", execute=False, max_minutes=120, allow_overwrite=False, out_dir="webshop_dist", remote=None, conn=None,
            update_book=None, log=print):
    t0 = time.time()
    cfg = shop_config()
    own_conn = conn is None
    if conn is None:
        from db import get_connection
        conn = get_connection()
    remote = remote or connect_from_env()
    summary = {}
    try:
        manifest = read_manifest(remote)
        images, pages = manifest_views(manifest)

        # 1. betaalde bestellingen overnemen
        shop_db = _download_shop_db(remote, cfg, log)
        if shop_db:
            new, done, attention = import_shop_orders(conn, shop_db, execute=execute, update_book=update_book, log=log)
            log(f"Webshop-bestellingen: {new} nieuw{' (proefrun, niets verwerkt)' if not execute else ''}, voorraad aangepast voor {done} eenheden.")
            summary.update(new_orders=new, stock_updates=done)
            if execute and attention:
                import sync
                sync._log(conn, "pull", "orders", "error", "\n".join(attention), platform="Webshop")
                conn.commit()
            if cfg.get("intime_key"):
                try:
                    n_ship = sync_intime_status(conn, shop_db, cfg["intime_key"], execute=execute, log=log)
                    log(f"Intime: {n_ship} webshop-bestelling(en) op 'verstuurd' gezet.")
                except Exception as exc:  # een storing bij Intime mag het bijwerken van de webshop nooit tegenhouden
                    log(f"Intime-terugkoppeling overgeslagen: {exc}")
            os.unlink(shop_db)
        else:
            log("Nog geen shop.sqlite op de server (nog geen bestellingen).")

        # 2. bouwen
        books, imported, fetch_image = load_from_postgres(conn)
        try:   # kortingscodes uit de dashboard-instellingen mee de catalogus in
            import sync
            from .common import DISCOUNT_SETTING, parse_discount_codes
            cfg["discount_codes"] = parse_discount_codes(sync._get_setting(conn, DISCOUNT_SETTING, "[]"))
            log(f"Kortingscodes: {len(cfg['discount_codes'])} actief.")
        except Exception as exc:   # codes zijn een extraatje: een fout hier mag het publiceren niet stoppen
            cfg["discount_codes"] = []
            log(f"Kortingscodes overgeslagen: {exc}")
        image_deadline = time.time() + max_minutes * 60 * 0.4
        translator = None
        if mode == "full" and len(cfg["langs"]) > 1:
            from .translate import PgStore, Translator
            # Zonder sleutel of in een proefrun wordt er niets naar DeepL gestuurd; bewaarde vertalingen worden wel gebruikt.
            translator = Translator(cfg["deepl_key"], PgStore(conn), cfg["deepl_max_chars"], readonly=not execute, log=log,
                                    total_cap=cfg["deepl_total_chars"])
            if not cfg["deepl_key"]:
                log("Geen WEBSHOP_DEEPL_KEY: beschrijvingen blijven Nederlands op de andere talen.")
        stats = build_site(books, out_dir, cfg, mode=mode, imported_order_ids=imported, known_images=images,
                           known_pages=pages, fetch_image=fetch_image, process_images=execute,
                           image_deadline=image_deadline, translator=translator, log=log)
        log(f"Gebouwd: {stats.get('books')} boeken, {stats.get('pages', 0)} pagina's in {stats.get('seconds')} s.")

        # 3. uploaden
        remaining = max(60, max_minutes * 60 - (time.time() - t0))
        result = deploy(remote, out_dir, mode=mode, execute=execute, keep=stats.get("keep", []), time_budget=remaining,
                        allow_overwrite=allow_overwrite or os.environ.get("WEBSHOP_ALLOW_OVERWRITE") == "1", log=log)
        changed = result.pop("changed_rels", [])
        summary.update(result)
        if execute and mode == "full" and changed:
            try:
                from .indexnow import notify
                notify(cfg, changed, log=log)
            except Exception as exc:   # een melding aan zoekmachines mag het publiceren nooit tegenhouden
                log(f"IndexNow overgeslagen: {exc}")
        if execute and mode == "full":
            try:   # controle van de foutpagina: staat de .htaccess goed en bestaat 404.html op de server?
                local_ht = (Path(out_dir) / ".htaccess").read_bytes()
                remote_ht = remote.read_bytes(".htaccess") or b""
                if remote_ht != local_ht:
                    remote.put(".htaccess", data=local_ht)
                    log(".htaccess op de server week af van de nieuwe en is vervangen.")
                has_err = b"ErrorDocument 404 /404.html" in remote_ht
                log(f"Foutpagina: .htaccess bevat ErrorDocument = {'ja' if has_err else 'nee (nu hersteld)'}; "
                    f"404.html op de server = {'ja' if remote.exists('404.html') else 'NEE'}.")
            except Exception as exc:
                log(f"Foutpagina-controle overgeslagen: {exc}")
            try:   # live controle vanaf de GitHub-server: toont de site de eigen 404-pagina, en is de datamap afgesloten?
                import secrets
                import urllib.error
                import urllib.request

                def _probe(url):
                    req = urllib.request.Request(url, headers={"User-Agent": "severein-controle"})
                    try:
                        with urllib.request.urlopen(req, timeout=20) as r:
                            return r.status, r.read(30000).decode("utf-8", "replace"), dict(r.headers)
                    except urllib.error.HTTPError as e:
                        return e.code, e.read(30000).decode("utf-8", "replace"), dict(e.headers)

                base = cfg["base_url"].rstrip("/")
                code, body, hdr = _probe(f"{base}/bestaat-niet-{secrets.token_hex(4)}")
                own = "Pagina niet gevonden" in body
                heads = {k.lower(): v for k, v in hdr.items()}
                log(f"Live controle onbekend adres: HTTP {code}; eigen foutpagina = {'ja' if own else 'NEE (standaard serverpagina)'}; "
                    f"server = {heads.get('server', '?')}; .htaccess-header (nosniff) = {'ja' if 'x-content-type-options' in heads else 'NEE'}.")
                for name in ("catalog.sqlite", "shop.sqlite"):
                    c2, _b, _h = _probe(f"{base}/d-{cfg['data_key']}/{name}")
                    log(f"Live controle datamap ({name}): HTTP {c2} " + ("— GOED, afgesloten." if c2 in (401, 403, 404) else "— LET OP: bereikbaar vanaf internet!"))
            except Exception as exc:
                log(f"Live controle overgeslagen: {exc}")
        if execute:
            import sync
            status = "ok" if result["complete"] else "error"
            detail = (f"webshop {mode}: {result['uploaded']} geüpload, {result['deleted']} verwijderd, {result['failed']} mislukt"
                      + ("" if result["complete"] else " (niet compleet; volgende run gaat verder)"))
            # Een niet-complete run is bij de eerste grote upload normaal; alleen echte mislukkingen geven een foutmelding.
            sync._log(conn, "push", "webshop", "error" if result["failed"] else "ok", detail, platform="Webshop")
            conn.commit()
        return summary
    finally:
        if own_conn:
            conn.close()

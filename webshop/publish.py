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

from .build import build_site, load_from_postgres
from .common import shop_config
from .deploy import (DeployError, connect_from_env, deploy, manifest_views, read_manifest)
from .orders import import_shop_orders


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
            os.unlink(shop_db)
        else:
            log("Nog geen shop.sqlite op de server (nog geen bestellingen).")

        # 2. bouwen
        books, imported, fetch_image = load_from_postgres(conn)
        image_deadline = time.time() + max_minutes * 60 * 0.4
        stats = build_site(books, out_dir, cfg, mode=mode, imported_order_ids=imported, known_images=images,
                           known_pages=pages, fetch_image=fetch_image, process_images=execute,
                           image_deadline=image_deadline, log=log)
        log(f"Gebouwd: {stats.get('books')} boeken, {stats.get('pages', 0)} pagina's in {stats.get('seconds')} s.")

        # 3. uploaden
        remaining = max(60, max_minutes * 60 - (time.time() - t0))
        result = deploy(remote, out_dir, mode=mode, execute=execute, keep=stats.get("keep", []), time_budget=remaining,
                        allow_overwrite=allow_overwrite or os.environ.get("WEBSHOP_ALLOW_OVERWRITE") == "1", log=log)
        summary.update(result)
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

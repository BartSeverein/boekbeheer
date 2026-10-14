"""
webshop/deploy.py — de gebouwde site via SFTP op de hosting zetten.

Uitgangspunten:
  - Alleen schrijven in de webroot (de map waar de SFTP-gebruiker in terechtkomt, of WEBSHOP_SFTP_ROOT).
  - Op de server staat een manifest (.shop-manifest.json) met per bestand een hash. Daarmee uploaden we alleen wat nieuw of
    veranderd is, en verwijderen we alleen bestanden die wíj eerder hebben geplaatst — nooit iets anders.
  - Elk bestand gaat eerst naar een tijdelijke naam en wordt daarna hernoemd, zodat bezoekers nooit een half bestand zien.
  - Volgorde: afbeeldingen, css/js, pagina's, PHP, en als allerlaatste de catalogus. De catalogus verwijst naar pagina's en
    afbeeldingen, dus die moeten er al staan.
  - Een run kan een tijdslimiet hebben en later gewoon verdergaan (de eerste upload van tienduizenden bestanden duurt lang).
"""
import hashlib
import io
import json
import os
import posixpath
import stat as statmod
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MANIFEST = ".shop-manifest.json"
WORKERS = max(1, int(os.environ.get("WEBSHOP_SFTP_WORKERS") or 4))


class DeployError(Exception):
    pass


class SftpRemote:
    """Dunne laag om een paramiko-SFTP-verbinding. 'factory' levert per thread een eigen SFTP-client."""

    def __init__(self, factory, root="."):
        self._factory = factory
        self._local = threading.local()
        self.root = root.rstrip("/") or "."
        self._dirs = set()

    @property
    def sftp(self):
        if not hasattr(self._local, "c"):
            self._local.c = self._factory()
        return self._local.c

    def _p(self, rel):
        return posixpath.join(self.root, rel) if self.root not in ("", ".") else rel

    def exists(self, rel):
        try:
            self.sftp.stat(self._p(rel))
            return True
        except IOError:
            return False

    def read_bytes(self, rel):
        try:
            with self.sftp.open(self._p(rel), "rb") as fh:
                return fh.read()
        except IOError:
            return None

    def _mkdirs(self, rel_dir):
        if rel_dir in ("", "."):
            return
        cache = self._dirs
        if rel_dir in cache:
            return
        parent = posixpath.dirname(rel_dir)
        self._mkdirs(parent)
        try:
            self.sftp.stat(self._p(rel_dir))
        except IOError:
            try:
                self.sftp.mkdir(self._p(rel_dir))
            except IOError:
                self.sftp.stat(self._p(rel_dir))   # bestond intussen (andere thread); anders echte fout
        cache.add(rel_dir)

    def put(self, rel, data=None, local_path=None):
        self._mkdirs(posixpath.dirname(rel))
        tmp = self._p(rel) + ".part"
        if local_path is not None:
            self.sftp.put(str(local_path), tmp)
        else:
            with self.sftp.open(tmp, "wb") as fh:
                fh.write(data)
        try:
            self.sftp.posix_rename(tmp, self._p(rel))
        except (IOError, AttributeError):
            try:
                self.sftp.remove(self._p(rel))
            except IOError:
                pass
            self.sftp.rename(tmp, self._p(rel))

    def delete(self, rel):
        try:
            self.sftp.remove(self._p(rel))
        except IOError:
            pass

    def close(self):
        pass


def connect_from_env():
    """Maakt een SftpRemote uit WEBSHOP_SFTP_HOST/PORT/USER/PASS (of WEBSHOP_SFTP_KEY, een PEM-tekst)."""
    import paramiko
    host = os.environ.get("WEBSHOP_SFTP_HOST")
    user = os.environ.get("WEBSHOP_SFTP_USER")
    if not host or not user:
        raise DeployError("WEBSHOP_SFTP_HOST en WEBSHOP_SFTP_USER ontbreken.")
    port = int(os.environ.get("WEBSHOP_SFTP_PORT") or 22)
    password = os.environ.get("WEBSHOP_SFTP_PASS") or None
    key_text = os.environ.get("WEBSHOP_SFTP_KEY") or None
    pkey = None
    if key_text:
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                pkey = cls.from_private_key(io.StringIO(key_text))
                break
            except Exception:
                continue
        if pkey is None:
            raise DeployError("WEBSHOP_SFTP_KEY is geen geldige privésleutel.")
    if pkey is None and not password:
        raise DeployError(
            "Geen inloggegevens: WEBSHOP_SFTP_KEY (privésleutel) of WEBSHOP_SFTP_PASS ontbreekt in de omgeving van deze run. "
            "Controleer of het secret bestaat en of de workflow het doorgeeft.")
    if os.environ.get("WEBSHOP_SFTP_DEBUG") == "1":
        import logging
        logging.basicConfig(level=logging.DEBUG)
    try:
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = 30
        transport.connect(username=user, password=password, pkey=pkey)
    except Exception as e:
        raise DeployError(f"Verbinden met {host}:{port} als {user} mislukt: {type(e).__name__}: {e}")
    if not transport.is_authenticated():
        raise DeployError("Inloggen is niet gelukt (de server heeft de sleutel of het wachtwoord niet geaccepteerd).")
    server_version = transport.remote_version

    def factory():
        try:
            return paramiko.SFTPClient.from_transport(transport)
        except Exception as e:
            raise DeployError(
                f"Ingelogd, maar de server wil geen SFTP-sessie openen ({type(e).__name__}: {e}). Server: {server_version}. "
                "Mogelijke oorzaken: SFTP staat niet aan voor deze gebruiker, of de server staat maar één sessie tegelijk toe "
                "(zet dan WEBSHOP_SFTP_WORKERS=1).")

    return SftpRemote(factory, os.environ.get("WEBSHOP_SFTP_ROOT") or ".")


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(remote):
    raw = remote.read_bytes(MANIFEST)
    if not raw:
        return {}
    try:
        return dict(json.loads(raw.decode("utf-8")).get("files", {}))
    except Exception:
        raise DeployError("Het manifest op de server is onleesbaar; haal het handmatig weg om opnieuw te beginnen.")


def write_manifest(remote, files):
    remote.put(MANIFEST, data=json.dumps({"files": files, "saved": int(time.time())}, separators=(",", ":")).encode())


def _priority(rel):
    if rel.startswith("i/"):
        return 0
    if rel.endswith((".css", ".js")):
        return 1
    if rel.endswith(".html") or rel in ("sitemap.xml", "robots.txt"):
        return 2
    if rel.endswith("catalog.sqlite"):
        return 5
    if rel.endswith(".php") or rel.endswith(".htaccess"):
        return 4
    return 3


def plan(dist_dir, manifest, keep=()):
    """Geeft (te_uploaden [(rel, pad, hash)], ongewijzigd, te_verwijderen [rel]) terug."""
    dist = Path(dist_dir)
    local = {}
    for p in dist.rglob("*"):
        if p.is_file() and not p.name.startswith(".build"):
            local[p.relative_to(dist).as_posix()] = p
    upload, same = [], 0
    for rel, p in local.items():
        h = sha1_file(p)
        if manifest.get(rel) == h:
            same += 1
        else:
            upload.append((rel, p, h))
    upload.sort(key=lambda x: (_priority(x[0]), x[0]))
    keep = {k.lstrip("/") for k in keep}
    stale = sorted(rel for rel in manifest if rel not in local and rel not in keep)
    return upload, same, stale


def deploy(remote, dist_dir, *, mode="full", execute=False, keep=(), time_budget=None, allow_overwrite=False,
           max_delete_fraction=0.2, log=print):
    dist = Path(dist_dir)
    manifest = read_manifest(remote)
    if mode == "catalog":
        # alleen de catalogus (en niets verwijderen)
        files = {p.relative_to(dist).as_posix(): p for p in dist.rglob("catalog.sqlite")}
        upload = [(rel, p, sha1_file(p)) for rel, p in files.items()]
        same, stale = 0, []
    else:
        upload, same, stale = plan(dist, manifest, keep)

    if not manifest and mode == "full":
        for name in ("index.html", ".htaccess"):
            if remote.exists(name) and not allow_overwrite:
                raise DeployError(
                    f"Op de server staat al een '{name}' die niet van deze webshop is. Controleer of dit echt de lege webroot van "
                    "de shop is (en niet van je bestaande website). Klopt het, zet dan WEBSHOP_ALLOW_OVERWRITE=1 en start opnieuw.")
    log(f"Plan: {len(upload)} te uploaden, {same} ongewijzigd, {len(stale)} te verwijderen.")
    if not execute:
        for rel, _, _ in upload[:10]:
            log(f"  zou uploaden: {rel}")
        return {"uploaded": 0, "planned": len(upload), "deleted": 0, "failed": 0, "complete": not upload, "dry_run": True}

    deadline = time.time() + time_budget if time_budget else None
    files_state = dict(manifest)
    failed, uploaded, lock = [], [0], threading.Lock()
    stopped = [False]

    def work(item):
        rel, path, h = item
        if stopped[0] or (deadline and time.time() > deadline):
            stopped[0] = True
            return None
        for attempt in range(3):
            try:
                remote.put(rel, local_path=path)
                return (rel, h)
            except Exception as e:  # netwerkhik: opnieuw proberen
                err = e
                time.sleep(1 + attempt)
        with lock:
            failed.append(f"{rel}: {err}")
        return None

    # Batches, zodat het manifest regelmatig wordt weggeschreven (een afgebroken run kan dan verder).
    # De catalogus gaat pas mee als al het andere gelukt is.
    first = [u for u in upload if _priority(u[0]) < 5]
    last = [u for u in upload if _priority(u[0]) >= 5]
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        for start in range(0, len(first), 200):
            for res in pool.map(work, first[start:start + 200]):
                if res:
                    files_state[res[0]] = res[1]
                    uploaded[0] += 1
            write_manifest(remote, files_state)
            if stopped[0]:
                break
            if start and start % 2000 == 0:
                log(f"  {uploaded[0]} van {len(first)} geüpload...")
        complete = not stopped[0] and not failed
        if complete:
            for res in pool.map(work, last):
                if res:
                    files_state[res[0]] = res[1]
                    uploaded[0] += 1
            complete = not failed and not stopped[0]
    finally:
        pool.shutdown()

    deleted = 0
    if complete and mode == "full" and stale:
        if len(stale) > max(50, int(len(manifest) * max_delete_fraction)):
            log(f"LET OP: {len(stale)} bestanden zouden verwijderd worden (meer dan toegestaan). Niets verwijderd; controleer de build.")
        else:
            for rel in stale:
                remote.delete(rel)
                files_state.pop(rel, None)
                deleted += 1
    write_manifest(remote, files_state)
    log(f"Klaar: {uploaded[0]} geüpload, {deleted} verwijderd, {len(failed)} mislukt" + ("" if complete else " — NIET compleet, volgende run gaat verder."))
    for f in failed[:10]:
        log("  mislukt: " + f)
    return {"uploaded": uploaded[0], "deleted": deleted, "failed": len(failed), "complete": complete, "dry_run": False}


def manifest_views(manifest):
    """(bekende afbeeldingspaden, bekende boekpagina's) uit het manifest, voor build_site."""
    images = [k for k in manifest if k.startswith("i/")]
    pages = {"/" + k[: -len("index.html")] for k in manifest if k.startswith("boek/") and k.endswith("/index.html")}
    return images, pages

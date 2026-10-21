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
WORKERS = max(1, int(os.environ.get("WEBSHOP_SFTP_WORKERS") or 2))


class DeployError(Exception):
    pass


def _is_conn_error(e):
    """Verbindingsproblemen (herstelbaar door opnieuw te verbinden) versus gewone bestandsfouten (bestaat niet, geen rechten)."""
    import socket
    try:
        import paramiko
        ssh_errors = (paramiko.SSHException,)
    except ImportError:  # in tests zonder paramiko
        ssh_errors = ()
    if isinstance(e, (EOFError, socket.timeout) + ssh_errors):
        return True
    if isinstance(e, OSError):
        return e.errno in (None, 32, 54, 104, 110)   # geen errno / broken pipe / reset by peer / timeout
    return False


class SftpRemote:
    """
    Dunne laag om een paramiko-SFTP-verbinding. 'factory' levert een SFTP-client voor de huidige verbinding, 'reset' bouwt
    de verbinding opnieuw op (een verbinding die lang stil staat, bijvoorbeeld tijdens het bouwen, wordt door veel
    servers verbroken). Elke bewerking wordt bij een verbindingsfout na opnieuw verbinden herhaald.
    """

    def __init__(self, factory, root=".", reset=None):
        self._factory = factory
        self._reset_fn = reset
        self._local = threading.local()
        self._lock = threading.Lock()
        self._gen = 0
        self.root = root.rstrip("/") or "."
        self._dirs = set()

    @property
    def sftp(self):
        if getattr(self._local, "gen", None) != self._gen or not hasattr(self._local, "c"):
            self._local.gen = self._gen
            self._local.c = self._factory()
        return self._local.c

    def _run(self, fn, attempts=4):
        last = None
        for i in range(attempts):
            gen = self._gen
            try:
                return fn(self.sftp)
            except Exception as e:
                if not _is_conn_error(e):
                    raise
                last = e
                time.sleep(min(2 * i, 6))
                with self._lock:
                    if self._gen == gen and self._reset_fn:   # de eerste thread die het merkt verbindt opnieuw
                        try:
                            self._reset_fn()
                        except Exception as e2:
                            last = e2
                        self._gen += 1
        raise last

    def _p(self, rel):
        return posixpath.join(self.root, rel) if self.root not in ("", ".") else rel

    def exists(self, rel):
        def fn(c):
            try:
                c.stat(self._p(rel))
                return True
            except IOError as e:
                if e.errno == 2:
                    return False
                raise
        return self._run(fn)

    def read_bytes(self, rel):
        def fn(c):
            try:
                with c.open(self._p(rel), "rb") as fh:
                    return fh.read()
            except IOError as e:
                if e.errno == 2:
                    return None
                raise
        return self._run(fn)

    def _mkdirs(self, c, rel_dir):
        if rel_dir in ("", ".") or rel_dir in self._dirs:
            return
        self._mkdirs(c, posixpath.dirname(rel_dir))
        try:
            c.stat(self._p(rel_dir))
        except IOError:
            try:
                c.mkdir(self._p(rel_dir))
            except IOError:
                c.stat(self._p(rel_dir))   # bestond intussen (andere thread); anders echte fout
        self._dirs.add(rel_dir)

    def put(self, rel, data=None, local_path=None):
        def fn(c):
            self._mkdirs(c, posixpath.dirname(rel))
            tmp = self._p(rel) + ".part"
            if local_path is not None:
                c.put(str(local_path), tmp)
            else:
                with c.open(tmp, "wb") as fh:
                    fh.write(data)
            try:
                c.posix_rename(tmp, self._p(rel))
            except (IOError, AttributeError):
                try:
                    c.remove(self._p(rel))
                except IOError:
                    pass
                c.rename(tmp, self._p(rel))
        return self._run(fn)

    def delete(self, rel):
        def fn(c):
            try:
                c.remove(self._p(rel))
            except IOError as e:
                if e.errno != 2:
                    raise
        return self._run(fn)

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
    state = {}

    def open_transport():
        try:
            transport = paramiko.Transport((host, port))
            transport.banner_timeout = 30
            transport.connect(username=user, password=password, pkey=pkey)
        except Exception as e:
            raise DeployError(f"Verbinden met {host}:{port} als {user} mislukt: {type(e).__name__}: {e}")
        if not transport.is_authenticated():
            raise DeployError("Inloggen is niet gelukt (de server heeft de sleutel of het wachtwoord niet geaccepteerd).")
        state["transport"] = transport
        state["version"] = transport.remote_version

    def reset():
        old = state.get("transport")
        try:
            if old:
                old.close()
        except Exception:
            pass
        open_transport()

    open_transport()

    def factory():
        try:
            return paramiko.SFTPClient.from_transport(state["transport"])
        except Exception as e:
            if _is_conn_error(e):
                raise   # verbinding weg: SftpRemote verbindt opnieuw
            raise DeployError(
                f"Ingelogd, maar de server wil geen SFTP-sessie openen ({type(e).__name__}: {e}). Server: {state.get('version')}. "
                "Mogelijke oorzaken: SFTP staat niet aan voor deze gebruiker, of de server staat maar één sessie tegelijk toe "
                "(zet dan WEBSHOP_SFTP_WORKERS=1).")

    return SftpRemote(factory, os.environ.get("WEBSHOP_SFTP_ROOT") or ".", reset=reset)


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


def save_manifest(remote, files, log=print):
    """Schrijft het manifest weg; een mislukte poging stopt de deploy niet (de voortgang wordt bij de volgende batch bewaard)."""
    try:
        write_manifest(remote, files)
        return True
    except Exception as e:
        log(f"  waarschuwing: manifest opslaan mislukt ({type(e).__name__}: {e})")
        return False


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
        # de catalogus (altijd) plus de kleine, snel wijzigende bestanden die de bouw in dit geval meelevert (script, stijl, PHP, .htaccess):
        # die alleen als ze afwijken van wat op de server staat. Er wordt niets verwijderd.
        upload, same, stale = [], 0, []
        for p in dist.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(dist).as_posix()
            h = sha1_file(p)
            if rel.endswith("catalog.sqlite") or manifest.get(rel) != h:
                upload.append((rel, p, h))
            else:
                same += 1
        upload.sort(key=lambda x: (_priority(x[0]), x[0]))
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
    uploaded_rels, deleted_rels = [], []
    stopped = [False]

    consecutive = [0]

    def work(item):
        rel, path, h = item
        if stopped[0] or (deadline and time.time() > deadline):
            stopped[0] = True
            return None
        err = None
        for attempt in range(2):
            try:
                remote.put(rel, local_path=path)
                consecutive[0] = 0
                return (rel, h)
            except Exception as e:
                err = e
                time.sleep(1 + attempt)
        with lock:
            failed.append(f"{rel}: {type(err).__name__}: {err}")
            consecutive[0] += 1
            if consecutive[0] >= 10:   # de server is er niet meer: niet de hele lijst nutteloos afwerken
                stopped[0] = True
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
                    uploaded_rels.append(res[0])
            save_manifest(remote, files_state, log)
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
                    uploaded_rels.append(res[0])
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
                deleted_rels.append(rel)
    save_manifest(remote, files_state, log)
    log(f"Klaar: {uploaded[0]} geüpload, {deleted} verwijderd, {len(failed)} mislukt" + ("" if complete else " — NIET compleet, volgende run gaat verder."))
    for f in failed[:10]:
        log("  mislukt: " + f)
    return {"uploaded": uploaded[0], "deleted": deleted, "failed": len(failed), "complete": complete, "dry_run": False,
            "changed_rels": uploaded_rels + deleted_rels}


def manifest_views(manifest):
    """(bekende afbeeldingspaden, bekende boekpagina's) uit het manifest, voor build_site."""
    images = [k for k in manifest if k.startswith("i/")]
    pages = {"/" + k[: -len("index.html")] for k in manifest if k.startswith("boek/") and k.endswith("/index.html")}
    return images, pages

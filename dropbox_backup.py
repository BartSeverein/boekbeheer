"""
dropbox_backup.py — Zet de dagelijkse back-up in Dropbox (een eigen 'app-map', niet openbaar gedeeld).

Benodigde omgevingsvariabelen (GitHub Actions-secrets):
    DROPBOX_APP_KEY        (app key van je Dropbox-app)
    DROPBOX_APP_SECRET     (app secret)
    DROPBOX_REFRESH_TOKEN  (eenmalig verkregen met dropbox_auth.py; verloopt niet vanzelf)

De Dropbox-app heeft bij voorkeur toegang tot alleen haar eigen map ('App folder'), met de rechten
files.content.write, files.content.read en files.metadata.read. Er wordt GEEN openbare deellink gemaakt: de back-up bevat
persoonsgegevens van kopers. Wie de map wil openen, moet daarvoor in zijn eigen Dropbox zijn ingelogd.
"""

import os
import re

import requests

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"
LIST_CONTINUE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"
DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"

BACKUP_FOLDER = "/back-ups"
MAX_SINGLE_UPLOAD_BYTES = 140 * 1024 * 1024   # Dropbox staat 150 MB toe in één aanvraag
KEEP_DAYS = 30                                 # zoveel dagelijkse back-ups blijven bewaard
BACKUP_NAME_RE = re.compile(r"^boekbeheer_back-up_(\d{4}-\d{2}-\d{2})\.zip$")
# Dropbox-link die om inloggen vraagt (geen openbare deellink).
DROPBOX_FOLDER_URL = "https://www.dropbox.com/home/Apps"


class DropboxError(Exception):
    pass


def is_configured():
    return all(os.environ.get(k) for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"))


def _check(resp, what):
    if not resp.ok:
        raise DropboxError(f"{what} mislukt: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp


def get_access_token():
    """Een kortlevende toegangssleutel, via de blijvende 'refresh token'."""
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"]},
        auth=(os.environ["DROPBOX_APP_KEY"], os.environ["DROPBOX_APP_SECRET"]),
        timeout=30,
    )
    _check(resp, "Inloggen bij Dropbox")
    token = resp.json().get("access_token")
    if not token:
        raise DropboxError("Inloggen bij Dropbox gaf geen toegangssleutel terug")
    return token


def upload_backup(token, filename, data):
    """Zet 'data' als back-upbestand in de back-upmap (overschrijft een bestand met dezelfde naam). Geeft het pad terug."""
    if len(data) > MAX_SINGLE_UPLOAD_BYTES:
        raise DropboxError(f"Back-up is te groot voor één upload ({len(data) / 1024 / 1024:.0f} MB)")
    path = f"{BACKUP_FOLDER}/{filename}"
    import json
    resp = requests.post(
        UPLOAD_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True}),
        },
        data=data,
        timeout=300,
    )
    _check(resp, "Uploaden naar Dropbox")
    meta = resp.json()
    if meta.get("size") is not None and meta["size"] != len(data):
        raise DropboxError(f"Dropbox bewaarde {meta['size']} bytes, verwacht {len(data)}: upload niet betrouwbaar")
    return meta.get("path_display") or path


def _list_backup_files(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(LIST_URL, headers=headers, json={"path": BACKUP_FOLDER}, timeout=30)
    _check(resp, "Map met back-ups opvragen")
    data = resp.json()
    entries = list(data.get("entries", []))
    while data.get("has_more"):
        resp = requests.post(LIST_CONTINUE_URL, headers=headers, json={"cursor": data["cursor"]}, timeout=30)
        _check(resp, "Map met back-ups opvragen")
        data = resp.json()
        entries += data.get("entries", [])
    return entries


def prune_old_backups(token, keep_days=KEEP_DAYS):
    """
    Verwijdert dagelijkse back-ups die ouder zijn dan 'keep_days' dagen — gerekend vanaf de NIEUWSTE back-up in de map.
    Alleen bestanden die precies de naam 'boekbeheer_back-up_JJJJ-MM-DD.zip' hebben worden aangeraakt, en de nieuwste
    wordt nooit verwijderd. Geeft het aantal verwijderde bestanden terug.
    """
    import datetime as dt
    dated = []
    for entry in _list_backup_files(token):
        m = BACKUP_NAME_RE.match(entry.get("name", ""))
        if m and entry.get(".tag", "file") == "file":
            dated.append((dt.date.fromisoformat(m.group(1)), entry))
    if not dated:
        return 0
    newest = max(d for d, _ in dated)
    cutoff = newest - dt.timedelta(days=keep_days)
    removed = 0
    for day, entry in dated:
        if day < cutoff and day != newest:
            resp = requests.post(
                DELETE_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"path": entry.get("path_lower") or f"{BACKUP_FOLDER}/{entry['name']}"},
                timeout=30,
            )
            _check(resp, "Oude back-up verwijderen")
            removed += 1
    return removed

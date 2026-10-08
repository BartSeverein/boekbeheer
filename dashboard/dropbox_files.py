"""
dropbox_files.py — Haalt bestanden uit de app-map in Dropbox, voor ingelogde gebruikers van het dashboard.

De bestanden staan NIET in GitHub (die repo is openbaar) maar in Dropbox, in de map 'handleiding' van de app-map
(Dropbox\\Apps\\Boekbeheer-backup\\handleiding). Het dashboard haalt ze op met dezelfde Dropbox-sleutels als de
back-up, en toont ze pas nadat de gebruiker is ingelogd. Er wordt geen deellink gemaakt.

Benodigde geheimen in Streamlit: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN.
"""

import json

import requests

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
HELP_FOLDER = "/handleiding"


class DropboxFileError(Exception):
    pass


def get_access_token(app_key, app_secret, refresh_token):
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(app_key, app_secret),
        timeout=30,
    )
    if not resp.ok:
        raise DropboxFileError(f"Inloggen bij Dropbox mislukt (HTTP {resp.status_code})")
    token = resp.json().get("access_token")
    if not token:
        raise DropboxFileError("Inloggen bij Dropbox gaf geen toegangssleutel terug")
    return token


def download_file(token, path):
    """De inhoud (bytes) van één bestand uit de app-map. Geeft DropboxFileError als het er niet staat."""
    resp = requests.post(
        DOWNLOAD_URL,
        headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": json.dumps({"path": path})},
        timeout=60,
    )
    if resp.status_code == 409:
        raise DropboxFileError(f"'{path}' staat niet in Dropbox")
    if not resp.ok:
        raise DropboxFileError(f"Ophalen uit Dropbox mislukt (HTTP {resp.status_code})")
    return resp.content


def fetch_help_file(filename, app_key, app_secret, refresh_token):
    """Haalt 'filename' uit de handleiding-map. Geeft (bytes, None) of (None, foutmelding in gewone taal)."""
    if not (app_key and app_secret and refresh_token):
        return None, "Dropbox is nog niet ingesteld in dit dashboard (DROPBOX_APP_KEY, DROPBOX_APP_SECRET en DROPBOX_REFRESH_TOKEN ontbreken in de Streamlit-instellingen)."
    try:
        token = get_access_token(app_key, app_secret, refresh_token)
        return download_file(token, f"{HELP_FOLDER}/{filename}"), None
    except DropboxFileError as e:
        return None, str(e)
    except requests.RequestException as e:
        return None, f"Dropbox is nu niet bereikbaar ({type(e).__name__})."

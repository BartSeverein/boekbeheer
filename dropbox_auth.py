"""
dropbox_auth.py — EENMALIG op je eigen computer draaien om de DROPBOX_REFRESH_TOKEN te krijgen.

Gebruik:  python dropbox_auth.py
Je voert de app key en app secret van je Dropbox-app in, opent de getoonde link, klikt op 'Toestaan',
en plakt de getoonde code terug. Het script toont daarna de refresh token; die zet je als GitHub-secret
(DROPBOX_REFRESH_TOKEN). Er wordt niets opgeslagen.
"""

import getpass

import requests

key = input("App key: ").strip()
secret = getpass.getpass("App secret (je ziet het niet terwijl je typt): ").strip()
print()
print("Open deze link in je browser (ingelogd bij Dropbox) en klik op 'Toestaan':")
print(f"https://www.dropbox.com/oauth2/authorize?client_id={key}&response_type=code&token_access_type=offline")
print()
code = input("Plak hier de code die Dropbox toont: ").strip()
resp = requests.post(
    "https://api.dropboxapi.com/oauth2/token",
    data={"code": code, "grant_type": "authorization_code"},
    auth=(key, secret),
    timeout=30,
)
if not resp.ok:
    raise SystemExit(f"Mislukt (HTTP {resp.status_code}): {resp.text}")
data = resp.json()
if "refresh_token" not in data:
    raise SystemExit(f"Dropbox gaf geen refresh token. Antwoord: {data}")
print()
print("Gelukt. Zet deze waarde als GitHub-secret DROPBOX_REFRESH_TOKEN:")
print(data["refresh_token"])

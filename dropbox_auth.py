"""
dropbox_auth.py — EENMALIG op je eigen computer draaien om de DROPBOX_REFRESH_TOKEN te krijgen.

Gebruik:  python dropbox_auth.py
Je voert de app key en app secret van je Dropbox-app in, opent de getoonde link, klikt op 'Toestaan',
en plakt de getoonde code terug. Het script toont daarna de refresh token; die zet je als GitHub-secret
(DROPBOX_REFRESH_TOKEN). Er wordt niets opgeslagen.
"""

import getpass
from urllib.parse import quote

import requests

# De rechten worden vastgelegd op het moment dat je toestemming geeft, in de refresh token zelf. Zet je later een recht aan
# in de Dropbox-app, dan moet je dit script opnieuw draaien voor een nieuwe token.
SCOPES = "files.content.write files.content.read files.metadata.read"

key = input("App key: ").strip()
secret = getpass.getpass("App secret (je ziet het niet terwijl je typt): ").strip()
print()
print("Open deze link in je browser (ingelogd bij Dropbox) en klik op 'Toestaan':")
print(
    f"https://www.dropbox.com/oauth2/authorize?client_id={key}&response_type=code"
    f"&token_access_type=offline&scope={quote(SCOPES)}"
)
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
granted = data.get("scope", "")
missing = [sc for sc in SCOPES.split() if sc not in granted.split()]
if missing:
    raise SystemExit(
        f"Let op: deze token mist de rechten {', '.join(missing)}. Zet ze aan onder 'Permissions' in je Dropbox-app, "
        f"klik op 'Submit' en draai dit script opnieuw. (Gekregen: {granted or 'onbekend'})"
    )
print()
print("Gelukt, met de juiste rechten. Zet deze waarde als GitHub-secret DROPBOX_REFRESH_TOKEN:")
print(data["refresh_token"])

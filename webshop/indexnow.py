"""
webshop/indexnow.py — meldt gewijzigde en verwijderde pagina's bij IndexNow (Bing, Yandex, Naver, Seznam, Yep; Google doet niet mee).

De sleutel is geen geheim: hij staat als <sleutel>.txt openbaar in de webroot (de build zet dat bestand neer), zodat de zoekmachines
kunnen nagaan dat de melding van deze site komt. Een melding die mislukt mag het publiceren nooit tegenhouden.
Uitzetten kan met WEBSHOP_INDEXNOW=nee; een eigen sleutel met WEBSHOP_INDEXNOW_KEY (8 tot 128 letters/cijfers/streepjes).
"""
import json
import urllib.request
from urllib.parse import urlparse

ENDPOINT = "https://api.indexnow.org/IndexNow"
BATCH = 10000   # maximum per aanvraag volgens het protocol


def page_url(rel, base_url):
    """'boek/x/index.html' -> 'https://…/boek/x/'; geeft None voor bestanden die geen pagina zijn."""
    if not rel.endswith("index.html"):
        return None   # 404.html, php, afbeeldingen, sitemap, feed enz. horen er niet bij
    path = "/" + rel[: -len("index.html")]
    return base_url.rstrip("/") + path


def notify(cfg, rels, log=print, post=None):
    """Meldt de pagina's uit 'rels' (relatieve paden van geüploade of verwijderde bestanden). Geeft het aantal gemelde adressen terug."""
    key = cfg.get("indexnow_key")
    if not cfg.get("indexnow") or not key or not cfg.get("base_url"):
        return 0
    base = cfg["base_url"].rstrip("/")
    urls = sorted({u for u in (page_url(r, base) for r in rels) if u})
    if not urls:
        return 0
    host = urlparse(base).netloc
    post = post or _post
    sent = 0
    for start in range(0, len(urls), BATCH):
        chunk = urls[start:start + BATCH]
        body = {"host": host, "key": key, "keyLocation": f"{base}/{key}.txt", "urlList": chunk}
        try:
            status = post(ENDPOINT, body)
        except Exception as exc:
            log(f"IndexNow: melding mislukt ({type(exc).__name__}: {exc}); de site zelf is wel bijgewerkt.")
            return sent
        if status in (200, 202):
            sent += len(chunk)
        else:
            log(f"IndexNow: de zoekmachine antwoordde met status {status}; de site zelf is wel bijgewerkt.")
            return sent
    log(f"IndexNow: {sent} adressen gemeld.")
    return sent


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code

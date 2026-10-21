# Webshop (severein.nl)

Statische site + 4 kleine PHP-scripts. Het boekenbestand komt uit dezelfde Postgres-database als de rest van de app.

## Hoe het werkt

```
Postgres (boeken, foto's, orders)
   │  python main.py webshop-publish <full|catalog> ja
   ▼
1. shop.sqlite van de server halen → betaalde bestellingen in `orders` zetten (platform 'Webshop'),
   voorraad -1 en doorgeven aan Boekwinkeltjes (zelfde aanpak als Bol)
2. site bouwen in webshop_dist/  (HTML, WebP-foto's, catalog.sqlite, PHP, sitemap, robots, .htaccess)
3. via SFTP alleen wat nieuw/gewijzigd is uploaden (manifest op de server), catalogus als allerlaatste
```

Op de server:

| Bestand | Rol |
|---|---|
| `index.html`, `boek/…`, `categorie/…`, `zoeken/`, `winkelwagen/`, `afrekenen/`, `bedankt/` | statische pagina's |
| `s.<hash>.css`, `c.<hash>.js` | enige CSS en JS (±3 KB / ±8 KB, geen libraries). Winkelwagen = `localStorage` |
| `search.php` | zoekt in `catalog.sqlite` (genormaliseerd: accenten/hoofdletters maken niet uit; ook op ISBN) |
| `checkout.php` | JSON-winkelwagen in → prijzen, voorraad en verzendkosten opnieuw controleren → bestelling + reservering (30 min) → Mollie-betaling → `{ok,url}` |
| `webhook.php` | Mollie meldt een statuswijziging; we vragen de status zelf bij Mollie op (geen vertrouwen in de POST) |
| `status.php` | de bedankpagina vraagt hier de status op (en vraagt zo nodig zelf Mollie als de webhook niet aankomt) |
| `d-<WEBSHOP_DATA_KEY>/catalog.sqlite` | boekenlijst (door de build geüpload, PHP leest alleen) |
| `d-<WEBSHOP_DATA_KEY>/shop.sqlite` | bestellingen (door PHP geschreven, door de pipeline gelezen) |
| `config.php` | instellingen + Mollie-sleutel (staat in de webroot; wordt niet uitgeserveerd, zie `.htaccess`) |
| `.shop-manifest.json` | welke bestanden wij hebben geplaatst en hun hash |

### Niet te veel verkopen
Elk boek is meestal 1 exemplaar, en het ligt ook op Boekwinkeltjes en (soms) Bol. Daarom:
- `checkout.php` telt van de voorraad in de catalogus af: betaalde bestellingen die nog niet zijn verwerkt + open bestellingen jonger dan 30 minuten.
- De pipeline verwerkt betaalde bestellingen in de voorraad en zet de nieuwe voorraad bij Boekwinkeltjes. De catalogus bevat een lijst met al verwerkte bestellingen, zodat er niet dubbel wordt afgeteld.
- Het venster waarin een boek tegelijk bij Boekwinkeltjes/Bol en in de shop verkocht kan worden is de tijd tot de volgende `catalog`-run. Draai die dus vaak (elke 15 minuten).
- Gebeurt het toch: de bestelling krijgt `LET OP` in de notitie, je krijgt een mail, en je betaalt de klant terug via Mollie.

## Eenmalig instellen

1. **GitHub-secrets** (Settings → Secrets → Actions). Verplicht: `WEBSHOP_SFTP_HOST`, `WEBSHOP_SFTP_USER`, `WEBSHOP_SFTP_PASS`, `WEBSHOP_DATA_KEY` (zelf verzonnen, 16+ letters/cijfers, geen andere tekens), `WEBSHOP_MOLLIE_API_KEY`, `WEBSHOP_CONTACT_EMAIL`.
   Optioneel: `WEBSHOP_SFTP_PORT` (22), `WEBSHOP_SFTP_ROOT` (leeg = de map waar je SFTP-login in terechtkomt), `WEBSHOP_BASE_URL` (https://severein.nl), `WEBSHOP_NAME` (Severein), `WEBSHOP_TAGLINE`, `WEBSHOP_OWNER_NAME`, `WEBSHOP_ADDRESS`, `WEBSHOP_KVK`, `WEBSHOP_VAT`, `WEBSHOP_IBAN`, `WEBSHOP_SMTP_HOST`/`_PORT` (587)/`_USER`/`_PASS`/`_FROM` (bevestigingsmails via SMTP; zonder deze instellingen gebruikt de shop PHP `mail()`, dat niet bij elke hoster werkt), `WEBSHOP_PHONE` (komen in de voorwaarden), `WEBSHOP_DEFAULT_SHIPPING_CENTS` (395), `WEBSHOP_EXTRA_PER_BOOK_CENTS` (0), `WEBSHOP_FREE_SHIPPING_FROM_CENTS` (0 = nooit gratis), `WEBSHOP_COUNTRIES` (NL; bijvoorbeeld `NL,BE,DE,FR`), `WEBSHOP_SHIPPING_RATES` (tarief per land voor een klein/groot pakje, bijvoorbeeld `NL=3.95/6.95,DE=7.50/12.00,FR=8.00/13.00,OTHER=10.00/16.00`; `OTHER` geldt voor elk ander land in `WEBSHOP_COUNTRIES`, een land zonder tarief en zonder `OTHER` wordt niet aangeboden), `WEBSHOP_SMALL_MAX_CENTS` (400: een boek met een eigen verzendprijs tot dit bedrag is een klein pakje, daarboven groot), `WEBSHOP_COPYRIGHT_NAME` (Severein Services; verschijnt onderaan als © 2001-jaar), `WEBSHOP_COUNTRY_SURCHARGE` (oude instelling: toeslag op het NL-tarief; wordt alleen gebruikt voor landen zonder eigen tarief), `WEBSHOP_LANGS` (`nl,en,de,fr`; zet op `nl` voor alleen Nederlands), `WEBSHOP_DEEPL_KEY`, `WEBSHOP_DEEPL_MAX_CHARS`, `WEBSHOP_DEEPL_TOTAL_CHARS` en `WEBSHOP_TRANSLATE_BOOKS` en `WEBSHOP_TRANSLATE_SHORT` (zie *Talen*).
2. **Mollie**: account afmaken, daarna begin je met de **test-API-sleutel** (`test_…`) in `WEBSHOP_MOLLIE_API_KEY`. Betalingen testen kan dan zonder echt geld. Voor live vervang je hem door de `live_…`-sleutel. Mollie moet `https://<jouw domein>/webhook.php` kunnen bereiken (dus geen wachtwoordbeveiliging op de site zetten tijdens het testen met echte webhooks; de bedankpagina werkt ook zonder webhook omdat `status.php` zelf Mollie vraagt).
3. **Proefrun**: Actions → *Webshop bouwen en uploaden* → modus `full`, echt_uitvoeren leeg of `nee`. Dat bouwt alles (zonder foto's te verwerken) en laat zien wat er geüpload zou worden.
4. **Eerste echte run**: modus `full`, echt_uitvoeren `ja`. Bij ~20.000 boeken duurt dit uren; de run stopt na `max_minuten` en de volgende run gaat verder (het manifest onthoudt wat er al staat). Start dezelfde run gewoon opnieuw tot hij zonder "NIET compleet" eindigt. Foto's worden in dezelfde stappen verwerkt.
5. **Controleer** in je browser: een boekpagina, zoeken, winkelwagen, een testbetaling (iDEAL-testscherm van Mollie).
6. **cron-job.org** (zoals bij de andere workflows): POST naar `https://api.github.com/repos/BartSeverein/boekbeheer/actions/workflows/webshop-publish.yml/dispatches` met body `{"ref":"main","inputs":{"modus":"catalog","echt_uitvoeren":"ja","trigger_source":"cron-job.org"}}` elke 15 minuten, en een tweede job met `"modus":"full"` eens per nacht.

## Aannames die je moet controleren
- **Hosting**: Apache 2.4 met `.htaccess`, PHP 7.4+ met `pdo_sqlite` en `curl`, en `mail()` (anders komen er geen bevestigingsmails; de bestelling zelf werkt wel). Draait de host op nginx, dan werkt de beveiliging van `config.php` en de `d-…`-map niet via `.htaccess`: laat dan een regel toevoegen die ze blokkeert.
- **SFTP**: de gebruiker mag in de webroot **lezen én schrijven** (we lezen `shop.sqlite` en het manifest terug) en kan bestanden hernoemen.
- **Verzendkosten**: per land een tarief voor een klein en een groot pakje (`WEBSHOP_SHIPPING_RATES`). Of een boek groot is, volgt uit zijn eigen verzendprijs in de database (boven `WEBSHOP_SMALL_MAX_CENTS` = groot). Zit er één groot boek in de wagen, dan geldt het grote tarief; daarbovenop komt de optionele toeslag per extra boek. Gratis verzenden vanaf een bedrag geldt alleen voor Nederland. Zonder `WEBSHOP_SHIPPING_RATES` is Nederland € 3,95 / € 6,95.
- **Wettelijke teksten** (`webshop/content/voorwaarden.html` en `privacy.html`) zijn een concept: laat ze nalopen. Er staat geen btw-vermelding in de prijzen; pas dat aan als dat voor jou van toepassing is en vul KvK/btw-gegevens in.
- **Verkochte boeken**: hun pagina verdwijnt bij de eerstvolgende `full`-run (tot dan staat er nog een pagina, maar bestellen kan niet meer: de catalogus ontbreekt het boek).
- Terugbetalingen/chargebacks in Mollie worden niet automatisch verwerkt.
- Het dashboard is nog niet aangepast voor `Webshop`-orders: ze staan wel in de orders-tabel (status 'Wachten op verzending'), maar de tellingen op Home houden er nog geen rekening mee.

## Lokaal proberen
```
export WEBSHOP_DATA_KEY=abcdefghijkl1234 WEBSHOP_BASE_URL=http://127.0.0.1:8088
python -c "from webshop.build import main_build; print(main_build('webshop_dist'))"   # vereist SUPABASE_DB_URL
cd webshop_dist && php -S 127.0.0.1:8088
```


## Talen (NL, EN, DE, FR)

- Nederlands staat op de hoofdadres (`/boek/...`), de rest onder `/en/`, `/de/` en `/fr/` met vertaalde padnamen (`/en/book/...`, `/de/buch/...`, `/fr/livre/...`). Rechtsboven staat de taalkeuze; elke pagina verwijst met `hreflang` naar zijn varianten (goed voor Google). Het winkelmandje wordt tussen talen gedeeld.
- Alle vaste teksten staan in `webshop/i18n.py` (één plek voor de site, het winkelmandje-script en de PHP-meldingen en bevestigingsmail). De bevestigingsmail gaat in de taal van de klant; jij krijgt de bestelling altijd in het Nederlands, met de taal van de klant erbij. Mollie toont de betaalpagina in dezelfde taal.
- Voorwaarden en privacy staan in `webshop/content/` per taal (`voorwaarden.en.html`, enz.). De Engelse, Duitse en Franse versies zijn vertalingen van het Nederlandse concept en moeten net zo goed worden nagelopen.
- **DeepL** vertaalt standaard alleen de *categorienamen* (een paar honderd tekens per taal). Zet daarvoor een secret `WEBSHOP_DEEPL_KEY` (een gratis API-sleutel eindigt op `:fx`). Titels, auteurs en uitgevers worden nooit vertaald. De **boekbeschrijvingen** worden pas vertaald als je het secret `WEBSHOP_TRANSLATE_BOOKS` op `ja` zet; tot dan staan ze op de andere talen in het Nederlands (gemarkeerd met `lang="nl"`).
  - Elke vertaling wordt bewaard in Postgres (tabel `shop_translations`, wordt vanzelf aangemaakt). Een tekst wordt dus één keer vertaald; pas bij een gewijzigde tekst opnieuw.
  - Twee grenzen beschermen je tegoed: `WEBSHOP_DEEPL_MAX_CHARS` per run (standaard 100.000) en `WEBSHOP_DEEPL_TOTAL_CHARS` over alle runs samen (standaard 900.000; alles wat ooit via dit systeem vertaald is, opgeteld). Staat je DeepL-tegoed op 1.000.000 tekens, dan raakt het dus nooit op. Gebruik je DeepL ook voor andere dingen, verlaag het totaal dan met dat verbruik.
  - Bij de boekbeschrijvingen gaan de nieuwste boeken eerst; wat na een run nog niet vertaald is, staat in het Nederlands tot de volgende run.
  - In een proefrun gaat er niets naar DeepL; het log meldt wel hoeveel tekens er nog te vertalen zijn, zodat je vooraf weet wat het kost.
  - Raakt het DeepL-tegoed op of weigert DeepL de sleutel, dan stopt het vertalen voor die run en wordt de site gewoon gebouwd met de Nederlandse tekst.
- Zonder `WEBSHOP_DEEPL_KEY` zijn menu's, knoppen en voorwaarden wel vertaald, maar blijven ook de categorienamen Nederlands.


**Bijzonderheden vertalen:** de korte tekst per boek ("Bijzonderheden": staat, stofomslag, enz.) wordt standaard via DeepL vertaald (`WEBSHOP_TRANSLATE_SHORT=nee` zet dat uit). De lange beschrijving blijft Nederlands tot je `WEBSHOP_TRANSLATE_BOOKS=ja` zet; zolang staat er "Over dit boek (in het Nederlands)" in de andere talen. Wat het tegoed niet haalt, blijft Nederlands tot een volgende run.

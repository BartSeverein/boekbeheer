# Webshop (boeken.severein.nl)

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
   Optioneel: `WEBSHOP_SFTP_PORT` (22), `WEBSHOP_SFTP_ROOT` (leeg = de map waar je SFTP-login in terechtkomt), `WEBSHOP_BASE_URL` (https://severein.nl), `WEBSHOP_NAME` (Severein), `WEBSHOP_TAGLINE`, `WEBSHOP_OWNER_NAME`, `WEBSHOP_ADDRESS`, `WEBSHOP_KVK`, `WEBSHOP_VAT`, `WEBSHOP_PHONE` (komen in de voorwaarden), `WEBSHOP_DEFAULT_SHIPPING_CENTS` (395), `WEBSHOP_EXTRA_PER_BOOK_CENTS` (0), `WEBSHOP_FREE_SHIPPING_FROM_CENTS` (0 = nooit gratis), `WEBSHOP_COUNTRIES` (NL; bijvoorbeeld `NL,BE`).
2. **Mollie**: account afmaken, daarna begin je met de **test-API-sleutel** (`test_…`) in `WEBSHOP_MOLLIE_API_KEY`. Betalingen testen kan dan zonder echt geld. Voor live vervang je hem door de `live_…`-sleutel. Mollie moet `https://<jouw domein>/webhook.php` kunnen bereiken (dus geen wachtwoordbeveiliging op de site zetten tijdens het testen met echte webhooks; de bedankpagina werkt ook zonder webhook omdat `status.php` zelf Mollie vraagt).
3. **Proefrun**: Actions → *Webshop bouwen en uploaden* → modus `full`, echt_uitvoeren leeg of `nee`. Dat bouwt alles (zonder foto's te verwerken) en laat zien wat er geüpload zou worden.
4. **Eerste echte run**: modus `full`, echt_uitvoeren `ja`. Bij ~20.000 boeken duurt dit uren; de run stopt na `max_minuten` en de volgende run gaat verder (het manifest onthoudt wat er al staat). Start dezelfde run gewoon opnieuw tot hij zonder "NIET compleet" eindigt. Foto's worden in dezelfde stappen verwerkt.
5. **Controleer** in je browser: een boekpagina, zoeken, winkelwagen, een testbetaling (iDEAL-testscherm van Mollie).
6. **cron-job.org** (zoals bij de andere workflows): POST naar `https://api.github.com/repos/BartSeverein/boekbeheer/actions/workflows/webshop-publish.yml/dispatches` met body `{"ref":"main","inputs":{"modus":"catalog","echt_uitvoeren":"ja","trigger_source":"cron-job.org"}}` elke 15 minuten, en een tweede job met `"modus":"full"` eens per nacht.

## Aannames die je moet controleren
- **Hosting**: Apache 2.4 met `.htaccess`, PHP 7.4+ met `pdo_sqlite` en `curl`, en `mail()` (anders komen er geen bevestigingsmails; de bestelling zelf werkt wel). Draait de host op nginx, dan werkt de beveiliging van `config.php` en de `d-…`-map niet via `.htaccess`: laat dan een regel toevoegen die ze blokkeert.
- **SFTP**: de gebruiker mag in de webroot **lezen én schrijven** (we lezen `shop.sqlite` en het manifest terug) en kan bestanden hernoemen.
- **Verzendkosten**: een bestelling kost de *hoogste* verzendprijs van de boeken in de wagen (+ optionele toeslag per extra boek). Alleen Nederland tenzij je `WEBSHOP_COUNTRIES` aanpast. Boeken zonder verzendprijs krijgen € 3,95.
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

# Boekwinkeltjes.nl sync + online dashboard

Synchroniseert je boeken en orders van Boekwinkeltjes.nl met een Supabase (Postgres)
database, automatisch in de cloud via GitHub Actions, met een Streamlit-dashboard
erbovenop.

## 1. Supabase-project aanmaken

1. Ga naar [supabase.com](https://supabase.com), maak een gratis account en start een nieuw project.
2. Ga naar **Project Settings -> Database -> Connection string -> URI** en kopieer die.
   Vul 'm in bij `SUPABASE_DB_URL` in `.env`.
3. Ga naar **SQL Editor -> New query**, plak de inhoud van `schema_postgres.sql` en klik **Run**.
   Dit maakt de tabellen `books`, `orders` en `sync_log` aan.
4. Onder **Table Editor** in Supabase kun je nu al direct je data spreadsheet-gewijs
   bekijken en bewerken — dat werkt meteen, los van het dashboard hieronder.

## 2. Lokaal testen

```bash
pip install -r requirements.txt
python main.py sync
```

Dit haalt je boeken en orders op van Boekwinkeltjes.nl en schrijft ze naar Supabase.
Test dit met `.env` nog op de **zandbak**-omgeving voordat je naar live overschakelt.

## 3. Automatiseren met GitHub Actions

1. Zet dit project in een **privé** GitHub-repository (bevat gevoelige info als je
   `.env` niet goed negeert — zorg dat `.env` in `.gitignore` staat en NOOIT wordt gecommit).
2. Ga naar **Settings -> Secrets and variables -> Actions** in je repository en voeg toe:
   - `BOEKWINKELTJES_API_KEY`
   - `BOEKWINKELTJES_BASE_URL` (de live-URL, zodra je zover bent)
   - `SUPABASE_DB_URL`
3. De workflow in `.github/workflows/sync.yml` draait vanaf dat moment automatisch
   elke 15 minuten `python main.py sync` — ook als je eigen computer uit staat.
   Je kunt 'm ook handmatig starten via het "Actions"-tabblad ("Run workflow").

## 4. Dashboard

Lokaal proberen:
```bash
cd dashboard
pip install -r requirements.txt
export SUPABASE_DB_URL="postgresql://...."   # of gebruik .streamlit/secrets.toml
streamlit run app.py
```

Online hosten (gratis):
1. Ga naar [share.streamlit.io](https://share.streamlit.io) (Streamlit Community Cloud)
   en verbind dezelfde GitHub-repository.
2. Kies `dashboard/app.py` als hoofdbestand.
3. Zet onder **Secrets** in de Streamlit-instellingen:
   ```
   SUPABASE_DB_URL = "postgresql://...."
   ```
4. Klaar — je krijgt een eigen URL met live omzet-grafieken, orderstatus-overzicht
   en voorraad per categorie.

## Bestanden

- `schema_postgres.sql` — databaseschema voor Supabase
- `db.py` — Postgres-connectie + schema-init
- `api_client.py` — wrapper rond de Boekwinkeltjes.nl API
- `sync.py` — pull/push synchronisatielogica
- `main.py` — command-line entry point (`init`, `sync`, `pull-books`, `pull-orders`, `push-books`)
- `add_test_book.py` — maakt een testboek aan om de sync mee te testen
- `.github/workflows/sync.yml` — automatische cloud-sync elke 15 minuten
- `dashboard/app.py` — Streamlit-dashboard

## Nog te doen / uit te breiden

- Push-logica voor **nieuwe** boeken (nu alleen updates van bestaande boeken)
- Order-statussen automatisch bijwerken (bijv. na verzending)
- Afbeeldingen synchroniseren (`/books/{id}/images`)
- Verzendlabels/tracking via `/packages` koppelen aan orders

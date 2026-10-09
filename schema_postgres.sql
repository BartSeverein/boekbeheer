-- schema_postgres.sql
-- Plak dit in de Supabase SQL Editor (Project -> SQL Editor -> New query) en run het.

CREATE TABLE IF NOT EXISTS books (
    id                  BIGINT PRIMARY KEY,   -- boekwinkeltjes book id
    book_number         BIGINT,
    location            TEXT,
    amount              INTEGER,
    category1           TEXT,
    category2           TEXT,
    category3           TEXT,
    language            TEXT,
    author              TEXT,
    title               TEXT,
    publisher           TEXT,   -- ruwe waarde zoals Boekwinkeltjes 'm geeft (kan "Naam ; Adres ; Contact" zijn)
    publisher_name      TEXT,
    publisher_address   TEXT,
    publisher_contact   TEXT,
    ean                 TEXT,
    short_description   TEXT,
    long_description    TEXT,
    price               NUMERIC,
    shipping_cost       NUMERIC,
    shipping_cost_bol   NUMERIC,   -- eigen verzendkosten-veld voor een latere Bol.com-synchronisatie
    shipping_category   INTEGER,
    listing_date        TEXT,
    weblink             TEXT,
    main_image_url      TEXT,   -- via og:image van de publieke boekpagina gehaald (aparte achtergrondtaak)
    length_cm           NUMERIC,  -- afmetingen (Google Books/ISBNdb/Bol, bij invoer bepaald): hoogste waarde = lengte
    width_cm            NUMERIC,  -- middelste waarde
    thickness_cm        NUMERIC,  -- laagste waarde (altijd de dunste maat van een boek)
    last_synced_at      TIMESTAMPTZ,
    local_updated_at    TIMESTAMPTZ,
    pending_push        BOOLEAN DEFAULT FALSE,
    pending_create      BOOLEAN DEFAULT FALSE,  -- TRUE = nog aan te maken bij Boekwinkeltjes.nl (nieuw boek vanuit dashboard)
    push_enabled        BOOLEAN DEFAULT TRUE,   -- FALSE = wijzigingen aan dit boek worden NOOIT naar Boekwinkeltjes.nl gestuurd
    queued              BOOLEAN DEFAULT FALSE,  -- TRUE = wacht op handmatige controle (bijv. na bulk-import); nog niet zichtbaar/gesynchroniseerd
    created_via_app     BOOLEAN DEFAULT FALSE   -- TRUE = aangemaakt via Nieuw boek/bulk-import (dus NIET via de gewone Boekwinkeltjes-pull) — bepaalt of het boek in aanmerking komt voor de Bol-push
);

CREATE TABLE IF NOT EXISTS orders (
    id                      BIGINT PRIMARY KEY,   -- boekwinkeltjes order id, of een negatief afgeleid id voor Bol-orders
    order_date              TEXT,
    status                  TEXT,
    online_payment_status   TEXT,
    platform                TEXT DEFAULT 'BW',   -- 'BW' (Boekwinkeltjes) of 'Bol'
    stock_adjusted          BOOLEAN DEFAULT FALSE, -- TRUE zodra deze (Bol-)order al is verwerkt in de voorraad, zodat 'm nooit twee keer wordt afgeteld
    book_id                 BIGINT,   -- verwijst naar books.id, maar GEEN foreign key:
                                       -- boeken die verkocht/verwijderd zijn staan mogelijk
                                       -- niet meer in de books-tabel, terwijl oude orders
                                       -- er nog wel naar verwijzen. De snapshot-velden
                                       -- hieronder (book_title, book_price, etc.) zorgen
                                       -- dat je toch alles kunt rapporteren.
    -- snapshot van het boek op het moment van de order (blijft kloppen ook als
    -- de huidige prijs/titel van het boek later verandert of het boek verwijderd wordt)
    book_title              TEXT,
    book_author             TEXT,
    book_price              NUMERIC,
    book_shipping_cost      NUMERIC,
    book_ean                TEXT,
    buyer_first_name        TEXT,
    buyer_last_name         TEXT,
    buyer_name              TEXT,
    buyer_phone             TEXT,
    buyer_email             TEXT,
    buyer_language          TEXT,
    buyer_street            TEXT,
    buyer_number            TEXT,
    buyer_number_extra      TEXT,
    buyer_zip_code          TEXT,
    buyer_city              TEXT,
    buyer_country           TEXT,
    buyer_company           TEXT,
    note                    TEXT,
    last_synced_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ,
    direction   TEXT,   -- 'pull' of 'push'
    resource    TEXT,   -- 'books', 'orders', 'images' of 'main_images'
    status      TEXT,   -- 'ok' of 'error'
    detail      TEXT,
    trigger     TEXT,   -- 'Gepland' of 'Handmatig'
    platform    TEXT DEFAULT 'BW'  -- 'BW' (Boekwinkeltjes) of 'Bol'
);

CREATE INDEX IF NOT EXISTS idx_orders_book_id ON orders (book_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_order_date ON orders (order_date);

CREATE TABLE IF NOT EXISTS book_images (
    book_id         BIGINT NOT NULL,
    image_id        BIGINT NOT NULL,   -- -1 = "gecontroleerd, geen afbeeldingen gevonden" (sentinel)
    position        INTEGER NOT NULL,  -- volgorde zoals de API teruggeeft; 0 = aanname hoofdafbeelding
    url_large       TEXT,
    url_medium      TEXT,
    url_small       TEXT,
    image_large_data    BYTEA,  -- lokaal gedownloade large-variant (voor door onszelf geüploade afbeeldingen)
    image_medium_data   BYTEA,  -- lokaal gedownloade medium-variant
    last_synced_at  TIMESTAMPTZ,
    PRIMARY KEY (book_id, image_id)
);

CREATE INDEX IF NOT EXISTS idx_book_images_book_id ON book_images (book_id);

CREATE TABLE IF NOT EXISTS book_uploaded_images (
    id                          BIGSERIAL PRIMARY KEY,
    book_id                     BIGINT NOT NULL,
    image_data                  BYTEA NOT NULL,
    content_type                TEXT NOT NULL,
    is_main                     BOOLEAN DEFAULT FALSE,
    position                    INTEGER DEFAULT 0,
    pushed_to_boekwinkeltjes    BOOLEAN DEFAULT FALSE,
    created_at                  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_book_uploaded_images_book_id ON book_uploaded_images (book_id);

CREATE TABLE IF NOT EXISTS bol_offer_mapping (
    ean             TEXT PRIMARY KEY,
    offer_id        TEXT NOT NULL,
    bol_stock       INTEGER,           -- laatst bij Bol bekende voorraad (van de laatste export/lezing)
    last_synced_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bol_offer_mapping_offer_id ON bol_offer_mapping (offer_id);

CREATE TABLE IF NOT EXISTS isbndb_usage_log (
    id              BIGSERIAL PRIMARY KEY,
    checked_at      TIMESTAMPTZ DEFAULT now(),
    daily_remaining INTEGER
);

CREATE TABLE IF NOT EXISTS category_subject_mapping (
    id                          BIGSERIAL PRIMARY KEY,
    boekwinkeltjes_category1    TEXT NOT NULL,
    boekwinkeltjes_category2    TEXT NOT NULL DEFAULT '',  -- leeg = geldt voor categorie1 zelf (geen subcategorie)
    subjects                    TEXT,                       -- ISBNdb-subjects, gescheiden door '|'
    UNIQUE (boekwinkeltjes_category1, boekwinkeltjes_category2)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS known_publishers (
    value TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS app_users (
    id              BIGSERIAL PRIMARY KEY,
    full_name       TEXT NOT NULL,
    short_name      TEXT UNIQUE NOT NULL,   -- ook de gebruikersnaam om in te loggen
    email           TEXT,
    password_hash   TEXT NOT NULL,
    password_salt   TEXT NOT NULL,
    password_plain  TEXT,
    role            TEXT DEFAULT 'gebruiker',   -- 'beheerder' of 'gebruiker'
    last_location   TEXT,                        -- laatst gebruikte waarde in het locatieveld door deze gebruiker   -- leesbaar bewaard zodat het opgezocht kan worden (verlaagt de veiligheid bewust)
    is_active       BOOLEAN DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,   -- bijgewerkt bij elke paginalading; voor 'wie is er nu ook actief'
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS book_activity_log (
    id              BIGSERIAL PRIMARY KEY,
    book_id         BIGINT NOT NULL,
    user_short_name TEXT NOT NULL,
    action          TEXT NOT NULL,   -- 'created' of 'edited'
    changed_fields  TEXT,            -- bij 'edited': welke velden zijn aangepast, gescheiden door '|'
    occurred_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_book_activity_book_id ON book_activity_log (book_id);
CREATE INDEX IF NOT EXISTS idx_book_activity_user ON book_activity_log (user_short_name);

-- Zelflerende koppeling tussen het ISBN-uitgeverscijferblok (de 2 tot 7 cijfers na
-- 97890/97894, variabele lengte per uitgever) en een uitgeversnaam. Wordt gevuld
-- elke keer dat een uitgever via de normale weg (Boekwinkeltjes/ISBNdb/enz.) wél
-- gevonden wordt; gebruikt als laatste redmiddel wanneer dat een keer niet lukt.
-- Eén (prefix, uitgever)-paar per rij, met een teller hoe vaak die combinatie is
-- gezien — pas bij genoeg herhaling (zie MIN_OBSERVATIONS in de code) vertrouwd.
CREATE TABLE IF NOT EXISTS isbn_prefix_observations (
    prefix          TEXT NOT NULL,
    publisher       TEXT NOT NULL,
    times_seen      INTEGER DEFAULT 1,
    last_seen_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (prefix, publisher)
);
CREATE INDEX IF NOT EXISTS idx_isbn_prefix_observations_prefix ON isbn_prefix_observations (prefix);

-- Verzendformaat (Boekwinkeltjes-veld 'shippingFormat', sinds oktober 2026 verplicht om een boek te kunnen
-- verkopen). Waarden zoals in Boekwinkeltjes' eigen keuzelijst: 0 = alleen afhalen, 1 = brievenbuspakje,
-- 2 = klein pakket, 3 = normaal pakket, 4 = groot of zwaar pakket. NULL = nog niet ingesteld.
ALTER TABLE books ADD COLUMN IF NOT EXISTS shipping_format INTEGER;

-- Conditie van het Bol-aanbod (voor de taartgrafiek op Home): categorie 'NEW' of 'SECONDHAND', en bij tweedehands de
-- staat 'AS_NEW', 'GOOD', 'REASONABLE' of 'MODERATE'. Wordt door de voorraadsync met Bol bijgehouden.
ALTER TABLE bol_offer_mapping ADD COLUMN IF NOT EXISTS condition_category TEXT;
ALTER TABLE bol_offer_mapping ADD COLUMN IF NOT EXISTS condition_state TEXT;

-- Wekelijkse meting van het aanbod (voor de twee staafgrafieken op Home). Eén rij per meetdag; de taak
-- 'Wekelijkse voorraadmeting' vult hem in de nacht van zondag op maandag.
CREATE TABLE IF NOT EXISTS weekly_stock_snapshots (
    measured_on   DATE PRIMARY KEY,
    taken_at      TIMESTAMPTZ DEFAULT now(),
    bw_titles     INTEGER NOT NULL,   -- titels actief in de verkoop (Boekwinkeltjes): voorraad > 0, niet in een wachtrij
    bol_titles    INTEGER NOT NULL,   -- daarvan ook actief op Bol (bol_stock > 0)
    bw_units      INTEGER NOT NULL,   -- exemplaren in voorraad van die titels
    bol_units     INTEGER NOT NULL,   -- exemplaren die op Bol worden aangeboden (nooit meer dan de eigen voorraad)
    bw_value      NUMERIC,            -- verkoopwaarde: som van Prijs Boekwinkeltjes x voorraad van die titels (in euro)
    bol_value     NUMERIC             -- daarvan het deel dat op Bol staat (zelfde prijs x exemplaren op Bol)
);
ALTER TABLE weekly_stock_snapshots ADD COLUMN IF NOT EXISTS bw_value NUMERIC;
ALTER TABLE weekly_stock_snapshots ADD COLUMN IF NOT EXISTS bol_value NUMERIC;

-- Row Level Security (RLS) op elke tabel. Dit blokkeert alleen Supabase's eigen,
-- in dit project ongebruikte publieke webAPI (PostgREST) — de app zelf praat via
-- een directe databaseverbinding (SUPABASE_DB_URL) en die omzeilt RLS altijd,
-- dus dit heeft geen enkel effect op de werking van de app. Veilig om herhaaldelijk
-- te draaien. Voeg hier een regel aan toe zodra er een nieuwe tabel bijkomt.
ALTER TABLE books ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE book_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE book_uploaded_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE bol_offer_mapping ENABLE ROW LEVEL SECURITY;
ALTER TABLE isbndb_usage_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE category_subject_mapping ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE known_publishers ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE book_activity_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE isbn_prefix_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_stock_snapshots ENABLE ROW LEVEL SECURITY;

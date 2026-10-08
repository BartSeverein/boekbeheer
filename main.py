"""
main.py — Command-line entry point.

Gebruik:
    python main.py init          # database aanmaken
    python main.py sync          # volledige sync (pull boeken, pull orders, push wijzigingen)
    python main.py pull-books
    python main.py pull-orders
    python main.py push-books
    python main.py pull-images [aantal]   # afbeeldingen ophalen voor een batch boeken (standaard 200)
    python main.py pull-main-images [aantal]   # hoofdfoto (og:image) ophalen voor een batch boeken (standaard 200)
    python main.py push-uploaded-images   # eigen geüploade afbeeldingen naar Boekwinkeltjes pushen
    python main.py pull-uploaded-images   # checken of gepushte afbeeldingen zijn verwerkt, en zo ja lokaal bevestigen
    python main.py quick-stock-sync       # lichte, snelle synchronisatie van alleen de voorraad (Boekwinkeltjes + Bol)
    python main.py test-bol-economic-operator   # veilige test: zoekt alleen de marktdeelnemer-ID op, verandert niets
    python main.py watchdog [test]       # waakhond: mailt als de synchronisatie te lang stilstaat; met 'test' een proefbericht
    python main.py reclaim-space [rapport]  # geeft lege ruimte terug aan de database; verwijdert geen gegevens (met 'rapport' alleen een rapport)
    python main.py check-storage [test]  # opslagcontrole: mailt bij 90/95/98/100% van de limiet; met 'test' een proefbericht
    python main.py photo-vacuum [real]   # fotostofzuiger: proefrun, of met 'real' echt afbeeldingsbestanden opruimen
    python main.py test-bol-offers-v11 [write-noop]   # veilige controle van Bol's aanbiedingen-API v11 (alleen lezen)
    python main.py push-new-books-to-bol  # ALLEEN nieuwe boeken (via de app aangemaakt) naar Bol pushen, verder niets
    python main.py drip-push-new-books    # druppelsgewijs (max 1 per interval, alleen in drukke perioden) nieuwe boeken naar Boekwinkeltjes
    python main.py backfill-shipping-format [real] [briefpost pakketpost]   # eenmalig: verzendformaat bij Boekwinkeltjes vullen (zonder 'real' een proefrun)
    python main.py update-legacy-shipping-cost [real] [briefpost]   # eenmalig: oude briefpost-verzendkosten (3,75/1,40) naar de huidige briefpost (zonder 'real' een proefrun)
    python main.py compare-main-images [aantal]   # alleen lezen: vergelijkt eerdere en huidige foto's per boek, schrijft hoofdafbeelding_controle.csv
    python main.py send-daily-csv-export  # stuurt een e-mail met boeken.csv en orders.csv als back-up
"""

import re
import sys

from db import init_db
import bol_client
import sync


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1]

    if command == "init":
        init_db()
        print("Database geïnitialiseerd.")
    elif command == "sync":
        result = sync.full_sync()
        print(f"Sync klaar: {result}")
    elif command == "pull-books":
        n = sync.pull_books()
        print(f"{n} boeken opgehaald.")
    elif command == "pull-orders":
        n = sync.pull_orders()
        print(f"{n} orders opgehaald.")
    elif command == "push-books":
        n = sync.push_pending_books()
        print(f"{n} boeken gepusht.")
    elif command == "push-new-books":
        n = sync.push_new_books()
        print(f"{n} nieuwe boeken aangemaakt.")
    elif command == "pull-images":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        result = sync.pull_images(limit=limit)
        print(f"Afbeeldingen-batch klaar: {result}")
    elif command == "pull-main-images":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        result = sync.pull_main_images(limit=limit)
        print(f"Hoofdfoto-batch klaar: {result}")
    elif command == "push-uploaded-images":
        n = sync.push_uploaded_images()
        print(f"{n} geüploade afbeeldingen gepusht.")
    elif command == "pull-uploaded-images":
        n = sync.pull_uploaded_book_images()
        print(f"{n} boek(en) met afbeeldingen bevestigd.")
    elif command == "quick-stock-sync":
        sync.quick_stock_sync()
        print("Snelle voorraadsync klaar.")
    elif command == "test-bol-economic-operator":
        # Veilige test: doet ALLEEN de opzoeking, maakt of wijzigt niets bij Bol.
        try:
            operator_id = bol_client.get_economic_operator_id_from_offers()
            if operator_id:
                print(f"Gevonden via je bestaande aanbiedingen: {operator_id}")
            else:
                print("Niet gevonden via je bestaande aanbiedingen — probeer de opzoeking op naam...")
                operator_id = bol_client.get_economic_operator_id(sync.BOL_ECONOMIC_OPERATOR_NAME)
                print(f"Gevonden! economicOperatorId voor '{sync.BOL_ECONOMIC_OPERATOR_NAME}': {operator_id}")
        except bol_client.BolAPIError as e:
            print(f"Mislukt: {e}")
    elif command == "watchdog":
        # Waakhond: mailt als de synchronisatie te lang stilstaat (en als het weer werkt, en elke maandag een overzicht).
        # Draait elk uur via GitHub's eigen tijdschema. Met 'test' komt alleen een proefbericht, zonder iets op te slaan.
        test_mail = len(sys.argv) > 2 and sys.argv[2] == "test"
        watchdog_lines, watchdog_failed = sync.watchdog_check(test_mail=test_mail)
        for line in watchdog_lines:
            print(line)
        if watchdog_failed:
            sys.exit(1)  # de workflow wordt dan rood: dit is een melding dat de waakhond zijn werk niet kon doen
    elif command == "reclaim-space":
        # Geeft lege ruimte terug aan de database (VACUUM FULL op book_uploaded_images) zonder iets te wissen
        # of te wijzigen. Met 'rapport' alleen een rapport, zonder iets te herschrijven.
        report_only = len(sys.argv) > 2 and sys.argv[2] == "rapport"
        for line in sync.reclaim_space(real=not report_only):
            print(line)
    elif command == "check-storage":
        # Opslagcontrole: mailt bij 90, 95, 98 en 100% van de limiet. Dit draait ook vanzelf als eerste stap
        # van elke volledige sync. Met 'test' komt er alleen een proefbericht, zonder iets op te slaan.
        test_mail = len(sys.argv) > 2 and sys.argv[2] == "test"
        for line in sync.check_storage_alerts(test_mail=test_mail):
            print(line)
    elif command == "photo-vacuum":
        # Fotostofzuiger: wist van oude foto's (die niet de voorkant zijn) het bestand, om de database
        # klein te houden. Zonder 'real' is het een proefrun: er wordt niets gewijzigd.
        real = len(sys.argv) > 2 and sys.argv[2] == "real"
        for line in sync.photo_vacuum(real=real):
            print(line)
    elif command == "test-bol-offers-v11":
        # Veilige controle van versie 11 van Bol's aanbiedingen-API: alleen lezen en vergelijken met
        # wat de app nu via versie 10 ziet. Met 'write-noop' wordt bij één aanbieding de voorraad
        # bijgewerkt naar precies het aantal dat er al staat (dus zonder iets te veranderen).
        write_noop = len(sys.argv) > 2 and sys.argv[2] == "write-noop"
        try:
            for line in bol_client.check_offers_v11(write_noop=write_noop):
                print(line)
        except bol_client.BolAPIError as e:
            print(f"Mislukt: {e}")
    elif command == "push-new-books-to-bol":
        # Draait ALLEEN het aanmaken van Bol-aanbiedingen voor nieuwe, via de app
        # aangemaakte boeken — de rest van de sync (orders, voorraad, enz.) draait niet mee.
        sync.push_new_books_to_bol()
        print("Klaar. Zie 'Laatste sync-runs' op Home (Bron: new_offers, Met: Bol) voor het resultaat.")
    elif command == "drip-push-new-books":
        sync.drip_push_new_books()
        print("Klaar (druppelt hooguit één boek per keer, en alleen tijdens de ingestelde drukke perioden).")
    elif command == "backfill-shipping-format":
        # Eenmalig: vult het nieuwe verplichte veld 'verzendformaat' bij Boekwinkeltjes voor bestaande boeken
        # (briefpost -> Brievenbuspakje, pakketpost -> Normaal pakket). Zonder 'real' is het een proefrun.
        # Optioneel daarachter de twee bedragen, bijvoorbeeld: backfill-shipping-format real 3.95 7.25
        args = sys.argv[2:]
        real = bool(args) and args[0] == "real"
        if real:
            args = args[1:]
        brief = float(args[0].replace(",", ".")) if len(args) >= 1 and args[0] else None
        pakket = float(args[1].replace(",", ".")) if len(args) >= 2 and args[1] else None
        for line in sync.backfill_shipping_format(real=real, briefpost=brief, pakketpost=pakket):
            print(line)
    elif command == "update-legacy-shipping-cost":
        # Eenmalig: boeken met de oude briefpost-verzendkosten (3,75 of 1,40) krijgen de huidige briefpost-kosten
        # en verzendformaat Brievenbuspakje, bij Boekwinkeltjes en lokaal. Zonder 'real' is het een proefrun.
        args = sys.argv[2:]
        real = bool(args) and args[0] == "real"
        if real:
            args = args[1:]
        brief = float(args[0].replace(",", ".")) if args and args[0] else None
        for line in sync.update_legacy_shipping_cost(real=real, briefpost=brief):
            print(line)
    elif command == "compare-main-images":
        # Alleen lezen: vergelijkt de foto's die wij eerder opsloegen met wat Boekwinkeltjes nu teruggeeft
        # en schrijft hoofdafbeelding_controle.csv. Optioneel een maximum aantal boeken, bijvoorbeeld: compare-main-images 50
        limit = int(sys.argv[2]) if len(sys.argv) >= 3 and sys.argv[2].strip() else None
        for line in sync.compare_main_images(limit=limit):
            print(line)
    elif command == "show-main-image":
        # Alleen lezen: toont per boeknummer de foto's (API), wat wij eerder opsloegen en de hoofdfoto op de boekpagina.
        # Bijvoorbeeld: show-main-image 245006264 245006265
        ids = [int(x) for x in re.split(r"[\s,;]+", " ".join(sys.argv[2:]).strip()) if x]
        for line in sync.show_main_image(ids):
            print(line)
    elif command == "explore-main-image-form":
        # Alleen lezen: laat zien welke links/formulieren de website heeft om de hoofdfoto in te stellen.
        # Bijvoorbeeld: explore-main-image-form 245006264
        for line in sync.explore_main_image_form(int(sys.argv[2])):
            print(line)
    elif command == "send-daily-csv-export":
        sync.send_daily_csv_export()
        print("Dagelijkse CSV-back-up klaar (Dropbox, of anders per e-mail; zie 'Laatste sync-runs' op Home).")
    else:
        print(f"Onbekend commando: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()

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
    python main.py reclaim-space [rapport]  # geeft lege ruimte terug aan de database; wist NIETS (met 'rapport' alleen een rapport)
    python main.py check-storage [test]  # opslagcontrole: mailt bij 90/95/98/100% van de limiet; met 'test' een proefbericht
    python main.py photo-vacuum [real]   # fotostofzuiger: proefrun, of met 'real' echt afbeeldingsbestanden opruimen
    python main.py test-bol-offers-v11 [write-noop]   # veilige controle van Bol's aanbiedingen-API v11 (alleen lezen)
    python main.py push-new-books-to-bol  # ALLEEN nieuwe boeken (via de app aangemaakt) naar Bol pushen, verder niets
    python main.py drip-push-new-books    # druppelsgewijs (max 1 per interval, alleen in drukke perioden) nieuwe boeken naar Boekwinkeltjes
    python main.py send-daily-csv-export  # stuurt een e-mail met boeken.csv en orders.csv als back-up
"""

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
    elif command == "send-daily-csv-export":
        sync.send_daily_csv_export()
        print("Dagelijkse CSV-back-up verstuurd per e-mail.")
    else:
        print(f"Onbekend commando: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()

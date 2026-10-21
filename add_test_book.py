"""
add_test_book.py — Maakt één testboek aan via de API (POST /books).

Gebruik dit om de sync te testen: maak een testboek aan, run daarna
`python main.py pull-books` en controleer of het boek in je lokale database staat.

LET OP: als BOEKWINKELTJES_BASE_URL in .env op de live-omgeving staat, zet dit
script een ECHT boek in je winkel. Test dit dus eerst in de zandbak-omgeving.
"""

import api_client

TEST_BOOK = {
    "bookNumber": 999999999,
    "location": "test-doos 1",
    "amount": 1,
    "category1": "literatuur",
    "language": "NL",
    "author": "Test, Auteur",
    "title": "Testboek voor sync",
    "publisher": "Testuitgeverij",
    "shortDescription": "Dit is een testboek om de synchronisatie te controleren.",
    "longDescription": "Aangemaakt door add_test_book.py — mag na een geslaagde test weer verwijderd worden.",
    "price": 1.00,
    "shippingCost": 3.50,
    # shippingCategory wordt hieronder automatisch bepaald op basis van je
    # ingestelde verzendkosten-type (none / fixed / variable), zie main().
}


def determine_shipping_category():
    """
    Vraagt de verzendkosten-instellingen op en bepaalt een geldige shippingCategory.

    - type 'none' of 'fixed': shippingCategory laat je gewoon weg (elke waarde
      anders dan een bestaande variabele categorie geeft anders 'validation.invalid_category').
    - type 'variable': shippingCategory moet het id zijn van een van de
      geconfigureerde categorieën onder 'options'.
    """
    fees = api_client.get_shipping_fees()
    data = fees.get("data", fees)
    fee_type = data.get("type")

    print(f"Verzendkosten-type op je account: {fee_type!r}")

    if fee_type == "variable":
        options = data.get("options", [])
        if not options:
            raise RuntimeError(
                "Verzendkostentype is 'variable', maar er zijn geen categorieën "
                "geconfigureerd. Maak er eerst één aan via POST /shipping-fees, "
                "of zet het type om naar 'fixed'/'none'."
            )
        chosen = options[0]
        print(f"Gebruik categorie: id={chosen['id']} naam={chosen.get('name')!r}")
        return chosen["id"]

    # 'none' of 'fixed': geen shippingCategory meesturen
    return None


def main():
    shipping_category = determine_shipping_category()
    payload = dict(TEST_BOOK)
    if shipping_category is not None:
        payload["shippingCategory"] = shipping_category

    result = api_client.create_book(payload)
    book = result.get("data", result)
    print("Testboek aangemaakt:")
    print(f"  id:          {book.get('id')}")
    print(f"  title:       {book.get('title')}")
    print(f"  weblink:     {book.get('weblink')}")
    print()
    print("Run nu: python main.py pull-books")
    print("en controleer in de books-tabel of dit boek is binnengekomen.")


if __name__ == "__main__":
    main()

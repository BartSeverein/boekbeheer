"""
webshop/i18n.py — alle vaste teksten van de webshop in vier talen, plus de URL-regels per taal.

Eén bron voor alles: de generator (pages.py), c.js (de "js_"-teksten, via c.js zelf) en de PHP-scripts
(de "php_"- en "mail_"-teksten, via het gegenereerde i18n.php).

Nederlands staat op de hoofdadres (/boek/...), de andere talen onder /en/, /de/ en /fr/ met vertaalde padnamen
(/en/book/..., /de/buch/..., /fr/livre/...). Boektitels, auteurs en uitgevers worden nooit vertaald.
De beschrijvingen van boeken en de categorienamen gaan via DeepL (translate.py).
"""

import re

LANGS = ("nl", "en", "de", "fr")
IDX = {l: i for i, l in enumerate(LANGS)}
LOCALE = {"nl": "nl_NL", "en": "en_GB", "de": "de_DE", "fr": "fr_FR"}
DEEPL_TARGET = {"en": "EN-GB", "de": "DE", "fr": "FR"}
LANG_LABEL = {"nl": "NL", "en": "EN", "de": "DE", "fr": "FR"}
LANG_NAME = {"nl": "Nederlands", "en": "English", "de": "Deutsch", "fr": "Français"}

# sleutel: (nl, en, de, fr)
T = {
    # --- gedeeld skelet
    "p_search_ph": ("Zoek op titel, auteur of ISBN", "Search by title, author or ISBN", "Suche nach Titel, Autor oder ISBN", "Rechercher par titre, auteur ou ISBN"),
    "p_search_btn": ("Zoek", "Search", "Suchen", "Chercher"),
    "p_terms": ("Voorwaarden en verzending", "Terms and shipping", "Bedingungen und Versand", "Conditions et livraison"),
    "p_privacy": ("Privacy", "Privacy", "Datenschutz", "Confidentialité"),
    "p_cats": ("Categorieën", "Categories", "Kategorien", "Catégories"),
    "p_crumb": ("Kruimelpad", "Breadcrumb", "Brotkrümelnavigation", "Fil d'Ariane"),
    "p_paging": ("Paginering", "Pagination", "Seitennavigation", "Pagination"),
    "p_lang": ("Taal", "Language", "Sprache", "Langue"),
    "p_home": ("Home", "Home", "Startseite", "Accueil"),
    "p_more_cats": ("Overige categorieën", "Other categories", "Weitere Kategorien", "Autres catégories"),
    "p_more_cats_intro": ("Alle andere categorieën, gesorteerd op aantal boeken.", "All other categories, sorted by number of books.",
                          "Alle weiteren Kategorien, sortiert nach Anzahl der Bücher.", "Toutes les autres catégories, classées par nombre de livres."),
    "p_show_photo": ("Toon foto {n}", "Show photo {n}", "Foto {n} anzeigen", "Afficher la photo {n}"),
    "p_about_nl": ("Over dit boek", "About this book (in Dutch)", "Über dieses Buch (auf Niederländisch)",
                   "À propos de ce livre (en néerlandais)"),
    "p_none": ("geen", "none", "keine", "aucun"),
    "p_other": ("overig", "other", "sonstige", "autres"),
    "p_tagline": ("Eindeloze variatie in buitengewone boeken: van de mooiste beelden tot de beste verhalen!",
                  "Endless variety in extraordinary books: from the most beautiful images to the best stories!",
                  "Endlose Vielfalt an außergewöhnlichen Büchern: von den schönsten Bildern bis zu den besten Geschichten!",
                  "Une variété infinie de livres extraordinaires : des plus belles images aux meilleures histoires !"),
    "p_title_suffix": ("boeken", "books", "Bücher", "livres"),
    "p_browse_cats": ("Bekijk categorieën", "Browse categories", "Kategorien ansehen", "Voir les catégories"),
    "p_about_me": ("Over mij", "About me", "Über mich", "À propos de moi"),
    "p_about_me_text": (
        "Ik ben een boekenliefhebber met boeken voor andere boekenliefhebbers. Door scherp te zijn op mooie kansen en door mijn brede netwerk van mensen die ook van boeken houden kan ik vaak de parels vinden die jij zoekt. Zo is langzaam een prachtige voorraad aan boeken ontstaan, met zorg opgeborgen en wachtend op een nieuwe lezer. Een eindeloze variatie in buitengewone boeken: van de mooiste beelden tot de beste verhalen!",
        "I'm a book enthusiast with books for other book enthusiasts. By staying alert to good opportunities and thanks to my wide network of people who love books too, I can often find the gems you're looking for. That's how a beautiful stock of books has slowly grown, stored with care and waiting for a new reader. Endless variety in extraordinary books: from the most beautiful images to the best stories!",
        "Ich bin ein Bücherliebhaber mit Büchern für andere Bücherliebhaber. Dank eines wachen Blicks für schöne Gelegenheiten und dank meines breiten Netzwerks von Menschen, die ebenfalls Bücher lieben, finde ich oft die Perlen, nach denen Sie suchen. So ist nach und nach ein wunderbarer Bestand an Büchern entstanden, sorgfältig aufbewahrt und wartend auf einen neuen Leser. Eine endlose Vielfalt an außergewöhnlichen Büchern: von den schönsten Bildern bis zu den besten Geschichten!",
        "Je suis un amoureux des livres, avec des livres pour d'autres amoureux des livres. Grâce à un œil attentif aux belles occasions et grâce à mon vaste réseau de personnes qui aiment aussi les livres, je peux souvent dénicher les perles que vous cherchez. C'est ainsi qu'est né peu à peu un magnifique stock de livres, rangés avec soin et attendant un nouveau lecteur. Une variété infinie de livres extraordinaires : des plus belles images aux plus belles histoires !"),
    "p_close": ("Sluiten", "Close", "Schließen", "Fermer"),
    "p_newest": ("Nieuw binnen", "New arrivals", "Neu eingetroffen", "Nouveautés"),
    # --- categorie en paginering
    "p_page_n": ("(pagina {n})", "(page {n})", "(Seite {n})", "(page {n})"),
    "p_in_cat": ("{total} boeken in deze categorie.", "{total} books in this category.",
                 "{total} Bücher in dieser Kategorie.", "{total} livres dans cette catégorie."),
    "p_in_cat_one": ("1 boek in deze categorie (maar wel een mooie!).", "1 book in this category (but a lovely one!).",
                     "1 Buch in dieser Kategorie (aber ein schönes!).", "1 livre dans cette catégorie (mais un beau !)."),
    "p_cat_desc": ("{label}: {total} boeken{suffix}. Bekijk het aanbod van {shop}.",
                   "{label}: {total} books{suffix}. Browse the range at {shop}.",
                   "{label}: {total} Bücher{suffix}. Entdecken Sie das Angebot von {shop}.",
                   "{label} : {total} livres{suffix}. Découvrez l'offre de {shop}."),
    "p_prev": ("← Vorige", "← Previous", "← Zurück", "← Précédent"),
    "p_next": ("Volgende →", "Next →", "Weiter →", "Suivant →"),
    "p_page_of": ("Pagina {p} van {n}", "Page {p} of {n}", "Seite {p} von {n}", "Page {p} sur {n}"),
    # --- boekpagina
    "l_author": ("Auteur", "Author", "Autor", "Auteur"),
    "l_publisher": ("Uitgever", "Publisher", "Verlag", "Éditeur"),
    "l_isbn": ("ISBN", "ISBN", "ISBN", "ISBN"),
    "l_lang": ("Taal", "Language", "Sprache", "Langue"),
    "l_notes": ("Bijzonderheden", "Condition notes", "Besonderheiten", "Particularités"),
    "l_cat": ("Categorie", "Category", "Kategorie", "Catégorie"),
    "p_share_lead": ("Deel dit boek via", "Share this book via", "Teile dieses Buch per", "Partagez ce livre par"),
    "p_share_wa": ("WhatsApp", "WhatsApp", "WhatsApp", "WhatsApp"),
    "p_share_mail": ("e-mail", "email", "E-Mail", "e-mail"),
    "p_share_of": ("of", "or", "oder", "ou"),
    "p_share_link": ("link", "link", "Link", "lien"),
    "p_share_msg": ("Ik deel dit boek graag met jou: ", "I'd like to share this book with you: ", "Ich teile dieses Buch gern mit dir: ",
                    "Je partage volontiers ce livre avec toi : "),
    "js_copied": ("Link gekopieerd!", "Link copied!", "Link kopiert!", "Lien copié !"),
    "p_about": ("Over dit boek", "About this book", "Über dieses Buch", "À propos de ce livre"),
    "p_in_stock": ("Op voorraad", "In stock", "Auf Lager", "En stock"),
    "p_copies": (" ({n} exemplaren)", " ({n} copies)", " ({n} Exemplare)", " ({n} exemplaires)"),
    "p_ship_line": ("Verzendkosten {ship} (in Nederland).",
                    "Shipping {ship} (in the Netherlands; for other rates see Terms and shipping).",
                    "Versandkosten {ship} (in den Niederlanden; für andere Tarife siehe Bedingungen und Versand).",
                    "Frais de port {ship} (aux Pays-Bas ; pour les autres tarifs, voir Conditions et livraison)."),
    "p_ship_line_one": ("Verzendkosten {ship}.", "Shipping {ship}.", "Versandkosten {ship}.", "Frais de port {ship}."),
    "p_plus": (" plus toeslag", " plus surcharge", " plus Zuschlag", " plus supplément"),
    "p_abroad": ("Dit tarief geldt voor Nederland; naar andere landen komt er een toeslag bij (zie Voorwaarden en verzending).",
                 "This rate is for the Netherlands; other countries cost extra (see Terms and shipping).",
                 "Dieser Satz gilt für die Niederlande; für andere Länder kommt ein Aufschlag hinzu (siehe Bedingungen und Versand).",
                 "Ce tarif vaut pour les Pays-Bas ; un supplément s'ajoute pour les autres pays (voir Conditions et livraison)."),
    "p_add": ("In winkelwagen leggen", "Add to cart", "In den Warenkorb legen", "Ajouter au panier"),
    "p_view_cart": ("Bekijk winkelwagen", "View cart", "Warenkorb ansehen", "Voir le panier"),
    "p_by": (" van {author}", " by {author}", " von {author}", " de {author}"),
    "p_meta_book": ("{price}. Snel verzonden.", "{price}. Fast shipping.",
                    "{price}. Schneller Versand.", "{price}. Expédition rapide."),
    # --- zoeken, winkelwagen, afrekenen, bedankt, 404
    "p_search": ("Zoeken", "Search", "Suche", "Recherche"),
    "p_search_desc": ("Zoek in mijn aanbod van boeken.", "Search my range of books.",
                      "Durchsuchen Sie mein Angebot an Büchern.", "Parcourez mon offre de livres."),
    "p_more": ("Meer resultaten", "More results", "Weitere Ergebnisse", "Plus de résultats"),
    "p_cart": ("Winkelwagen", "Cart", "Warenkorb", "Panier"),
    "p_cart_desc": ("Je winkelwagen.", "Your cart.", "Ihr Warenkorb.", "Votre panier."),
    "p_nojs": ("Voor de winkelwagen is JavaScript nodig.", "The cart requires JavaScript.",
               "Für den Warenkorb wird JavaScript benötigt.", "Le panier nécessite JavaScript."),
    "p_checkout": ("Afrekenen", "Checkout", "Kasse", "Paiement"),
    "f_name": ("Naam", "Name", "Name", "Nom"),
    "f_email": ("E-mailadres", "Email address", "E-Mail-Adresse", "Adresse e-mail"),
    "f_street": ("Straat en huisnummer", "Street and house number", "Straße und Hausnummer", "Rue et numéro"),
    "f_postcode": ("Postcode", "Postcode", "Postleitzahl", "Code postal"),
    "f_city": ("Plaats", "City", "Ort", "Ville"),
    "f_country": ("Land", "Country", "Land", "Pays"),
    "f_note": ("Opmerking (optioneel)", "Note (optional)", "Anmerkung (optional)", "Remarque (facultatif)"),
    "f_website": ("Website", "Website", "Website", "Site web"),
    "p_agree": ("Door te betalen ga je akkoord met de {a}voorwaarden{/a}. Je rekent veilig af via Mollie (o.a. Wero).",
                "By paying you agree to the {a}terms{/a}. You pay securely via Mollie (e.g. Wero).",
                "Mit der Zahlung akzeptieren Sie die {a}Bedingungen{/a}. Sie bezahlen sicher über Mollie (z. B. Wero).",
                "En payant, vous acceptez les {a}conditions{/a}. Vous payez en toute sécurité via Mollie (par ex. Wero)."),
    "f_pay": ("Betalen", "Pay", "Bezahlen", "Payer"),
    "p_order": ("Bestelling", "Order", "Bestellung", "Commande"),
    "p_order_desc": ("Status van je bestelling.", "Status of your order.", "Status Ihrer Bestellung.", "Statut de votre commande."),
    "p_keep_shopping": ("Verder winkelen", "Continue shopping", "Weiter einkaufen", "Continuer vos achats"),
    "p_404_title": ("Pagina niet gevonden", "Page not found", "Seite nicht gefunden", "Page introuvable"),
    "p_404_text": ("Dit boek is mogelijk verkocht of de pagina bestaat niet meer. Zoek hierboven verder of kies een categorie.",
                   "This book may have been sold or the page no longer exists. Keep searching above or pick a category.",
                   "Dieses Buch wurde möglicherweise verkauft oder die Seite existiert nicht mehr. Suchen Sie oben weiter oder wählen Sie eine Kategorie.",
                   "Ce livre a peut-être été vendu ou la page n'existe plus. Poursuivez votre recherche ci-dessus ou choisissez une catégorie."),
    # --- teksten in c.js
    "js_cart": ("Winkelwagen", "Cart", "Warenkorb", "Panier"),
    "js_in_cart": ("In winkelwagen gelegd", "Added to cart", "In den Warenkorb gelegt", "Ajouté au panier"),
    "js_add": ("In winkelwagen leggen", "Add to cart", "In den Warenkorb legen", "Ajouter au panier"),
    "js_empty": ("Je winkelwagen is leeg.", "Your cart is empty.", "Ihr Warenkorb ist leer.", "Votre panier est vide."),
    "js_remove": ("Verwijder", "Remove", "Entfernen", "Retirer"),
    "js_books": ("Boeken", "Books", "Bücher", "Livres"),
    "js_shipping": ("Verzendkosten", "Shipping", "Versandkosten", "Frais de port"),
    "js_free": ("gratis", "free", "kostenlos", "gratuit"),
    "js_total": ("Totaal", "Total", "Gesamt", "Total"),
    "js_checkout": ("Afrekenen", "Checkout", "Zur Kasse", "Commander"),
    "js_sum": ("Totaal: {total} (boeken {sub} + verzending {ship})", "Total: {total} (books {sub} + shipping {ship})",
               "Gesamt: {total} (Bücher {sub} + Versand {ship})", "Total : {total} (livres {sub} + livraison {ship})"),
    "js_wait": ("Even geduld...", "One moment...", "Einen Moment bitte...", "Un instant..."),
    "js_unexpected": ("Onverwacht antwoord van de server.", "Unexpected response from the server.",
                      "Unerwartete Antwort vom Server.", "Réponse inattendue du serveur."),
    "js_oops": ("Er ging iets mis. Probeer het opnieuw.", "Something went wrong. Please try again.",
                "Etwas ist schiefgelaufen. Bitte versuchen Sie es erneut.", "Une erreur s'est produite. Veuillez réessayer."),
    "js_noconn": ("Geen verbinding. Probeer het opnieuw.", "No connection. Please try again.",
                  "Keine Verbindung. Bitte versuchen Sie es erneut.", "Pas de connexion. Veuillez réessayer."),
    "js_abroad": ("Dit zijn de verzendkosten voor Nederland; naar andere landen zijn de kosten anders (zie Voorwaarden en verzending).",
                  "This is the shipping for the Netherlands; for other countries the costs differ (see Terms and shipping).",
                  "Dies sind die Versandkosten für die Niederlande; für andere Länder fallen andere Kosten an (siehe Bedingungen und Versand).",
                  "Ce sont les frais pour les Pays-Bas ; pour les autres pays, les frais sont différents (voir Conditions et livraison)."),
    "js_t_none": ("Geen bestelling gevonden.", "No order found.", "Keine Bestellung gefunden.", "Aucune commande trouvée."),
    "js_t_paid": ("Bedankt! Je betaling is ontvangen. Je krijgt een bevestiging per e-mail.",
                  "Thank you! I have received your payment. You will receive a confirmation by email.",
                  "Vielen Dank! Ihre Zahlung ist eingegangen. Sie erhalten eine Bestätigung per E-Mail.",
                  "Merci ! J'ai bien reçu votre paiement. Vous recevrez une confirmation par e-mail."),
    "js_t_wait": ("Ik wacht op de bevestiging van je betaling...", "I am waiting for confirmation of your payment...",
                  "Ich warte auf die Bestätigung Ihrer Zahlung...", "J'attends la confirmation de votre paiement..."),
    "js_t_open": ("De betaling is nog niet bevestigd. Zodra die binnen is, krijg je een e-mail. Je winkelwagen blijft bewaard.",
                  "The payment has not been confirmed yet. Once it is, you will receive an email. Your cart is kept.",
                  "Die Zahlung ist noch nicht bestätigt. Sobald sie eingeht, erhalten Sie eine E-Mail. Ihr Warenkorb bleibt gespeichert.",
                  "Le paiement n'est pas encore confirmé. Dès qu'il le sera, vous recevrez un e-mail. Votre panier est conservé."),
    "js_t_failed": ("De betaling is niet gelukt of geannuleerd. Je winkelwagen is bewaard; je kunt het opnieuw proberen.",
                    "The payment failed or was cancelled. Your cart is kept; you can try again.",
                    "Die Zahlung ist fehlgeschlagen oder wurde abgebrochen. Ihr Warenkorb bleibt gespeichert; Sie können es erneut versuchen.",
                    "Le paiement a échoué ou a été annulé. Votre panier est conservé ; vous pouvez réessayer."),
    "js_t_err": ("Status niet op te halen. Controleer je e-mail voor de bevestiging.",
                 "Could not retrieve the status. Check your email for the confirmation.",
                 "Status konnte nicht abgerufen werden. Prüfen Sie Ihre E-Mails auf die Bestätigung.",
                 "Impossible de récupérer le statut. Vérifiez votre e-mail pour la confirmation."),
    "js_s_wait": ("Zoeken...", "Searching...", "Suche läuft...", "Recherche..."),
    "js_s_one": ("boek gevonden", "book found", "Buch gefunden", "livre trouvé"),
    "js_s_many": ("boeken gevonden", "books found", "Bücher gefunden", "livres trouvés"),
    "js_s_none": ("Geen boeken gevonden.", "No books found.", "Keine Bücher gefunden.", "Aucun livre trouvé."),
    "js_s_min": ("Vul minstens twee tekens in.", "Enter at least two characters.",
                 "Geben Sie mindestens zwei Zeichen ein.", "Saisissez au moins deux caractères."),
    "js_s_fail": ("Zoeken is nu niet mogelijk.", "Search is not available right now.",
                  "Die Suche ist derzeit nicht verfügbar.", "La recherche est momentanément indisponible."),
    # --- teksten in de PHP-scripts
    "php_invalid": ("Ongeldig verzoek.", "Invalid request.", "Ungültige Anfrage.", "Requête non valide."),
    "php_too_big": ("Verzoek te groot.", "Request too large.", "Anfrage zu groß.", "Requête trop volumineuse."),
    "php_retry": ("Er ging iets mis. Probeer het opnieuw.", "Something went wrong. Please try again.",
                  "Etwas ist schiefgelaufen. Bitte versuchen Sie es erneut.", "Une erreur s'est produite. Veuillez réessayer."),
    "php_too_many": ("Te veel pogingen. Probeer het over een paar minuten opnieuw.", "Too many attempts. Please try again in a few minutes.",
                     "Zu viele Versuche. Bitte versuchen Sie es in ein paar Minuten erneut.", "Trop de tentatives. Veuillez réessayer dans quelques minutes."),
    "php_required": ("Vul alle verplichte velden in.", "Please fill in all required fields.",
                     "Bitte füllen Sie alle Pflichtfelder aus.", "Veuillez remplir tous les champs obligatoires."),
    "php_email": ("Dit e-mailadres lijkt niet te kloppen.", "This email address doesn't look right.",
                  "Diese E-Mail-Adresse scheint nicht zu stimmen.", "Cette adresse e-mail semble incorrecte."),
    "php_country": ("Ik verzend helaas niet naar dit land.", "Unfortunately I do not ship to this country.",
                    "Leider versende ich nicht in dieses Land.", "Je n'expédie malheureusement pas vers ce pays."),
    "php_cart_bad": ("Je winkelwagen is leeg of te groot.", "Your cart is empty or too large.",
                     "Ihr Warenkorb ist leer oder zu groß.", "Votre panier est vide ou trop volumineux."),
    "php_sold": ("Helaas, een of meer boeken zijn net verkocht. Ik heb ze uit je winkelwagen gehaald.",
                 "Sorry, one or more books have just been sold. I have removed them from your cart.",
                 "Leider wurden ein oder mehrere Bücher gerade verkauft. Ich habe sie aus Ihrem Warenkorb entfernt.",
                 "Désolé, un ou plusieurs livres viennent d'être vendus. Je les ai retirés de votre panier."),
    "php_prices": ("De prijzen zijn gewijzigd. Controleer je winkelwagen en probeer het opnieuw.",
                   "The prices have changed. Please check your cart and try again.",
                   "Die Preise haben sich geändert. Bitte prüfen Sie Ihren Warenkorb und versuchen Sie es erneut.",
                   "Les prix ont changé. Veuillez vérifier votre panier et réessayer."),
    "php_pay_fail": ("Betalen is nu niet mogelijk. Probeer het later opnieuw.", "Payment is not possible right now. Please try again later.",
                     "Die Zahlung ist derzeit nicht möglich. Bitte versuchen Sie es später erneut.",
                     "Le paiement est momentanément impossible. Veuillez réessayer plus tard."),
    "php_error": ("Er ging iets mis. Probeer het later opnieuw.", "Something went wrong. Please try again later.",
                  "Etwas ist schiefgelaufen. Bitte versuchen Sie es später erneut.", "Une erreur s'est produite. Veuillez réessayer plus tard."),
    "php_search_min": ("Vul minstens twee tekens in.", "Enter at least two characters.",
                       "Geben Sie mindestens zwei Zeichen ein.", "Saisissez au moins deux caractères."),
    "php_search_fail": ("Zoeken is nu niet mogelijk.", "Search is not available right now.",
                        "Die Suche ist derzeit nicht verfügbar.", "La recherche est momentanément indisponible."),
    "php_desc": ("Bestelling #{id} bij {shop}", "Order #{id} at {shop}", "Bestellung #{id} bei {shop}", "Commande n°{id} chez {shop}"),
    # --- e-mail aan de klant
    "mail_subject": ("Bevestiging bestelling #{id}", "Order confirmation #{id}", "Bestellbestätigung #{id}", "Confirmation de la commande n°{id}"),
    "mail_greeting": ("Beste klant,", "Dear customer,", "Sehr geehrte Kundin, sehr geehrter Kunde,", "Cher client, chère cliente,"),
    "mail_thanks": ("Bedankt voor je bestelling #{id}.", "Thank you for your order #{id}.",
                    "Vielen Dank für Ihre Bestellung #{id}.", "Merci pour votre commande n°{id}."),
    "mail_ship": ("Verzendkosten", "Shipping", "Versandkosten", "Frais de port"),
    "mail_total": ("Totaal", "Total", "Gesamt", "Total"),
    "mail_addr": ("Verzendadres", "Shipping address", "Lieferadresse", "Adresse de livraison"),
    "mail_reply": ("Vragen over je bestelling? Stuur die gewoon als antwoord op deze e-mail.",
                   "Questions about your order? Just send them as a reply to this email.",
                   "Fragen zu Ihrer Bestellung? Senden Sie diese einfach als Antwort auf diese E-Mail.",
                   "Des questions sur votre commande ? Envoyez-les simplement en réponse à cet e-mail."),
    "mail_regards": ("Met vriendelijke groet,", "Kind regards,", "Mit freundlichen Grüßen", "Cordialement,"),
}

# Padnamen: sleutel -> (nl, en, de, fr)
SEG = {
    "book": ("boek", "book", "buch", "livre"),
    "category": ("categorie", "category", "kategorie", "categorie"),
    "page": ("pagina", "page", "seite", "page"),
    "search": ("zoeken", "search", "suche", "recherche"),
    "cart": ("winkelwagen", "cart", "warenkorb", "panier"),
    "checkout": ("afrekenen", "checkout", "kasse", "paiement"),
    "thanks": ("bedankt", "thanks", "danke", "merci"),
    "terms": ("voorwaarden", "terms", "bedingungen", "conditions"),
    "privacy": ("privacy", "privacy", "datenschutz", "confidentialite"),
    "morecats": ("overige-categorieen", "other-categories", "weitere-kategorien", "autres-categories"),
}
_NL_SEG = {v[0]: k for k, v in SEG.items()}

COUNTRY_NAMES = {
    "NL": ("Nederland", "Netherlands", "Niederlande", "Pays-Bas"),
    "BE": ("België", "Belgium", "Belgien", "Belgique"),
    "DE": ("Duitsland", "Germany", "Deutschland", "Allemagne"),
    "FR": ("Frankrijk", "France", "Frankreich", "France"),
    "LU": ("Luxemburg", "Luxembourg", "Luxemburg", "Luxembourg"),
    "AT": ("Oostenrijk", "Austria", "Österreich", "Autriche"),
    "IE": ("Ierland", "Ireland", "Irland", "Irlande"),
    "ES": ("Spanje", "Spain", "Spanien", "Espagne"),
    "IT": ("Italië", "Italy", "Italien", "Italie"),
    "DK": ("Denemarken", "Denmark", "Dänemark", "Danemark"),
    "SE": ("Zweden", "Sweden", "Schweden", "Suède"),
    "PL": ("Polen", "Poland", "Polen", "Pologne"),
    "PT": ("Portugal", "Portugal", "Portugal", "Portugal"),
    "GB": ("Verenigd Koninkrijk", "United Kingdom", "Vereinigtes Königreich", "Royaume-Uni"),
    "CH": ("Zwitserland", "Switzerland", "Schweiz", "Suisse"),
}

BOOK_LANGUAGES = {  # taalcode (zoals in de database, bijv. NL, de) -> (nl, en, de, fr)
    "nl": ("Nederlands", "Dutch", "Niederländisch", "Néerlandais"),
    "en": ("Engels", "English", "Englisch", "Anglais"),
    "de": ("Duits", "German", "Deutsch", "Allemand"),
    "fr": ("Frans", "French", "Französisch", "Français"),
    "es": ("Spaans", "Spanish", "Spanisch", "Espagnol"),
    "it": ("Italiaans", "Italian", "Italienisch", "Italien"),
    "la": ("Latijn", "Latin", "Latein", "Latin"),
    "fy": ("Fries", "Frisian", "Friesisch", "Frison"),
    "pt": ("Portugees", "Portuguese", "Portugiesisch", "Portugais"),
    "sv": ("Zweeds", "Swedish", "Schwedisch", "Suédois"),
    "da": ("Deens", "Danish", "Dänisch", "Danois"),
    "no": ("Noors", "Norwegian", "Norwegisch", "Norvégien"),
    "ru": ("Russisch", "Russian", "Russisch", "Russe"),
    "pl": ("Pools", "Polish", "Polnisch", "Polonais"),
    "cs": ("Tsjechisch", "Czech", "Tschechisch", "Tchèque"),
    "hu": ("Hongaars", "Hungarian", "Ungarisch", "Hongrois"),
    "el": ("Grieks", "Greek", "Griechisch", "Grec"),
    "tr": ("Turks", "Turkish", "Türkisch", "Turc"),
    "af": ("Afrikaans", "Afrikaans", "Afrikaans", "Afrikaans"),
    "id": ("Indonesisch", "Indonesian", "Indonesisch", "Indonésien"),
    "ja": ("Japans", "Japanese", "Japanisch", "Japonais"),
    "zh": ("Chinees", "Chinese", "Chinesisch", "Chinois"),
    "he": ("Hebreeuws", "Hebrew", "Hebräisch", "Hébreu"),
    "ar": ("Arabisch", "Arabic", "Arabisch", "Arabe"),
}
# Alternatieve schrijfwijzen in de database: Nederlandse namen, Engelse namen en 3-lettercodes.
_LANG_ALIAS = {}
for _code, _row in BOOK_LANGUAGES.items():
    _LANG_ALIAS[_code] = _code
    for _name in _row:
        _LANG_ALIAS[_name.lower()] = _code
_LANG_ALIAS.update({
    "dut": "nl", "nld": "nl", "vlaams": "nl", "flemish": "nl", "eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr",
    "spa": "es", "ita": "it", "lat": "la", "fry": "fy", "por": "pt", "swe": "sv", "dan": "da", "dk": "da", "nor": "no",
    "nb": "no", "nn": "no", "rus": "ru", "pol": "pl", "ces": "cs", "cze": "cs", "hun": "hu", "gre": "el", "ell": "el",
    "tur": "tr", "afr": "af", "ind": "id", "jpn": "ja", "jp": "ja", "chi": "zh", "zho": "zh", "heb": "he", "ara": "ar",
    "gb": "en", "us": "en", "uk": "en", "be": "nl",
})


def t(key, lang="nl", **kw):
    row = T[key]
    text = row[IDX.get(lang, 0)]
    return text.format(**kw) if kw else text


def prefix(lang):
    return "" if lang == "nl" else f"/{lang}"


def localize(path, lang):
    """Zet een Nederlands pad (/boek/x-1/, /categorie/a/pagina/2/, /zoeken/, /) om naar het pad in `lang`."""
    if lang == "nl":
        return path
    parts = [p for p in path.split("/") if p]
    if not parts:
        return f"/{lang}/"
    first = _NL_SEG.get(parts[0])
    if first:
        parts[0] = SEG[first][IDX[lang]]
    if len(parts) >= 3 and parts[-2] == "pagina" and parts[-1].isdigit():
        parts[-2] = SEG["page"][IDX[lang]]
    return f"/{lang}/" + "/".join(parts) + "/"


def country_name(code, lang):
    row = COUNTRY_NAMES.get(code)
    return row[IDX[lang]] if row else code


def book_language(value, lang):
    """Volledige taalnaam in `lang` (NL -> Nederlands / Dutch / Niederländisch / Néerlandais). Onbekend: waarde ongewijzigd."""
    raw = (value or "").strip()
    if not raw:
        return raw
    parts = [p.strip() for p in re.split(r"[/,;&+]", raw) if p.strip()]
    names = []
    for p in parts:
        code = _LANG_ALIAS.get(p.lower().rstrip("."))
        if not code:
            return raw
        name = BOOK_LANGUAGES[code][IDX[lang]]
        if name not in names:
            names.append(name)
    return ", ".join(names)


def js_strings(lang):
    return {k[3:]: v[IDX[lang]] for k, v in T.items() if k.startswith("js_")}


def php_data(langs, cfg):
    """Gegevens voor i18n.php: meldingen, mailteksten, paden en Mollie-taalcodes per taal."""
    msgs = {l: {k: v[IDX[l]] for k, v in T.items() if k.startswith(("php_", "mail_"))} for l in langs}
    paths = {
        "book": {l: localize("/boek/", l) for l in langs},
        "thanks": {l: localize("/bedankt/", l) for l in langs},
    }
    return {"langs": list(langs), "msgs": msgs, "paths": paths, "locale": {l: LOCALE[l] for l in langs}}

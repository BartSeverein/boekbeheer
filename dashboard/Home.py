"""
Home.py — startpunt van het Boekwinkeltjes-dashboard: bouwt alleen het menu (in de taal van de gebruiker) en
start daarna de gekozen pagina. De inhoud van 'Home' staat in home_page.py, de andere pagina's in pages/.

Lokaal draaien:
    streamlit run Home.py

Op Streamlit Community Cloud:
    Hoofdbestand Home.py; zet SUPABASE_DB_URL in de app-instellingen onder "Secrets".
"""

import streamlit as st

from i18n import menu_title

# Taal voor het menu bepalen zonder iets te tonen: een gebruiker die via de 'ingelogd blijven'-cookie binnenkomt
# wordt hier alvast in de sessie gezet (require_login op de pagina doet dat anders ook).
lang = "nl"
try:
    from common import _user_from_cookie

    user = st.session_state.get("user")
    if not user and not st.session_state.get("_uitgelogd"):
        user = _user_from_cookie()
        if user:
            st.session_state["user"] = user
    lang = (user or {}).get("language") or "nl"
except Exception:
    lang = "nl"

# (bestand, Nederlandse menutekst, icoon, url-pad). De volgorde is de volgorde in het menu; de url-paden zijn
# dezelfde als voorheen (afgeleid van de bestandsnamen), zodat bestaande links blijven werken.
PAGINAS = [
    ("home_page.py", "Home", "📚", ""),
    ("pages/1_Nieuw_boek.py", "Nieuw boek", "📑", "Nieuw_boek"),
    ("pages/1_Nieuwe_kaart.py", "Nieuwe kaart", "📮", "Nieuwe_kaart"),
    ("pages/2_Toevoegen_in_bulk.py", "Toevoegen in bulk", "📥", "Toevoegen_in_bulk"),
    ("pages/3_Boekdetails.py", "Boekdetails", "📖", "Boekdetails"),
    ("pages/3_Kaartdetails.py", "Kaartdetails", "🖼️", "Kaartdetails"),
    ("pages/5_Orderdetails.py", "Orderdetails", "🛒", "Orderdetails"),
    ("pages/6_Kaarten_in_bulk.py", "Kaarten in bulk", "📦", "Kaarten_in_bulk"),
    ("pages/6_Winkeldochters.py", "Winkeldochters", "🕸️", "Winkeldochters"),
    ("pages/7_Auteursbeheer.py", "Auteursbeheer", "✍️", "Auteursbeheer"),
    ("pages/7_Kaartplaatsbeheer.py", "Kaartplaatsbeheer", "🗺️", "Kaartplaatsbeheer"),
    ("pages/8_Uitgeversbeheer.py", "Uitgeversbeheer", "🏭", "Uitgeversbeheer"),
    ("pages/9_Categoriebeheer.py", "Categoriebeheer", "🏷️", "Categoriebeheer"),
    ("pages/10_Gebruikersbeheer.py", "Gebruikersbeheer", "🔑", "Gebruikersbeheer"),
    ("pages/11_Backup_bekijken.py", "Back-up bekijken", "💾", "Backup_bekijken"),
    ("pages/12_Afbeeldingen.py", "Afbeeldingen", "🖼️", "Afbeeldingen"),
    ("pages/13_Hulp_en_instellingen.py", "Hulp en instellingen", "ℹ️", "Hulp_en_instellingen"),
]

# Pagina's die een Engelse gebruiker niet in het menu ziet (ze blijven wel bereikbaar via hun url).
VERBORGEN_VOOR_EN = {
    "Nieuwe kaart", "Kaartdetails", "Orderdetails", "Kaarten in bulk", "Winkeldochters", "Auteursbeheer",
    "Kaartplaatsbeheer", "Uitgeversbeheer", "Categoriebeheer", "Back-up bekijken", "Afbeeldingen",
}

pages = []
for pad, titel, icoon, url in PAGINAS:
    if lang == "en" and titel in VERBORGEN_VOOR_EN:
        continue
    kwargs = {"title": menu_title(titel, lang), "icon": icoon}
    if url:
        kwargs["url_path"] = url
    else:
        kwargs["default"] = True
    pages.append(st.Page(pad, **kwargs))

st.navigation(pages).run()

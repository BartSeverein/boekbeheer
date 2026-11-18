"""
i18n.py — Nederlands/Engels voor het dashboard.

De taal staat per gebruiker in app_users.language ('nl' of 'en'; leeg = 'nl') en wordt ingesteld op
Gebruikersbeheer. De Nederlandse tekst is zelf de sleutel: t("Opslaan") geeft voor een Engelse gebruiker
de vertaling uit i18n_en.EN, en voor iedereen anders (of bij een ontbrekende vertaling) gewoon het Nederlands.

    t("Opslaan")                              -> "Save"
    t("{0} boeken gevonden", aantal)          -> "{0} books found" met het aantal ingevuld
    t("Hallo {naam}", naam="Bart")            -> idem met een naam

Teksten met invulplekken gebruiken str.format: letterlijke accolades in de tekst staan dubbel ({{ en }}).
"""

import re

import streamlit as st

try:
    from i18n_en import EN
except ImportError:  # i18n_en.py nog niet geüpload: alles blijft Nederlands
    EN = {}

try:
    from i18n_en import MENU_EN
except ImportError:
    MENU_EN = {}

LANGUAGES = {"nl": "Nederlands", "en": "English"}


def current_language():
    user = st.session_state.get("user")
    lang = (user or {}).get("language") or "nl"
    return lang if lang in LANGUAGES else "nl"


def is_english():
    return current_language() == "en"


def t(nl, *args, **kwargs):
    """Vertaal een Nederlandse tekst naar de taal van de ingelogde gebruiker (zie bovenaan)."""
    if is_english():
        text = EN.get(nl)
        if text is None:
            text = EN.get(nl.strip(), nl)
    else:
        text = nl
    if args or kwargs:
        try:
            return text.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            return nl.format(*args, **kwargs)
    return text


def menu_title(nl_title, lang=None):
    """Menutekst voor een pagina; 'lang' meegeven als er nog geen gebruiker in de sessie staat."""
    lang = lang or current_language()
    return MENU_EN.get(nl_title, nl_title) if lang == "en" else nl_title


def category_label(label):
    """
    Categorielabel (Nederlands, zoals op Boekwinkeltjes) voor weergave: voor Engelse gebruikers de vertaling,
    anders ongewijzigd. Wat wordt opgeslagen blijft altijd het Nederlandse label.
    """
    if not label or not is_english():
        return label
    try:
        from categories import ENGLISH_TRANSLATIONS
    except ImportError:
        return label
    if label in EN:
        return EN[label]
    english = ENGLISH_TRANSLATIONS.get(label.strip().lower())
    if not english:
        return label
    return english[:1].upper() + english[1:]


def tdf(df):
    """Tabel voor weergave: voor Engelse gebruikers de kolomkoppen vertalen. De tabel zelf blijft ongewijzigd."""
    if not is_english():
        return df
    try:
        if hasattr(df, "rename") and hasattr(df, "columns"):
            return df.rename(columns=lambda c: t(c) if isinstance(c, str) else c)
    except Exception:
        pass
    return df


def category_option(label):
    """Weergave van een categorie-keuze in een keuzelijst (alleen weergave: de gekozen waarde blijft het Nederlandse label).
    Begrijpt ook 'Categorie › Subcategorie', '(huidig) ...' en '(leeg)'."""
    if not is_english() or not isinstance(label, str):
        return label
    if " › " in label:
        return " › ".join(category_option(part) for part in label.split(" › "))
    match = re.match(r"^\(huidig\) (.*)$", label)
    if match:
        return t("(huidig) {0}", category_label(match.group(1)))
    return category_label(t(label)) if label in EN else category_label(label)

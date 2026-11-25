"""
pages/10_Gebruikersbeheer.py — Gebruikers beheren: aanmaken, aanpassen, verwijderen,
en per gebruiker zien wanneer ze laatst inlogden en hoeveel boeken ze hebben
toegevoegd/aangepast (voor salarisdoeleinden).

Rechten:
- Beheerder ziet alle wachtwoorden, mag nieuwe gebruikers toevoegen, en mag
  elke gebruiker bewerken/verwijderen.
- Gebruiker ziet alleen het eigen wachtwoord (andere wachtwoorden tonen als
  ********), kan geen nieuwe gebruikers toevoegen, en kan alleen het eigen
  account bewerken (geen verwijderknop).
"""

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from common import (
    render_logo,
    require_login,
    get_users_with_stats,
    get_books_added_log,
    get_todo_extras,
    add_todo_extra,
    update_todo_extra,
    delete_todo_extra,
    create_user,
    update_user,
    delete_user,
    format_datetime_nl,
    na,
)
from i18n import LANGUAGES, t, tdf

st.set_page_config(page_title="Gebruikersbeheer", page_icon="🔑", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
current_user = require_login()
st.title(t('🔑 Gebruikersbeheer'))

is_admin = (current_user is None) or (current_user.get("role") == "beheerder")
my_short_name = current_user["short_name"] if current_user else None

users = get_users_with_stats()

st.subheader(t('Overzicht'))
if users:
    df = pd.DataFrame(users)
    df["last_login_at"] = df["last_login_at"].apply(format_datetime_nl)
    df["last_location"] = df["last_location"].apply(na)
    df["email"] = df["email"].apply(na)
    df["language"] = df["language"].apply(lambda v: LANGUAGES.get(v or "nl", v))

    # Wachtwoord afschermen: beheerder ziet alles, gebruiker alleen het eigen wachtwoord
    def _mask_password(row):
        if is_admin or row["short_name"] == my_short_name:
            return na(row["password_plain"])
        return "********"

    df["password_plain"] = df.apply(_mask_password, axis=1)

    df = df.rename(
        columns={
            "full_name": "Volledige naam",
            "short_name": "Korte naam",
            "email": "E-mailadres",
            "password_plain": "Wachtwoord",
            "role": "Rol",
            "language": "Taal",
            "last_location": "Laatste locatie",
            "last_login_at": "Laatst ingelogd",
            "aantal_toegevoegd": "Ingevoerd",
            "aantal_toegevoegd_7d": "Laatste 7 dagen",
        }
    )[
        [
            "Volledige naam",
            "Korte naam",
            "E-mailadres",
            "Wachtwoord",
            "Rol",
            "Taal",
            "Laatst ingelogd",
            "Laatste locatie",
            "Ingevoerd",
            "Laatste 7 dagen",
        ]
    ]
    if not is_admin:   # privacy: alleen de beheerder ziet de aantallen
        df = df.drop(columns=["Ingevoerd", "Laatste 7 dagen"])
    st.dataframe(tdf(df), width="stretch")
else:
    st.info(t('Nog geen gebruikers aangemaakt.'))

st.divider()

if is_admin:
    with st.expander(t('➕ Nieuwe gebruiker toevoegen')):
        with st.form("new_user_form"):
            new_full_name = st.text_input(t('Volledige naam'))
            new_short_name = st.text_input(t('Korte naam (= gebruikersnaam om in te loggen)'))
            new_email = st.text_input(t('E-mailadres'))
            new_password = st.text_input(t('Wachtwoord'), type="password")
            new_language = st.selectbox(t('Taal'), list(LANGUAGES), format_func=lambda v: LANGUAGES[v], key="new_user_language")
            submitted = st.form_submit_button(t('Toevoegen'))
            if submitted:
                if not new_full_name or not new_short_name or not new_password:
                    st.error(t('Volledige naam, korte naam en wachtwoord zijn verplicht.'))
                else:
                    try:
                        create_user(new_full_name, new_short_name.strip(), new_email, new_password, language=new_language)
                        st.success(t("Gebruiker '{0}' toegevoegd.", new_full_name))
                        st.rerun()
                    except Exception as e:
                        st.error(t('Kon gebruiker niet toevoegen (bestaat de korte naam al?): {0}', e))

if users:
    if is_admin:
        editable_users = users
    else:
        editable_users = [u for u in users if u["short_name"] == my_short_name]

    if editable_users:
        with st.expander(t("✏️ Gebruiker bewerken of verwijderen") if is_admin else t("✏️ Gebruiker bewerken")):
            user_options = {f"{u['full_name']} ({u['short_name']})": u for u in editable_users}
            chosen_label = st.selectbox(t('Kies een gebruiker'), list(user_options.keys()), key="edit_user_select")
            chosen_user = user_options[chosen_label]

            with st.form(f"edit_user_form_{chosen_user['id']}"):
                edit_full_name = st.text_input(t('Volledige naam'), value=chosen_user["full_name"])
                edit_short_name = st.text_input(t('Korte naam'), value=chosen_user["short_name"])
                edit_email = st.text_input(t('E-mailadres'), value=chosen_user["email"] or "")
                edit_password = st.text_input(
                    t('Wachtwoord (klik het oogje om te tonen; pas aan om te wijzigen)'),
                    value=chosen_user.get("password_plain") or "",
                    type="password",
                )
                _lang_now = chosen_user.get("language") or "nl"
                edit_language = st.selectbox(
                    t('Taal'),
                    list(LANGUAGES),
                    index=list(LANGUAGES).index(_lang_now) if _lang_now in LANGUAGES else 0,
                    format_func=lambda v: LANGUAGES[v],
                    key=f"edit_user_language_{chosen_user['id']}",
                )
                if is_admin:
                    ROLE_OPTIONS = ["gebruiker", "beheerder"]
                    current_role = chosen_user.get("role") or "gebruiker"
                    role_index = ROLE_OPTIONS.index(current_role) if current_role in ROLE_OPTIONS else 0
                    edit_role = st.selectbox(t('Rol'), ROLE_OPTIONS, index=role_index)
                else:
                    edit_role = chosen_user.get("role") or "gebruiker"
                    st.text_input(t('Rol'), value=edit_role, disabled=True)
                if is_admin:
                    col_save, col_delete = st.columns(2)
                    with col_save:
                        save_clicked = st.form_submit_button(t('Opslaan'), type="primary")
                    with col_delete:
                        delete_clicked = st.form_submit_button(t('Verwijderen'), type="secondary")
                else:
                    save_clicked = st.form_submit_button(t('Opslaan'), type="primary")
                    delete_clicked = False

            if save_clicked:
                update_user(
                    chosen_user["id"],
                    edit_full_name,
                    edit_short_name.strip(),
                    edit_email,
                    new_password=edit_password if edit_password else None,
                    role=edit_role if is_admin else None,
                    language=edit_language,
                )
                st.success(t('Opgeslagen.'))
                st.rerun()

            if delete_clicked:
                delete_user(chosen_user["id"])
                st.success(t("Gebruiker '{0}' verwijderd.", chosen_user['full_name']))
                st.rerun()


# ---------- Boeken toegevoegd: ieders eigen grafiek (niemand ziet die van een ander) ----------
my_user = next((u for u in users if u["short_name"] == my_short_name), None) if users else None
if my_user or (is_admin and users):
    st.divider()
    st.subheader(t('Boeken toegevoegd'))
    periode = st.radio(t('Per'), ["Dag", "Week", "Maand"], horizontal=True, key="added_period", format_func=t)

    log_rows = get_books_added_log()
    log_df = pd.DataFrame([{"user": r["user_short_name"], "tijdstip": r["tijdstip"]} for r in log_rows], columns=["user", "tijdstip"])
    log_df["tijdstip"] = pd.to_datetime(log_df["tijdstip"])

    nu = pd.Timestamp.now(tz="Europe/Amsterdam").tz_localize(None).normalize()
    if periode == "Dag":
        freq, aantal = "D", 30
        start = nu - pd.Timedelta(days=aantal - 1)
        log_df["periode"] = log_df["tijdstip"].dt.normalize()
        toelichting = "laatste 30 dagen"
    elif periode == "Week":
        freq, aantal = "W-MON", 12
        start = (nu - pd.Timedelta(days=nu.weekday())) - pd.Timedelta(weeks=aantal - 1)
        log_df["periode"] = log_df["tijdstip"].dt.normalize() - pd.to_timedelta(log_df["tijdstip"].dt.weekday, unit="D")
        toelichting = "laatste 12 weken (per maandag)"
    else:
        freq, aantal = "MS", 12
        start = nu.replace(day=1) - pd.DateOffset(months=aantal - 1)
        log_df["periode"] = log_df["tijdstip"].dt.to_period("M").dt.to_timestamp()
        toelichting = "laatste 12 maanden"
    index = pd.date_range(start, nu, freq="D" if periode == "Dag" else ("7D" if periode == "Week" else "MS"))
    if is_admin:
        _blues = ["#0B3C8C", "#83C9FF", "#1F77D0", "#4FA3E8", "#06245A", "#A9D8FF", "#2E5E9E", "#5BB4F0"]
        st.caption(t('Aantal toegevoegde boeken per gebruiker, {0}. Alleen de beheerder ziet deze grafiek.', toelichting))
        _cols = {}
        # iedereen die ooit iets heeft ingevoerd, ook oud-gebruikers; wie nooit invoert (zoals Lem) blijft weg
        _inputters = sorted(log_df["user"].dropna().unique(), key=str.lower)
        for u in _inputters:
            mine = log_df[log_df["user"] == u]
            _cols[u] = mine.groupby("periode").size().reindex(index, fill_value=0)
        _df = pd.DataFrame(_cols, index=index)
        if _df.empty or not len(_df.columns):
            st.info(t('Nog geen boeken toegevoegd.'))
            _inputters = []
        else:
            # vaste kleur per persoon (op voornaam in de gebruikerslijst); anderen krijgen de overige tinten
            _vast = {"bart": "#83C9FF", "lem": "#0B3C8C", "spike": "#1F77D0", "jules": "#4FA3E8"}
            _by_full = {u["short_name"]: (u.get("full_name") or "").lower() for u in users}
            _vrij = [c for c in _blues if c not in _vast.values()]
            _kleuren = []
            for c in _df.columns:
                _eerste = (_by_full.get(c) or c).lower().split(" ")[0]
                _kleuren.append(_vast.get(_eerste) or (_vrij.pop(0) if _vrij else "#7F7F7F"))
            st.line_chart(_df, height=260, color=_kleuren)
        _tot = ", ".join(f"{u}: {int(_df[u].sum())}" for u in _inputters)
        if _inputters:
            st.caption(t('Totaal in deze periode') + ": " + _tot)
    else:
        st.caption(t('Aantal toegevoegde boeken, {0}. Alleen jij kan deze grafiek zien.', toelichting))
        for u in [my_user]:
            mine = log_df[log_df["user"] == u["short_name"]]
            counts = mine.groupby("periode").size().reindex(index, fill_value=0)
            totaal = int(counts.sum())
            st.markdown(t('**{0}** ({1}) — {2} in deze periode', u['full_name'], u['short_name'], totaal))
            st.line_chart(counts.rename("Boeken"), height=220, color="#83C9FF")

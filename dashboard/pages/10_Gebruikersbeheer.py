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
    delete_todo_extra,
    create_user,
    update_user,
    delete_user,
    format_datetime_nl,
    na,
)

st.set_page_config(page_title="Gebruikersbeheer", page_icon="🔑", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
current_user = require_login()
st.title("🔑 Gebruikersbeheer")

is_admin = (current_user is None) or (current_user.get("role") == "beheerder")
my_short_name = current_user["short_name"] if current_user else None

users = get_users_with_stats()

st.subheader("Overzicht")
if users:
    df = pd.DataFrame(users)
    df["last_login_at"] = df["last_login_at"].apply(format_datetime_nl)
    df["last_location"] = df["last_location"].apply(na)
    df["email"] = df["email"].apply(na)

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
            "Laatst ingelogd",
            "Laatste locatie",
            "Ingevoerd",
            "Laatste 7 dagen",
        ]
    ]
    if not is_admin:   # privacy: alleen de beheerder ziet de aantallen
        df = df.drop(columns=["Ingevoerd", "Laatste 7 dagen"])
    st.dataframe(df, width="stretch")
else:
    st.info("Nog geen gebruikers aangemaakt.")

st.divider()

if is_admin:
    with st.expander("➕ Nieuwe gebruiker toevoegen"):
        with st.form("new_user_form"):
            new_full_name = st.text_input("Volledige naam")
            new_short_name = st.text_input("Korte naam (= gebruikersnaam om in te loggen)")
            new_email = st.text_input("E-mailadres")
            new_password = st.text_input("Wachtwoord", type="password")
            submitted = st.form_submit_button("Toevoegen")
            if submitted:
                if not new_full_name or not new_short_name or not new_password:
                    st.error("Volledige naam, korte naam en wachtwoord zijn verplicht.")
                else:
                    try:
                        create_user(new_full_name, new_short_name.strip(), new_email, new_password)
                        st.success(f"Gebruiker '{new_full_name}' toegevoegd.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Kon gebruiker niet toevoegen (bestaat de korte naam al?): {e}")

if users:
    if is_admin:
        editable_users = users
    else:
        editable_users = [u for u in users if u["short_name"] == my_short_name]

    if editable_users:
        with st.expander("✏️ Gebruiker bewerken" + (" of verwijderen" if is_admin else "")):
            user_options = {f"{u['full_name']} ({u['short_name']})": u for u in editable_users}
            chosen_label = st.selectbox("Kies een gebruiker", list(user_options.keys()), key="edit_user_select")
            chosen_user = user_options[chosen_label]

            with st.form(f"edit_user_form_{chosen_user['id']}"):
                edit_full_name = st.text_input("Volledige naam", value=chosen_user["full_name"])
                edit_short_name = st.text_input("Korte naam", value=chosen_user["short_name"])
                edit_email = st.text_input("E-mailadres", value=chosen_user["email"] or "")
                edit_password = st.text_input(
                    "Wachtwoord (klik het oogje om te tonen; pas aan om te wijzigen)",
                    value=chosen_user.get("password_plain") or "",
                    type="password",
                )
                if is_admin:
                    ROLE_OPTIONS = ["gebruiker", "beheerder"]
                    current_role = chosen_user.get("role") or "gebruiker"
                    role_index = ROLE_OPTIONS.index(current_role) if current_role in ROLE_OPTIONS else 0
                    edit_role = st.selectbox("Rol", ROLE_OPTIONS, index=role_index)
                else:
                    edit_role = chosen_user.get("role") or "gebruiker"
                    st.text_input("Rol", value=edit_role, disabled=True)
                if is_admin:
                    col_save, col_delete = st.columns(2)
                    with col_save:
                        save_clicked = st.form_submit_button("Opslaan")
                    with col_delete:
                        delete_clicked = st.form_submit_button("Verwijderen", type="secondary")
                else:
                    save_clicked = st.form_submit_button("Opslaan")
                    delete_clicked = False

            if save_clicked:
                update_user(
                    chosen_user["id"],
                    edit_full_name,
                    edit_short_name.strip(),
                    edit_email,
                    new_password=edit_password if edit_password else None,
                    role=edit_role if is_admin else None,
                )
                st.success("Opgeslagen.")
                st.rerun()

            if delete_clicked:
                delete_user(chosen_user["id"])
                st.success(f"Gebruiker '{chosen_user['full_name']}' verwijderd.")
                st.rerun()


# ---------- Boeken toegevoegd: ieders eigen grafiek (niemand ziet die van een ander) ----------
my_user = next((u for u in users if u["short_name"] == my_short_name), None) if users else None
if my_user:
    st.divider()
    st.subheader("Boeken toegevoegd")
    periode = st.radio("Per", ["Dag", "Week", "Maand"], horizontal=True, key="added_period")

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
    st.caption(f"Aantal toegevoegde boeken, {toelichting}. Alleen jij kan deze grafiek zien.")

    for u in [my_user]:
        mine = log_df[log_df["user"] == u["short_name"]]
        counts = mine.groupby("periode").size().reindex(index, fill_value=0)
        totaal = int(counts.sum())
        st.markdown(f"**{u['full_name']}** ({u['short_name']}) — {totaal} in deze periode")
        st.line_chart(counts.rename("Boeken"), height=220, color="#83C9FF")


# ---------- Stokken achter de deur: handmatige taken voor de dagelijkse to-do-mail ----------
import datetime as _dt

st.divider()
st.subheader("Stokken achter de deur")
st.write(
    'Elke ochtend om 7:15 krijgt de beheerder een e-mail met taken die op die dag gedaan moeten worden. '
    'Een aantal taken liggen vast, zoals pakjes inpakken. Maar soms zijn er taken die het ook waard zijn om de beheerder '
    'op het hart te drukken. Dat zijn de "stokken achter de deur" die je hier kunt toevoegen.'
)
_flash = st.session_state.pop("todo_extra_flash", None)
if _flash:
    st.success(_flash)

_vandaag = _dt.datetime.now(ZoneInfo("Europe/Amsterdam")).date()
_dagen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
with st.form("todo_extra_form", clear_on_submit=True):
    c1, c2 = st.columns([1, 4])
    with c1:
        extra_date = st.date_input("Datum", value=_vandaag + _dt.timedelta(days=1), min_value=_vandaag, format="DD-MM-YYYY")
    with c2:
        extra_text = st.text_input("Taak (een korte regel)", max_chars=200)
    if st.form_submit_button("Toevoegen"):
        if not extra_text.strip():
            st.error("Vul een tekst in.")
        else:
            add_todo_extra(extra_date, extra_text, my_short_name)
            st.session_state["todo_extra_flash"] = f"Toegevoegd voor {_dagen[extra_date.weekday()]} {extra_date.strftime('%d-%m-%Y')}."
            st.rerun()

_extras = get_todo_extras()
if _extras:
    st.caption("Klaar om meegestuurd te worden, eerstvolgende bovenaan. Na het versturen verdwijnt een taak vanzelf uit de lijst.")
    for e in _extras:
        d = e["due_date"]
        c1, c2, c3 = st.columns([2, 10, 1])
        c1.write(f"{_dagen[d.weekday()]} {d.strftime('%d-%m-%Y')}" + (" (gaat mee met de eerstvolgende mail)" if d < _vandaag else ""))
        c2.write(e["text"])
        if c3.button("🗑️", key=f"del_extra_{e['id']}", help="Verwijderen"):
            delete_todo_extra(e["id"])
            st.rerun()
else:
    st.info("Er staan geen taken klaar.")

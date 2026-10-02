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

import pandas as pd
import streamlit as st

from common import (
    render_logo,
    require_login,
    get_users_with_stats,
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
            "aantal_toegevoegd": "Boeken toegevoegd",
            "aantal_aangepast": "Boeken aangepast",
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
            "Boeken toegevoegd",
            "Boeken aangepast",
        ]
    ]
    st.dataframe(df, use_container_width=True)
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

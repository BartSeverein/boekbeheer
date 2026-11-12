"""
pages/13_Voorkanten.py — De voorkant van elk boek nakijken en zo nodig een andere foto als voorkant kiezen voor de webshop en
Bol. Bij Boekwinkeltjes ligt de eerste foto vast; deze keuze staat daar los van en verandert daar niets.
"""

import html
from pathlib import Path

import streamlit as st

from common import (
    render_logo,
    require_login,
    get_front_review_books,
    set_front_image,
    mark_front_checked,
    _has_front_column,
)

st.set_page_config(page_title="Voorkanten", page_icon="🖼️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🖼️ Voorkanten")

st.caption(
    "Kijk per boek of de foto met ⭐ de echte voorkant is. Klopt het, klik dan op '✔ Klopt'. Klopt het niet, klik dan op 'Kies' "
    "onder de foto die de voorkant moet zijn. De keuze geldt voor de webshop en voor Bol; bij Boekwinkeltjes verandert niets. "
    "Alleen boeken op voorraad met minstens twee foto's staan hier. De webshop toont de nieuwe voorkant na de volgende volledige run."
)

if not _has_front_column():
    st.error("De database mist nog de kolommen voor de voorkantkeuze. Voer eerst voorkant_keuze.sql uit in de Supabase SQL Editor "
             "en herstart daarna de app.")
    st.stop()

PER_PAGE = 15
PER_ROW = 8

f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    view = st.radio("Toon", ["Nog na te kijken", "Alles"], horizontal=True, key="fronts_view")
with f2:
    only_bol = st.checkbox("Alleen boeken die op Bol staan", key="fronts_only_bol")
with f3:
    search = st.text_input("Zoek op titel, auteur of boek-id", key="fronts_search")
only_unchecked = view == "Nog na te kijken"

# Bij 'Nog na te kijken' verdwijnt een boek uit de lijst zodra het is nagekeken: dan altijd de eerste boeken tonen.
page_key = f"fronts_page_{view}_{only_bol}_{search}"
page = 0 if only_unchecked else int(st.session_state.get(page_key, 0))
books, total = get_front_review_books(only_unchecked=only_unchecked, only_bol=only_bol, search=search,
                                      limit=PER_PAGE, offset=page * PER_PAGE)

_msg = st.session_state.pop("fronts_msg", None)
if _msg:
    st.warning(_msg)

if not books:
    st.success("Niets meer na te kijken." if only_unchecked else "Geen boeken gevonden.")
    st.stop()

if only_unchecked:
    st.markdown(f"**Nog {total} {'boek' if total == 1 else 'boeken'} na te kijken.**")
else:
    pages = (total - 1) // PER_PAGE + 1
    n1, n2, n3 = st.columns([1, 3, 1])
    with n1:
        if st.button("← Vorige", disabled=page == 0, use_container_width=True):
            st.session_state[page_key] = page - 1
            st.rerun()
    with n2:
        st.markdown(f"<div style='text-align:center;'>Pagina {page + 1} van {pages} ({total} boeken)</div>", unsafe_allow_html=True)
    with n3:
        if st.button("Volgende →", disabled=page + 1 >= pages, use_container_width=True):
            st.session_state[page_key] = page + 1
            st.rerun()


def _act(ok_msg):
    ok, msg = ok_msg
    if not ok:
        st.session_state["fronts_msg"] = msg
    st.rerun()


for book in books:
    st.divider()
    images = book["images"]
    chosen = next((im for im in images if im["is_front"]), None)
    front_id = (chosen or images[0])["image_id"]
    h1, h2, h3 = st.columns([6, 1, 1])
    with h1:
        badges = ("  ·  op Bol" if book["on_bol"] else "") + ("  ·  ✔ nagekeken" if book["checked"] else "") \
                 + ("  ·  eigen voorkant gekozen" if chosen else "")
        st.markdown(f"**{html.escape(book['title'] or '(geen titel)')}**  \n"
                    f"{html.escape(book['author'] or '')}  ·  boek {book['id']}  ·  {len(images)} foto's{badges}")
    with h2:
        if st.button("✔ Klopt", key=f"fronts_ok_{book['id']}", type="primary", use_container_width=True,
                     disabled=book["checked"] and not only_unchecked):
            _act(mark_front_checked(book["id"]))
    with h3:
        if st.button("Openen", key=f"fronts_open_{book['id']}", use_container_width=True):
            st.session_state["preselect_book_id"] = int(book["id"])
            st.switch_page("pages/3_Boekdetails.py")
    for start in range(0, len(images), PER_ROW):
        cols = st.columns(PER_ROW)
        for col, im in zip(cols, images[start:start + PER_ROW]):
            with col:
                is_front = im["image_id"] == front_id
                border = "3px solid #2e9e4f" if is_front else "3px solid transparent"
                if im["src"]:
                    st.markdown(f'<img src="{im["src"]}" loading="lazy" style="height:150px; width:auto; max-width:100%; '
                                f'object-fit:contain; border:{border}; border-radius:4px;" />', unsafe_allow_html=True)
                else:
                    st.caption("(geen voorbeeld)")
                if is_front:
                    st.markdown("<div style='text-align:center;'>⭐ Voorkant</div>", unsafe_allow_html=True)
                    if chosen and st.button("Keuze weghalen", key=f"fronts_clear_{book['id']}", use_container_width=True,
                                            help="Weer de eerste foto als voorkant."):
                        _act(set_front_image(book["id"], None))
                elif st.button("Kies", key=f"fronts_pick_{book['id']}_{im['image_id']}", use_container_width=True):
                    _act(set_front_image(book["id"], im["image_id"]))

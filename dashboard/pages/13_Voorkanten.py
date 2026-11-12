"""
pages/13_Voorkanten.py — Per boek nakijken welke foto's de webshop (en Bol) tonen, in twee rondes:
  - Voorkant: welke foto is de voorkant voor de webshop en Bol;
  - Extra foto's webshop: welke (hoogstens twee) foto's naast de voorkant op de webshop komen.
Bij Boekwinkeltjes ligt de eerste foto vast; deze keuzes staan daar los van en veranderen daar niets.

Snelheid: de lijst wordt één keer per blad geladen en in de sessie bewaard. Elk boek is een eigen 'fragment': een klik
ververst alleen dat ene boek en doet één schrijfactie in de database, zonder de hele pagina opnieuw op te bouwen.
"""

import html
from pathlib import Path

import streamlit as st

from common import (
    render_logo,
    require_login,
    get_front_review_books,
    set_front_image,
    set_web_extra_quick,
    mark_front_checked,
    mark_fronts_checked,
    delete_book_image,
    get_all_images,
    _has_front_column,
    MAX_WEB_EXTRA,
)

st.set_page_config(page_title="Voorkanten", page_icon="🖼️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("🖼️ Voorkanten en webshopfoto's")

if not _has_front_column():
    st.error("De database mist nog de kolommen voor de voorkantkeuze. Voer eerst voorkant_keuze.sql uit in de Supabase SQL Editor "
             "en herstart daarna de app.")
    st.stop()

PER_PAGE = 15
PER_ROW = 8

round_label = st.radio("Wat kijk je na?", ["Voorkant", "Extra foto's webshop"], horizontal=True, key="fronts_round")
mode = "front" if round_label == "Voorkant" else "extra"

if mode == "front":
    st.caption(
        "Klik alleen bij boeken waar iets mis is: 'Kies' onder de foto die de voorkant moet zijn (⭐ is de huidige), of 🗑️ om een foto "
        "weg te halen. Klopt de rest, klik dan onderaan op 'Rest klopt, volgende'. De keuze geldt voor de webshop en voor Bol; bij "
        "Boekwinkeltjes verandert niets. Alleen boeken op voorraad met minstens twee foto's staan hier. De webshop toont de "
        "wijzigingen na de volgende volledige run."
    )
else:
    st.caption(
        f"De webshop toont de voorkant plus hoogstens {MAX_WEB_EXTRA} extra foto's; de blauwe rand laat zien welke dat nu zijn. "
        "Zonder eigen keuze zijn het de eerste twee na de voorkant. Klik op 'Webshop' onder een foto om zelf te kiezen (nog eens "
        "klikken haalt de keuze weg); zodra je er één kiest, gelden alleen nog je eigen keuzes. Klopt de rest, klik dan onderaan op "
        "'Rest klopt, volgende'. Alleen boeken op voorraad met minstens vier foto's staan hier: met minder staan ze toch allemaal "
        "op de webshop. Bol krijgt altijd alle foto's. De webshop toont de wijzigingen na de volgende volledige run."
    )

f1, f2, f3 = st.columns([2, 2, 3])
with f1:
    view = st.radio("Toon", ["Nog na te kijken", "Alles"], horizontal=True, key="fronts_view")
with f2:
    only_bol = st.checkbox("Alleen boeken die op Bol staan", key="fronts_only_bol")
with f3:
    search = st.text_input("Zoek op titel, auteur of boek-id", key="fronts_search")
only_unchecked = view == "Nog na te kijken"

# Het blad wordt alleen opnieuw geladen bij een ander filter, een ander blad of na 'volgende'.
page = 0 if only_unchecked else int(st.session_state.get("fronts_page", 0))
batch_key = (mode, view, only_bol, search, page)
batch = st.session_state.get("fronts_batch")
if not batch or batch["key"] != batch_key or st.session_state.pop("fronts_reload", False):
    books, total = get_front_review_books(only_unchecked=only_unchecked, only_bol=only_bol, search=search,
                                          limit=PER_PAGE, offset=page * PER_PAGE, mode=mode)
    for b in books:
        b["done"] = False       # in deze sessie afgehandeld
    batch = {"key": batch_key, "books": {b["id"]: b for b in books}, "order": [b["id"] for b in books], "total": total}
    st.session_state["fronts_batch"] = batch

if not batch["order"]:
    st.success("Niets meer na te kijken." if only_unchecked else "Geen boeken gevonden.")
    st.stop()

total = batch["total"]
if only_unchecked:
    st.markdown(f"**Nog {total} {'boek' if total == 1 else 'boeken'} na te kijken** (bij het laden van dit blad).")
else:
    pages = (total - 1) // PER_PAGE + 1
    n1, n2, n3 = st.columns([1, 3, 1])
    with n1:
        if st.button("← Vorige", disabled=page == 0, use_container_width=True):
            st.session_state["fronts_page"] = page - 1
            st.rerun()
    with n2:
        st.markdown(f"<div style='text-align:center;'>Blad {page + 1} van {pages} ({total} boeken)</div>", unsafe_allow_html=True)
    with n3:
        if st.button("Volgende →", disabled=page + 1 >= pages, use_container_width=True):
            st.session_state["fronts_page"] = page + 1
            st.rerun()


def _img(src, border):
    if src:
        st.markdown(f'<img src="{src}" loading="lazy" style="height:150px; width:auto; max-width:100%; object-fit:contain; '
                    f'border:{border}; border-radius:4px;" />', unsafe_allow_html=True)
    else:
        st.caption("(geen voorbeeld)")


@st.fragment
def book_card(book_id):
    book = st.session_state["fronts_batch"]["books"][book_id]
    images = book["images"]
    title = html.escape(book["title"] or "(geen titel)")
    if book["done"]:
        c1, c2 = st.columns([8, 1])
        with c1:
            st.markdown(f"✔ <span style='opacity:0.6;'>{title}  ·  boek {book['id']}  ·  {book.get('done_text', 'nagekeken')}</span>",
                        unsafe_allow_html=True)
        with c2:
            if st.button("Terug", key=f"fronts_undo_{book_id}", use_container_width=True, help="Dit boek opnieuw bekijken."):
                book["done"] = False
                st.rerun(scope="fragment")
        return
    chosen = next((im for im in images if im["is_front"]), None)
    front_id = (chosen or images[0])["image_id"] if images else None
    # Zoals de webshop: de voorkant eerst, daarna de rest in de volgorde van Boekwinkeltjes.
    ordered = sorted(images, key=lambda im: im["image_id"] != front_id)
    own_extras = [im["image_id"] for im in ordered[1:] if im["web_extra"]][:MAX_WEB_EXTRA]
    on_web = set(own_extras) if own_extras else {im["image_id"] for im in ordered[1:1 + MAX_WEB_EXTRA]}
    h1, h2, h3 = st.columns([6, 1, 1])
    with h1:
        badges = ("  ·  op Bol" if book["on_bol"] else "") + ("  ·  ✔ eerder nagekeken" if book["checked"] else "")
        if mode == "front":
            badges += "  ·  eigen voorkant gekozen" if chosen else ""
        else:
            badges += f"  ·  eigen keuze: {len(own_extras)} extra" if own_extras else "  ·  geen eigen keuze (eerste twee na de voorkant)"
        st.markdown(f"**{title}**  \n{html.escape(book['author'] or '')}  ·  boek {book['id']}  ·  {len(images)} foto's{badges}")
    with h2:
        if st.button("✔ Klopt", key=f"fronts_ok_{book_id}", type="primary", use_container_width=True):
            mark_front_checked(book_id, mode=mode)
            book["done"], book["checked"], book["done_text"] = True, True, "nagekeken"
            st.rerun(scope="fragment")
    with h3:
        if st.button("Openen", key=f"fronts_open_{book_id}", use_container_width=True):
            st.session_state["preselect_book_id"] = int(book_id)
            st.switch_page("pages/3_Boekdetails.py")
    warning = book.pop("warning", None)
    if warning:
        st.warning(warning)
    shown = images if mode == "front" else ordered
    for start in range(0, len(shown), PER_ROW):
        cols = st.columns(PER_ROW)
        for col, im in zip(cols, shown[start:start + PER_ROW]):
            with col:
                is_front = im["image_id"] == front_id
                if mode == "front":
                    _img(im["src"], "3px solid #2e9e4f" if is_front else "3px solid transparent")
                    if is_front:
                        st.markdown("<div style='text-align:center;'>⭐ Voorkant</div>", unsafe_allow_html=True)
                        if chosen and st.button("Keuze weghalen", key=f"fronts_clear_{book_id}", use_container_width=True,
                                                help="Weer de eerste foto als voorkant."):
                            set_front_image(book_id, None)
                            for other in images:
                                other["is_front"] = False
                            st.rerun(scope="fragment")
                    elif st.button("Kies", key=f"fronts_pick_{book_id}_{im['image_id']}", use_container_width=True):
                        set_front_image(book_id, im["image_id"])
                        for other in images:
                            other["is_front"] = other["image_id"] == im["image_id"]
                        book["done"], book["checked"], book["done_text"] = True, True, "voorkant gekozen"
                        st.rerun(scope="fragment")
                else:
                    is_on_web = im["image_id"] in on_web
                    _img(im["src"], "3px solid #2e9e4f" if is_front else ("3px solid #0068C9" if is_on_web else "3px solid transparent"))
                    if is_front:
                        st.markdown("<div style='text-align:center;'>⭐ Voorkant</div>", unsafe_allow_html=True)
                    else:
                        picked = im["image_id"] in own_extras
                        if st.button("✔ Webshop" if picked else "Webshop", key=f"fronts_web_{book_id}_{im['image_id']}",
                                     type="primary" if picked else "secondary", use_container_width=True):
                            ok, msg = set_web_extra_quick(book_id, im["image_id"], not picked)
                            if ok:
                                im["web_extra"] = not picked
                            else:
                                book["warning"] = msg
                            st.rerun(scope="fragment")
                if st.button("🗑️", key=f"fronts_del_{book_id}_{im['image_id']}", use_container_width=True,
                             help="Deze foto meteen verwijderen, net als de prullenbak op Boekdetails."):
                    delete_book_image(book_id, "confirmed", im["image_id"])
                    get_all_images.clear()
                    book["images"] = [x for x in images if x["image_id"] != im["image_id"]]
                    st.rerun(scope="fragment")


for book_id in batch["order"]:
    st.divider()
    book_card(book_id)

st.divider()
if only_unchecked:
    if st.button("✔ Rest klopt, volgende →", type="primary", use_container_width=True,
                 help="Markeert alle boeken op dit blad die nog open staan als nagekeken en laadt de volgende."):
        mark_fronts_checked([i for i in batch["order"] if not batch["books"][i]["done"]], mode=mode)
        st.session_state["fronts_reload"] = True
        st.rerun()

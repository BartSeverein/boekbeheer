"""
kaarten_ui.py — Het invulformulier voor een ansichtkaart, gedeeld door Nieuwe kaart en Kaartdetails.
"""

import streamlit as st

import kaarten_db as K


def velden_formulier(prefix, kaart=None):
    """
    Tekent de velden van een kaart. 'kaart' is None (nieuwe kaart) of de rij uit get_kaart().
    Geeft (velden, themas, fout) terug; 'fout' is None of een tekst die vertelt wat er nog mist.
    De widgets hebben sleutels met 'prefix' ervoor, zodat twee kaarten elkaar niet storen.
    """
    kaart = kaart or {}

    def key(naam):
        return f"{prefix}_{naam}"

    titel = st.text_input("Titel", value=kaart.get("titel") or "", key=key("titel"),
                          placeholder="bijvoorbeeld: Ede, Hoogstraat")

    provincies = K.get_provincies()
    prov_opties = [""] + provincies
    prov_start = kaart.get("provincie") if kaart.get("provincie") in provincies else ""
    provincie = st.selectbox("Provincie", prov_opties, index=prov_opties.index(prov_start), key=key("provincie"),
                             format_func=lambda p: p or "(kies een provincie)")

    plaats_naam = None
    if provincie:
        plaatsen = K.get_plaatsen(provincie)
        namen = [n for _, n in plaatsen]
        start_naam = next((n for i, n in plaatsen if i == kaart.get("plaats_id")), None)
        opties = [""] + namen
        # Per provincie een eigen sleutel: wissel je van provincie, dan begint de plaatsenlijst weer leeg.
        plaats_naam = st.selectbox("Plaats", opties, index=opties.index(start_naam) if start_naam else 0,
                                   key=key(f"plaats_{provincie}"), accept_new_options=True,
                                   format_func=lambda n: n or "(kies of typ een plaats)")
        if plaats_naam and plaats_naam.strip().lower() not in {n.lower() for n in namen}:
            st.caption(f"'{plaats_naam.strip()}' is nieuw en wordt bij het opslaan toegevoegd aan {provincie} (tenzij het een bekende schrijfwijze is).")
    else:
        st.selectbox("Plaats", ["(eerst een provincie kiezen)"], disabled=True, key=key("plaats_leeg"))

    themas_alle = K.get_themas()
    themas = st.multiselect("Thema's", themas_alle, default=[t for t in kaart.get("themas", []) if t in themas_alle],
                            key=key("themas"), help="Het eerste thema dat je kiest is het hoofdthema.")

    uitgevers = K.get_uitgevers()
    uids = [None] + [i for i, _ in uitgevers]
    ulabels = {i: l for i, l in uitgevers}
    ustart = kaart.get("uitgever_id") if kaart.get("uitgever_id") in ulabels else None
    uitgever_id = st.selectbox("Uitgever", uids, index=uids.index(ustart), key=key("uitgever"),
                               format_func=lambda i: ulabels.get(i, "(onbekend)"))

    col_j, col_p = st.columns(2)
    jaar = col_j.number_input("Jaartal van uitgave", min_value=1800, max_value=2100, step=1,
                              value=kaart.get("jaar_uitgave"), key=key("jaar"))
    perioden = [""] + K.get_perioden()
    p_start = kaart.get("periode") or K.periode_voor_jaar(int(jaar) if jaar else None) or ""
    periode = col_p.selectbox("Periode", perioden, index=perioden.index(p_start) if p_start in perioden else 0,
                              key=key(f"periode_{jaar or 0}"), format_func=lambda p: p or "(onbekend)",
                              help="Volgt het jaartal zodra je dat invult; je kunt het ook zelf kiezen.")

    col1, col2, col3 = st.columns(3)
    beschreven = col1.checkbox("Beschreven", value=kaart.get("beschreven", True), key=key("beschreven"))
    gelopen = col2.checkbox("Gelopen", value=kaart.get("gelopen", True), key=key("gelopen"))
    stempel = col3.checkbox("Stempeldoordruk", value=kaart.get("stempeldoordruk", False), key=key("stempel"))

    kleur_start = kaart.get("kleur") if kaart.get("kleur") in K.KLEUREN else K.KLEUREN[0]   # zwart/wit is de standaard
    kleur = st.radio("Kleur?", K.KLEUREN, index=K.KLEUREN.index(kleur_start), horizontal=True, key=key("kleur"))

    col_pr, col_v = st.columns(2)
    prijs = col_pr.number_input("Prijs (€)", min_value=0.0, step=0.25, format="%.2f",
                                value=float(kaart["prijs"]) if kaart.get("prijs") is not None else 0.0, key=key("prijs"))
    amount = col_v.number_input("Voorraad", min_value=0, step=1, value=int(kaart.get("amount", 1)), key=key("amount"))
    opmerking = st.text_area("Opmerking", value=kaart.get("opmerking") or "", key=key("opmerking"), height=80)

    fout = None
    if prijs <= 0:   # provincie en plaats zijn niet verplicht: een kaart (klederdracht bijvoorbeeld) hoeft niet plaatsgebonden te zijn
        fout = "Vul een prijs in."

    velden = {
        "titel": titel.strip() or None, "provincie": provincie or None, "plaats_id": None, "_plaats_naam": (plaats_naam or "").strip() or None, "uitgever_id": uitgever_id,
        "periode": periode or None, "jaar_uitgave": int(jaar) if jaar else None, "beschreven": beschreven, "gelopen": gelopen,
        "stempeldoordruk": stempel, "kleur": kleur, "prijs": prijs or None, "amount": int(amount),
        "locatie": None, "opmerking": opmerking.strip() or None,
    }
    return velden, themas, fout

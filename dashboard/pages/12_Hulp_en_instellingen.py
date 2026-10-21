"""
pages/12_Hulp_en_instellingen.py — Architectuurschema, handleiding-download,
supportgegevens en de instellingen voor het druppelsysteem.
"""

import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from dropbox_files import fetch_help_file
from common import (
    _get_secret,
    active_daily_codes,
    AMSTERDAM_TZ,
    delete_placeholder_uploads,
    find_placeholder_uploads,
    get_all_images,
    GITHUB_TOKEN_NEVER,
    get_github_token_expiry,
    github_token_banner,
    render_logo,
    require_login,
    get_setting,
    set_github_token_expiry,
    set_setting,
    get_shipping_costs,
)

st.set_page_config(page_title="Hulp en instellingen", page_icon="ℹ️", layout="wide")
render_logo(Path(__file__).parent.parent / "assets" / "logo_mail.png")
require_login()
st.title("ℹ️ Hulp en instellingen")

# ---------- Info & help ----------

@st.cache_data(ttl=600, show_spinner=False)
def _help_file(filename):
    """Een bestand uit de Dropbox-map 'handleiding' (alleen opgehaald voor ingelogde gebruikers). (bytes, fout)."""
    return fetch_help_file(
        filename,
        _get_secret("DROPBOX_APP_KEY"),
        _get_secret("DROPBOX_APP_SECRET"),
        _get_secret("DROPBOX_REFRESH_TOKEN"),
    )


st.subheader("Architectuur")
architecture_bytes, architecture_error = _help_file("Architectuur.png")
if architecture_bytes:
    st.image(architecture_bytes, width="stretch")
else:
    st.info(f"Het architectuurschema kon niet worden getoond: {architecture_error}")

st.divider()

manual_bytes, manual_error = _help_file("boekbeheer.pdf")
if manual_bytes:
    st.download_button(
        f"Download handleiding (PDF, {round(len(manual_bytes) / 1024)} Kb)",
        data=manual_bytes,
        file_name="boekbeheer.pdf",
        mime="application/pdf",
    )
else:
    st.info(f"De handleiding kon niet worden opgehaald: {manual_error}")

st.markdown(
    "**Support:** Bart, 06-20539792 of [klik hier voor Whatsapp](https://wa.me/0031620539792)"
)

st.divider()
st.markdown("Het Boekbeheersysteem is © 2026, Severein Services")

# ---------- Druppelsysteem Boekwinkeltjes ----------

st.divider()
st.header("Druppelsysteem Boekwinkeltjes")
st.caption(
    "Boeken worden druppelsgewijs aan Boekwinkeltjes toegevoegd, voor optimale "
    "zichtbaarheid en verkoopkansen. Dat gaat in drie drukke bezoekperioden: de "
    "lunchpauze, einde werktijd en de avond. Kies hieronder voor elke dag van de "
    "week wat die perioden zijn, en om de hoeveel minuten een boek moet worden "
    "toegevoegd. Laat een blokje leeg als er in die periode niet gedruppeld mag worden."
)

# Het schema staat als JSON onder 'bw_drip_schedule' in app_settings:
#   {"ma": {"lunch": ["11:00", "13:30"], "endwork": null, "evening": ["19:30", "22:00"]}, "di": ..., "zo": ...}
# De achtergrondtaak (sync.py, drip_push_new_books) leest datzelfde formaat.
DRIP_DAYS = [
    ("ma", "Maandag"), ("di", "Dinsdag"), ("wo", "Woensdag"), ("do", "Donderdag"),
    ("vr", "Vrijdag"), ("za", "Zaterdag"), ("zo", "Zondag"),
]
DRIP_WINDOWS = [("lunch", "🥪 Lunchpauze"), ("endwork", "📉 Einde werktijd"), ("evening", "🌜 Avond")]
# Alleen nog gebruikt als er nog geen schema is opgeslagen: de oude, losse
# instellingen (voor elke dag hetzelfde) vormen dan het beginpunt van het grid.
DRIP_DEFAULTS = {
    "bw_drip_interval_minutes": "8",
    "bw_drip_lunch_start": "11:00",
    "bw_drip_lunch_end": "14:00",
    "bw_drip_endwork_start": "16:15",
    "bw_drip_endwork_end": "16:45",
    "bw_drip_evening_start": "19:00",
    "bw_drip_evening_end": "22:00",
}


def _drip_grid_columns():
    """(periode, 0 = van / 1 = tot, kolomtitel) in de volgorde van het grid."""
    columns = []
    for window, label in DRIP_WINDOWS:
        columns.append((window, 0, f"{label} van"))
        columns.append((window, 1, f"{label} tot"))
    return columns


def _load_drip_schedule():
    """Het opgeslagen schema, of (als dat er nog niet is) afgeleid van de oude losse instellingen."""
    raw = get_setting("bw_drip_schedule")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    legacy = {
        window: [
            get_setting(f"bw_drip_{window}_start", DRIP_DEFAULTS[f"bw_drip_{window}_start"]),
            get_setting(f"bw_drip_{window}_end", DRIP_DEFAULTS[f"bw_drip_{window}_end"]),
        ]
        for window, _ in DRIP_WINDOWS
    }
    return {day: {window: list(pair) for window, pair in legacy.items()} for day, _ in DRIP_DAYS}


def _drip_schedule_to_grid(schedule):
    """Zet het schema om naar een tabel: een rij per dag, zes kolommen (3 periodes x van/tot)."""
    rows = []
    for day, day_label in DRIP_DAYS:
        day_data = schedule.get(day) if isinstance(schedule.get(day), dict) else {}
        row = {"Dag": day_label}
        for window, index, column in _drip_grid_columns():
            pair = day_data.get(window)
            value = pair[index] if isinstance(pair, (list, tuple)) and len(pair) == 2 else None
            row[column] = value or None
        rows.append(row)
    # dtype=object: zo blijven lege blokjes echt leeg (None) en worden ze niet NaN/float
    return pd.DataFrame(rows, dtype=object)


def _normalize_drip_time(value):
    """Geeft (tijd als 'uu:mm' of None als leeg, foutmelding of None)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None, None
    text = str(value).strip()
    if text == "":
        return None, None
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)
    if match and int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
        return f"{int(match.group(1)):02d}:{match.group(2)}", None
    return None, f"'{text}' is geen geldige tijd (gebruik uu:mm, bijvoorbeeld 11:00)"


def _drip_grid_to_schedule(grid_df):
    """Zet de ingevulde tabel terug om naar een schema. Geeft (schema, lijst met foutmeldingen)."""
    schedule = {day: {window: None for window, _ in DRIP_WINDOWS} for day, _ in DRIP_DAYS}
    errors = []
    day_by_label = {label: day for day, label in DRIP_DAYS}
    for _, row in grid_df.iterrows():
        day = day_by_label.get(row["Dag"])
        if day is None:
            continue
        for window, window_label in DRIP_WINDOWS:
            start, start_error = _normalize_drip_time(row[f"{window_label} van"])
            end, end_error = _normalize_drip_time(row[f"{window_label} tot"])
            place = f"{row['Dag']}, {window_label}"
            if start_error or end_error:
                errors.append(f"{place}: {start_error or end_error}")
            elif start is None and end is None:
                continue  # bewust leeg gelaten: in dit blokje wordt niet gedruppeld
            elif start is None or end is None:
                errors.append(f"{place}: vul zowel 'van' als 'tot' in, of laat allebei leeg.")
            elif start >= end:
                errors.append(f"{place}: 'tot' ({end}) moet later zijn dan 'van' ({start}).")
            else:
                schedule[day][window] = [start, end]
    return schedule, errors


# De sleutel van het grid krijgt een versienummer dat na het opslaan ophoogt, zodat
# het grid dan opnieuw wordt opgebouwd uit wat er nu daadwerkelijk is opgeslagen
# (bijv. '9.30' wordt dan weergegeven als het opgeslagen '09:30').
drip_grid_version = st.session_state.get("drip_grid_version", 0)

edited_grid = st.data_editor(
    _drip_schedule_to_grid(_load_drip_schedule()),
    key=f"drip_schedule_editor_{drip_grid_version}",
    hide_index=True,
    num_rows="fixed",
    disabled=["Dag"],
    width="stretch",
    column_config={
        "Dag": st.column_config.TextColumn("Dag", width="small"),
        **{
            column: st.column_config.TextColumn(
                column, max_chars=5,
                help="Tijd als uu:mm, bijvoorbeeld 11:00. Laat 'van' én 'tot' leeg als er in dit blokje niet gedruppeld mag worden.",
            )
            for _, _, column in _drip_grid_columns()
        },
    },
)

st.markdown("**⏳ Interval**")
interval_minutes = st.slider(
    "Om de hoeveel minuten een boek wordt toegevoegd",
    min_value=1,
    max_value=20,
    value=int(get_setting("bw_drip_interval_minutes", DRIP_DEFAULTS["bw_drip_interval_minutes"])),
    key="drip_interval",
)

if st.button("Opslaan", key="save_drip_settings"):
    new_schedule, schedule_errors = _drip_grid_to_schedule(edited_grid)
    if schedule_errors:
        st.error("Niet opgeslagen — pas dit eerst aan:\n\n" + "\n".join(f"- {error}" for error in schedule_errors))
    else:
        set_setting("bw_drip_schedule", json.dumps(new_schedule, ensure_ascii=False))
        set_setting("bw_drip_interval_minutes", str(interval_minutes))
        st.session_state["drip_grid_version"] = drip_grid_version + 1
        st.session_state["drip_saved_notice"] = True
        st.rerun()

if st.session_state.pop("drip_saved_notice", False):
    st.success("Opgeslagen.")

st.caption(
    "Geldt alleen voor Boekwinkeltjes. De synchronisatie met Bol kent dit "
    "'nieuw toegevoegd'-mechanisme niet en blijft altijd direct doorgaan. Staan er "
    "na het laatste blokje van de dag nog boeken te wachten, dan worden die in één "
    "keer alsnog aangemaakt. Heeft een dag helemaal geen ingevuld blokje, dan gaat "
    "er die dag niets naar Boekwinkeltjes."
)


# ---------- Verzendkosten Boekwinkeltjes ----------

st.divider()
st.header("Verzendkosten Boekwinkeltjes")
st.caption(
    "De verzendkosten die je klanten betalen. Bij een nieuw boek geeft de app een advies "
    "(busstuk of pakket) op basis van de afmetingen: bij een busstuk worden de kosten voor "
    "briefpost voorgesteld, bij een pakket die voor pakketpost, en is het onbekend hoe groot "
    "het boek is, dan het hoogste van de twee. Past de vervoerder zijn prijzen aan, dan kun je "
    "dat hier doen. De nieuwe bedragen gelden voor boeken die je vanaf nu invoert; boeken die "
    "al zijn opgeslagen houden hun eigen verzendkosten."
)

current_briefpost, current_pakketpost = get_shipping_costs()
shipping_col1, shipping_col2 = st.columns(2)
with shipping_col1:
    shipping_briefpost = st.number_input(
        "✉️ Briefpost (€)", min_value=0.0, value=current_briefpost, step=0.05, format="%.2f",
        key="shipping_briefpost",
    )
with shipping_col2:
    shipping_pakketpost = st.number_input(
        "📦 Pakketpost (€)", min_value=0.0, value=current_pakketpost, step=0.05, format="%.2f",
        key="shipping_pakketpost",
    )

if st.button("Opslaan", key="save_shipping_costs"):
    if shipping_briefpost <= 0 or shipping_pakketpost <= 0:
        st.error("Niet opgeslagen — vul bij allebei een bedrag in dat groter is dan 0.")
    else:
        set_setting("bw_shipping_briefpost", f"{shipping_briefpost:.2f}")
        set_setting("bw_shipping_pakketpost", f"{shipping_pakketpost:.2f}")
        get_shipping_costs.clear()  # zodat de andere pagina's meteen met de nieuwe bedragen werken
        st.success("Opgeslagen.")
        if shipping_briefpost > shipping_pakketpost:
            st.warning("Let op: briefpost is nu duurder dan pakketpost. Klopt dat? Zo niet, dan staan ze mogelijk verwisseld.")


# ---------- Dagcode voor klanten ----------

DAILY_SETTING = "webshop_daily_code"

st.divider()
st.header("Dagcode voor klanten")
st.caption(
    "Elke dag verzint het systeem één code van vier tekens (zonder I, O, 1 en 0). Klanten die die dag een bestelling betalen krijgen die code "
    "in de bevestigingsmail, met een percentage korting op hun volgende boeken. Elke code is een maand geldig. Je kunt de code van vandaag "
    "hier opzoeken om hem zelf aan een klant te geven, bijvoorbeeld via Boekwinkeltjes. De codes zijn voor de webshop-server altijd direct geldig."
)

try:
    _daily_cfg = json.loads(get_setting(DAILY_SETTING, "{}") or "{}")
    if not isinstance(_daily_cfg, dict):
        _daily_cfg = {}
except ValueError:
    _daily_cfg = {}
_daily_active_now = bool(_daily_cfg.get("active", True))
try:
    _daily_pct_now = max(1, min(100, int(round(float(_daily_cfg.get("percent", 15))))))
except (TypeError, ValueError):
    _daily_pct_now = 15

_daily_left, _daily_right = st.columns(2)
daily_active = _daily_left.checkbox("Dagcode in de bevestigingsmail zetten", value=_daily_active_now, key="daily_active")
daily_pct = _daily_right.number_input("Percentage korting", min_value=1, max_value=100, value=_daily_pct_now, step=1, key="daily_pct")
if st.button("Dagcode-instelling opslaan", key="save_daily_code"):
    set_setting(DAILY_SETTING, json.dumps({"active": bool(daily_active), "percent": int(daily_pct)}))
    st.success("Opgeslagen. De mails en de webshop gebruiken dit na de eerstvolgende catalogus-update (binnen een half uur). "
               "Let op: het percentage geldt voor alle codes die nog geldig zijn.")

_data_key = (_get_secret("WEBSHOP_DATA_KEY") or "").strip()
if not _data_key:
    st.info("Om de codes hier te kunnen tonen is de sleutel nodig: zet in de secrets van Streamlit WEBSHOP_DATA_KEY "
            "(dezelfde waarde als het geheim WEBSHOP_DATA_KEY in GitHub).")
else:
    _codes = active_daily_codes(_data_key)
    _today, _today_code, _today_until = _codes[0]
    st.markdown(f"**Code van vandaag ({_today.strftime('%d-%m-%Y')}):**")
    st.code(_today_code, language=None)
    st.caption(f"Geldig tot en met {_today_until.strftime('%d-%m-%Y')}. Een klant die vandaag betaalt, krijgt deze code in de mail.")
    with st.expander("Alle codes die nu nog geldig zijn"):
        st.dataframe(
            pd.DataFrame([{"Uitgegeven op": d.strftime("%d-%m-%Y"), "Code": c, "Geldig tot en met": u.strftime("%d-%m-%Y")} for d, c, u in _codes]),
            hide_index=True, width="stretch",
        )


# ---------- Kortingscodes webshop ----------

DISCOUNT_SETTING = "webshop_discount_codes"
DISCOUNT_KINDS = {"Percentage korting": "percent", "Gratis verzending": "shipping"}

st.divider()
st.header("Kortingscodes webshop")
st.caption(
    "Codes die klanten bij het afrekenen op severein.nl kunnen invullen. Er zijn twee soorten: een percentage korting op de "
    "boekenprijs, of geen verzendkosten. Een code mag je uitzetten (vinkje) of laten verlopen met een einddatum (die dag geldt hij nog). "
    "Een wijziging staat in de webshop zodra de catalogus de eerstvolgende keer wordt bijgewerkt (binnen een half uur); "
    "de server controleert elke code bij het afrekenen opnieuw."
)


def _load_discount_rows():
    try:
        raw = json.loads(get_setting(DISCOUNT_SETTING, "[]") or "[]")
    except ValueError:
        raw = []
    rows = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        until = None
        try:
            if it.get("until"):
                until = pd.to_datetime(it["until"]).date()
        except Exception:
            until = None
        rows.append({
            "Code": it.get("code", ""),
            "Soort": "Gratis verzending" if it.get("type") == "shipping" else "Percentage korting",
            "Percentage": None if it.get("type") == "shipping" else it.get("percent"),
            "Tot en met": until,
            "Actief": bool(it.get("active", True)),
        })
    return pd.DataFrame(rows, columns=["Code", "Soort", "Percentage", "Tot en met", "Actief"])


discount_df = st.data_editor(
    _load_discount_rows(), num_rows="dynamic", hide_index=True, width="stretch", key="discount_editor",
    column_config={
        "Code": st.column_config.TextColumn("Code", help="Letters en cijfers; hoofdletters maken niet uit.", max_chars=30),
        "Soort": st.column_config.SelectboxColumn("Soort", options=list(DISCOUNT_KINDS), default="Percentage korting", required=True),
        "Percentage": st.column_config.NumberColumn("Percentage (%)", min_value=1, max_value=100, step=1, format="%d"),
        "Tot en met": st.column_config.DateColumn("Tot en met (optioneel)", format="DD-MM-YYYY"),
        "Actief": st.column_config.CheckboxColumn("Actief", default=True),
    },
)

if st.button("Kortingscodes opslaan", key="save_discount_codes"):
    problems, saved, seen = [], [], set()
    for _, row in discount_df.iterrows():
        code = re.sub(r"[^A-Z0-9_.\-]", "", str(row.get("Code") or "").strip().upper())[:30]
        if not code:
            if str(row.get("Code") or "").strip():
                problems.append(f"'{row.get('Code')}' bevat geen bruikbare tekens.")
            continue
        if code in seen:
            problems.append(f"De code {code} staat er twee keer in.")
            continue
        seen.add(code)
        kind = DISCOUNT_KINDS.get(row.get("Soort"), "percent")
        entry = {"code": code, "type": kind, "active": bool(row.get("Actief", True))}
        if kind == "percent":
            pct = row.get("Percentage")
            if pct is None or pd.isna(pct) or not 1 <= float(pct) <= 100:
                problems.append(f"{code}: vul een percentage tussen 1 en 100 in.")
                continue
            entry["percent"] = int(round(float(pct)))
        until = row.get("Tot en met")
        if until is not None and not pd.isna(until):
            entry["until"] = pd.to_datetime(until).strftime("%Y-%m-%d")
        saved.append(entry)
    if problems:
        st.error("Niet opgeslagen — " + " ".join(problems))
    else:
        set_setting(DISCOUNT_SETTING, json.dumps(saved, ensure_ascii=False))
        st.success(f"Opgeslagen ({len(saved)} code(s)). Actief in de webshop na de eerstvolgende catalogus-update.")


# ---------- GitHub-sleutel ----------

st.divider()
st.header("GitHub-sleutel")
st.caption(
    "Alle geplande taken (cron-job.org) en de knoppen in dit dashboard gebruiken één GitHub-sleutel. Heeft die een einddatum, "
    "dan stoppen alle taken tegelijk zodra hij verloopt. GitHub geeft die datum niet betrouwbaar door, dus vul je hem hier in, "
    "zoals GitHub hem toont (\"Expires on …\"). Home waarschuwt dan 30 dagen van tevoren, en de waakhond mailt je. "
    "Heeft de sleutel geen einddatum, vink dan \"verloopt niet\" aan: dan blijft de waarschuwing weg."
)
current_token_expiry = get_github_token_expiry()
banner_kind, banner_text = github_token_banner(current_token_expiry)
getattr(st, banner_kind)(banner_text)
token_never_expires = st.checkbox(
    "Deze sleutel verloopt niet", value=(current_token_expiry == GITHUB_TOKEN_NEVER), key="github_token_never"
)
token_expiry_input = st.date_input(
    "Einddatum van de GitHub-sleutel",
    value=None if current_token_expiry in (None, GITHUB_TOKEN_NEVER) else current_token_expiry,
    format="DD-MM-YYYY",
    disabled=token_never_expires,
    key="github_token_expiry_input",
)
if st.button("Opslaan", key="save_github_token_expiry"):
    if token_never_expires:
        set_github_token_expiry(GITHUB_TOKEN_NEVER)
        st.success("Opgeslagen: de sleutel verloopt niet.")
        st.rerun()
    elif token_expiry_input is None:
        st.error("Niet opgeslagen — kies eerst een datum, of vink aan dat de sleutel niet verloopt.")
    else:
        set_github_token_expiry(token_expiry_input)
        st.success(f"Opgeslagen: de sleutel verloopt op {token_expiry_input.strftime('%d-%m-%Y')}.")
        st.rerun()
with st.expander("Zo vernieuw je de sleutel"):
    st.markdown(
        """
1. Ga op GitHub (profielfoto rechtsboven) naar **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Maak een nieuwe sleutel voor alleen de repository `boekbeheer`, met het recht **Actions: Read and write**. Kies de langst
   mogelijke looptijd, of geen einddatum als GitHub dat aanbiedt en je dat risico aanvaardbaar vindt. Of kies **Regenerate
   token** bij de bestaande sleutel, als GitHub dat aanbiedt.
3. Kopieer de nieuwe waarde meteen, want GitHub toont hem maar één keer. Deel hem nergens, ook niet in een chat.
4. Vervang hem op twee plekken: in de Streamlit-instellingen (`GITHUB_TOKEN`) en in de kopregel `Authorization`
   (`Bearer …`) van **elke** job bij cron-job.org.
5. Vul hierboven de nieuwe einddatum in, of vink aan dat de sleutel niet verloopt.
        """
    )


st.divider()
st.header("Nepfoto's die nog niet naar Boekwinkeltjes zijn gestuurd")
st.caption(
    "Soms geeft Google of een andere bron als omslag alleen een plaatshouder (\"BOOK COVER NOT AVAILABLE\"). "
    "Nieuwe boeken krijgen die niet meer mee, maar boeken die al eerder zijn aangemaakt (in de bulk-wachtrij of via "
    "Nieuw boek) kunnen er nog een hebben. Hier zoek je ze op en verwijder je ze, **voordat** ze naar Boekwinkeltjes gaan. "
    "Dit geldt voor alle foto's die nog niet zijn doorgestuurd, niet alleen voor de bulk-wachtrij. Foto's die al bij "
    "Boekwinkeltjes staan, worden niet bekeken of aangeraakt. Je ziet eerst welke het zijn, en verwijdert ze zelf."
)

delete_message = st.session_state.pop("placeholder_delete_message", None)
if delete_message:
    st.success(delete_message)

if st.button("Zoek nepfoto's", key="find_placeholder_uploads"):
    st.session_state["placeholder_uploads_found"] = find_placeholder_uploads()

found_placeholders = st.session_state.get("placeholder_uploads_found")
if found_placeholders is not None:
    if not found_placeholders:
        st.success("Geen nepfoto's gevonden.")
    else:
        st.warning(f"{len(found_placeholders)} nepfoto('s) gevonden bij {len({f['book_id'] for f in found_placeholders})} boek(en).")
        st.dataframe(
            pd.DataFrame(
                [{"Boek": f["title"], "ISBN": f["ean"], "Boek-id": f["book_id"]} for f in found_placeholders]
            ),
            width="stretch",
            hide_index=True,
        )
        if st.button(f"Verwijder deze {len(found_placeholders)} nepfoto('s)", key="delete_placeholder_uploads"):
            deleted_count = delete_placeholder_uploads([f["upload_id"] for f in found_placeholders])
            get_all_images.clear()  # zodat Boekdetails de foto's meteen opnieuw laadt
            st.session_state["placeholder_uploads_found"] = None
            st.session_state["placeholder_delete_message"] = (
                f"{deleted_count} nepfoto('s) verwijderd. De boeken zelf zijn niet aangeraakt."
            )
            st.rerun()

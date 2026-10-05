"""
Herkent plaatjes die geen echte omslag zijn: plaatshouders zoals "BOOK COVER NOT AVAILABLE", en plaatjes die
onleesbaar of te klein zijn. Bedoeld voor automatisch opgehaalde omslagen (Google Books, ISBNdb, Open Library),
zodat zo'n plaatje niet als foto bij een boek komt en dan naar Boekwinkeltjes wordt gestuurd.

De vergelijking is een 'vingerafdruk' van het plaatje (zie _fingerprint) en dus ongevoelig voor formaat en
compressie. Voorbeelden van plaatshouders staan in assets/placeholders/; zet je daar een nieuw voorbeeld bij,
dan wordt die soort ook herkend.
"""
import io
import os

from PIL import Image

PLACEHOLDER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "placeholders")
# Hoeveel van de 64 punten een vingerafdruk mag afwijken om nog als dezelfde plaatshouder te tellen. Gemeten:
# dezelfde plaatshouder, verkleind, vergroot, sterk gecomprimeerd of licht vervaagd, wijkt hooguit 4 punten af;
# nagebootste echte omslagen (ook grijze met een licht vlak) zitten er minstens 15 vandaan.
MAX_DISTANCE = 6
# Een omslag waarvan de kortste zijde korter is dan dit (bijvoorbeeld een 1x1-pixel plaatje) is onbruikbaar.
MIN_SIDE_PX = 50

_reference_cache = {"signature": None, "fingerprints": []}


def _to_gray(im):
    """Naar grijstinten, met doorzichtige delen op wit (anders telt een doorzichtige achtergrond als zwart)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        background = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(background, im)
    return im.convert("RGB").convert("L")


def _fingerprint(im):
    """Twee 64-bits vingerafdrukken: dHash (verschil tussen buurpixels) en aHash (boven/onder het gemiddelde)."""
    gray = _to_gray(im)
    pixels = list(gray.resize((9, 8), Image.LANCZOS).tobytes())
    dhash = 0
    for row in range(8):
        for col in range(8):
            dhash = (dhash << 1) | (1 if pixels[row * 9 + col] > pixels[row * 9 + col + 1] else 0)
    pixels = list(gray.resize((8, 8), Image.LANCZOS).tobytes())
    average = sum(pixels) / 64
    ahash = 0
    for value in pixels:
        ahash = (ahash << 1) | (1 if value > average else 0)
    return dhash, ahash


def _distance(a, b):
    return bin(a ^ b).count("1")


def _reference_fingerprints(directory):
    """De vingerafdrukken van de voorbeelden in 'directory'; opnieuw ingelezen zodra de map verandert."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")))
    except OSError:
        return []
    signature = (directory, tuple((n, os.path.getmtime(os.path.join(directory, n))) for n in names))
    if _reference_cache["signature"] == signature:
        return _reference_cache["fingerprints"]
    fingerprints = []
    for name in names:
        try:
            with Image.open(os.path.join(directory, name)) as im:
                im.load()
                fingerprints.append((name, _fingerprint(im)))
        except Exception:
            continue  # een onleesbaar voorbeeld slaan we over; de rest blijft werken
    _reference_cache["signature"] = signature
    _reference_cache["fingerprints"] = fingerprints
    return fingerprints


def check_cover_image(data, reference_dir=None):
    """
    Beoordeelt een omslagplaatje. Geeft terug:
      'ok'           - lijkt een echte omslag,
      'placeholder'  - lijkt op een bekende plaatshouder ('geen omslag beschikbaar'),
      'too_small'    - te klein om bruikbaar te zijn,
      'unreadable'   - geen plaatje dat gelezen kan worden.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            if min(width, height) < MIN_SIDE_PX:
                return "too_small"
            dhash, ahash = _fingerprint(im)
    except Exception:
        return "unreadable"
    for _name, (ref_dhash, ref_ahash) in _reference_fingerprints(reference_dir or PLACEHOLDER_DIR):
        if _distance(dhash, ref_dhash) <= MAX_DISTANCE and _distance(ahash, ref_ahash) <= MAX_DISTANCE:
            return "placeholder"
    return "ok"

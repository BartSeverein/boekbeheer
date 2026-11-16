"""
kaarten_verkleinen.py — Verkleint scans van ansichtkaarten vóór je ze uploadt naar het dashboard.

Zonder opties opent het een klein venster waarin je de map met scans en de map voor de verkleinde scans kiest.
Alle .jpg, .jpeg en .png in de eerste map (ook in submappen) worden JPEG's van hoogstens 1600 pixels op de langste zijde, met
dezelfde bestandsnaam (260112_01v.jpg) in de tweede map. Bestaat het bestand daar al, dan wordt het overgeslagen.
Aan het eind staat welke kaarten een voorkant (v) of achterkant (a) missen. De originelen blijven ongemoeid.

Als programma voor Windows: zie .github/workflows/build-kaarten-verkleinen.yml (maakt kaarten_verkleinen.exe).
Op de opdrachtregel kan het ook:  python kaarten_verkleinen.py "map met scans" "map voor kleine scans"
"""

import queue
import re
import sys
import threading
from pathlib import Path

from PIL import Image, ImageOps

MAX_PX = 1600
KWALITEIT = 82


def verklein_map(bron, doel, melding=print, voortgang=lambda klaar, totaal: None):
    """Verkleint alle scans in 'bron' naar 'doel'. 'melding' krijgt tekstregels, 'voortgang' (klaar, totaal)."""
    bron, doel = Path(bron), Path(doel)
    if not bron.is_dir():
        melding(f"Map niet gevonden: {bron}")
        return
    doel.mkdir(parents=True, exist_ok=True)
    bestanden = [p for p in sorted(bron.rglob("*")) if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if doel.resolve() == bron.resolve() or doel.resolve() in [p.parent.resolve() for p in bestanden[:1]]:
        melding("Kies voor de verkleinde scans een andere map dan die met de originelen.")
        return
    gedaan = overgeslagen = fouten = 0
    kanten = {}
    for i, pad in enumerate(bestanden, 1):
        uitvoer = doel / (pad.stem + ".jpg")
        m = re.match(r"^(.+?)([vaVA])$", pad.stem)
        if m:
            kanten.setdefault(m.group(1), set()).add(m.group(2).lower())
        if uitvoer.exists():
            overgeslagen += 1
        else:
            try:
                img = ImageOps.exif_transpose(Image.open(pad)).convert("RGB")
                img.thumbnail((MAX_PX, MAX_PX))
                img.save(uitvoer, "JPEG", quality=KWALITEIT, optimize=True)
                gedaan += 1
            except Exception as exc:
                fouten += 1
                melding(f"Mislukt: {pad.name}: {exc}")
        voortgang(i, len(bestanden))
    melding(f"Klaar: {gedaan} verkleind, {overgeslagen} overgeslagen (stonden er al), {fouten} mislukt.")
    mist = [nr for nr, k in sorted(kanten.items()) if k != {"v", "a"}]
    if mist:
        melding("Let op, een scan ontbreekt bij: " + ", ".join(mist))


def venster():
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title("Kaarten verkleinen")
    root.geometry("640x430")
    bron, doel = tk.StringVar(), tk.StringVar()
    berichten = queue.Queue()

    def rij(rijnr, tekst, var):
        ttk.Label(root, text=tekst).grid(row=rijnr, column=0, sticky="w", padx=10, pady=(12, 0), columnspan=2)
        ttk.Entry(root, textvariable=var, width=62).grid(row=rijnr + 1, column=0, sticky="we", padx=10)
        ttk.Button(root, text="Kies map…", command=lambda: var.set(filedialog.askdirectory(title=tekst) or var.get())
                   ).grid(row=rijnr + 1, column=1, padx=(0, 10))

    rij(0, "Map met de scans (originelen)", bron)
    rij(2, "Map voor de verkleinde scans (een andere map)", doel)
    balk = ttk.Progressbar(root, mode="determinate")
    balk.grid(row=5, column=0, columnspan=2, sticky="we", padx=10, pady=(16, 4))
    log = tk.Text(root, height=12, state="disabled", wrap="word")
    log.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=10, pady=(4, 10))
    root.columnconfigure(0, weight=1)
    root.rowconfigure(6, weight=1)

    def schrijf(tekst):
        log.configure(state="normal"); log.insert("end", tekst + "\n"); log.see("end"); log.configure(state="disabled")

    def pols():
        try:
            while True:
                soort, waarde = berichten.get_nowait()
                if soort == "tekst":
                    schrijf(waarde)
                elif soort == "voortgang":
                    balk["maximum"], balk["value"] = waarde[1], waarde[0]
                elif soort == "klaar":
                    knop.configure(state="normal")
        except queue.Empty:
            pass
        root.after(150, pols)

    def start():
        if not bron.get() or not doel.get():
            schrijf("Kies eerst beide mappen.")
            return
        knop.configure(state="disabled")
        schrijf("Bezig…")

        def werk():
            try:
                verklein_map(bron.get(), doel.get(), lambda t: berichten.put(("tekst", t)),
                             lambda k, t: berichten.put(("voortgang", (k, t))))
            except Exception as exc:
                berichten.put(("tekst", f"Fout: {exc}"))
            berichten.put(("klaar", None))

        threading.Thread(target=werk, daemon=True).start()

    knop = ttk.Button(root, text="Verkleinen", command=start)
    knop.grid(row=4, column=0, columnspan=2, pady=(14, 0))
    pols()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        verklein_map(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 1:
        venster()
    else:
        print(__doc__)

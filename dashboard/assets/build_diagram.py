import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle, Ellipse, Wedge

W, H = 940, 530
fig, ax = plt.subplots(figsize=(14, 14 * H / W))
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.invert_yaxis()
ax.set_aspect("equal")
ax.axis("off")

GREEN = "#3B6D11"
RED = "#A32D2D"
BLUE = "#185FA5"
ORANGE = "#EF9F27"
THIN_GRAY = "#888780"
PURPLE_FILL = "#EEEDFE"; PURPLE_EDGE = "#7F77DD"; PURPLE_TEXT = "#26215C"
CORAL_FILL = "#FAECE7"; CORAL_EDGE = "#D85A30"; CORAL_TEXT = "#4A1B0C"
GRAY_FILL = "#F1EFE8"; GRAY_EDGE = "#888780"; GRAY_TEXT = "#2C2C2A"
MAIL = "#B5368A"
TEAL_FILL = "#E1F5EE"; TEAL_EDGE = "#1D9E75"; TEAL_TEXT = "#04342C"; TEAL = "#0F6E56"
AMBER_FILL = "#FAEEDA"; AMBER_EDGE = "#BA7517"; AMBER_TEXT = "#412402"
BLUE_FILL = "#E6F1FB"; BLUE_EDGE = "#378ADD"; BLUE_TEXT = "#042C53"
PEOPLE = "#5F5E5A"
SHIP = "#1B7F95"
CYAN_FILL = "#E0F4F7"; CYAN_EDGE = "#2A9DB5"; CYAN_TEXT = "#0B3A45"


def box(x, y, w, h, title, subtitle, facecolor, edgecolor, textcolor):
    b = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=8",
                        linewidth=1, edgecolor=edgecolor, facecolor=facecolor)
    ax.add_patch(b)
    ax.text(x + w / 2, y + h * 0.38, title, ha="center", va="center", color=textcolor, fontsize=12, fontweight="medium")
    ax.text(x + w / 2, y + h * 0.72, subtitle, ha="center", va="center", color=textcolor, fontsize=9.5)


def arrow(x1, y1, x2, y2, color, lw, both=False, scale=14):
    style = "<|-|>" if both else "-|>"
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=scale,
                         linewidth=lw, color=color, shrinkA=0, shrinkB=0)
    ax.add_patch(a)


def cloud_with_people(cx, y0):
    """Wolkje (bovenkant op y0, gecentreerd op cx) met drie mensen en het woord 'Klanten'."""
    shapes = [
        ("c", cx, y0 + 28, 28),
        ("c", cx - 40, y0 + 34, 22),
        ("c", cx + 40, y0 + 34, 22),
        ("c", cx - 62, y0 + 50, 18),
        ("c", cx + 62, y0 + 50, 18),
        ("e", cx, y0 + 52, 150, 40),
    ]

    def make(shape, **kw):
        if shape[0] == "c":
            return Circle((shape[1], shape[2]), shape[3], **kw)
        return Ellipse((shape[1], shape[2]), shape[3], shape[4], **kw)

    for s in shapes:
        ax.add_patch(make(s, facecolor=GRAY_FILL, edgecolor=GRAY_EDGE, linewidth=2.4, zorder=1))
    for s in shapes:
        ax.add_patch(make(s, facecolor=GRAY_FILL, edgecolor="none", linewidth=0, zorder=2))

    for dx in (-32, 0, 32):
        px = cx + dx
        ax.add_patch(Circle((px, y0 + 27), 6.5, facecolor=PEOPLE, edgecolor="none", zorder=3))
        ax.add_patch(Wedge((px, y0 + 47), 11, 180, 360, facecolor=PEOPLE, edgecolor="none", zorder=3))
    ax.text(cx, y0 + 61, "Klanten", ha="center", va="center", fontsize=10, color=GRAY_TEXT, zorder=3)


# Rij 1: verrijkingsbronnen
box(80, 40, 130, 56, "ISBNdb", "boekgegevens", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)
box(315, 40, 130, 56, "Google Books", "boekgegevens", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)
box(550, 40, 130, 56, "Open Library", "omslagfoto", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)

arrow(145, 96, 378, 159, GREEN, 2.5)
arrow(380, 96, 380, 159, GREEN, 2.5)
arrow(615, 96, 382, 159, GREEN, 2.5)

# Hoofdrij
box(40, 160, 170, 64, "Boekwinkeltjes", "voorraad en orders", CORAL_FILL, CORAL_EDGE, CORAL_TEXT)
box(290, 160, 180, 64, "Boekbeheersysteem", "Database en app", GRAY_FILL, GRAY_EDGE, GRAY_TEXT)
box(550, 160, 170, 64, "Bol", "voorraad en orders", BLUE_FILL, BLUE_EDGE, BLUE_TEXT)

# BW <-> App
arrow(210, 168, 288, 168, GREEN, 2.5)
arrow(210, 184, 288, 184, RED, 2.5)
arrow(210, 200, 290, 200, BLUE, 2.5, both=True)
arrow(210, 216, 290, 216, ORANGE, 2.5, both=True)

# App <-> Bol: groen/rood ongewijzigd, blauw bidirectioneel, oranje eenrichtings (App->Bol)
arrow(550, 168, 472, 168, GREEN, 2.5)
arrow(550, 184, 472, 184, RED, 2.5)
arrow(470, 200, 550, 200, BLUE, 2.5, both=True)
arrow(470, 216, 550, 216, ORANGE, 2.5)

# Tweede rij: klanten van BW en Bol, boekenopslag en de eigen webshop
ROW3_Y = 276
cloud_with_people(125, ROW3_Y)
arrow(125, 224, 125, ROW3_Y, THIN_GRAY, 1.2, both=True, scale=10)
cloud_with_people(840, 156)
arrow(722, 192, 764, 196, THIN_GRAY, 1.2, both=True, scale=10)

box(225, ROW3_Y, 150, 64, "Boekenopslag", "Magazijn en orderpicking", GRAY_FILL, GRAY_EDGE, GRAY_TEXT)
arrow(310, 224, 310, ROW3_Y, THIN_GRAY, 1.2, both=True, scale=10)

box(395, ROW3_Y, 150, 64, "Webshop", "severein.nl", TEAL_FILL, TEAL_EDGE, TEAL_TEXT)
# App -> Webshop: boekgegevens (oranje) en voorraad (blauw) via de nachtelijke/viertelijkse build; Webshop -> App: orders (rood)
arrow(406, 224, 406, ROW3_Y, ORANGE, 2.5)
arrow(430, 224, 430, ROW3_Y, BLUE, 2.5)
arrow(454, ROW3_Y, 454, 224, RED, 2.5)

# Intime (pakketdienst): haalt orders op bij Bol en de webshop, meldt verzending (track-and-trace) terug aan Bol, de webshop en de app
box(585, ROW3_Y, 150, 64, "Intime", "pakketdienst en labels", CYAN_FILL, CYAN_EDGE, CYAN_TEXT)
arrow(635, 224, 635, ROW3_Y, SHIP, 2.5, both=True)
arrow(547, ROW3_Y + 32, 583, ROW3_Y + 32, SHIP, 2.5, both=True)
arrow(468, 226, 620, ROW3_Y - 2, SHIP, 2.5, both=True)

# Onderste rij: klanten van de webshop en Mollie
ROW4_Y = 408
cloud_with_people(420, ROW4_Y)
arrow(445, ROW3_Y + 64, 445, ROW4_Y + 2, THIN_GRAY, 1.2, both=True, scale=10)
box(545, ROW4_Y + 12, 165, 62, "Mollie", "betalingen (Wero e.a.)", AMBER_FILL, AMBER_EDGE, AMBER_TEXT)
arrow(515, ROW3_Y + 64, 585, ROW4_Y + 12, TEAL, 2.5, both=True)
arrow(505, ROW4_Y + 46, 543, ROW4_Y + 46, TEAL, 2.5, both=True)

# Brevo (SMTP): de webshop stuurt bevestigingsmails via Brevo naar de klant
box(95, ROW4_Y + 12, 160, 62, "Brevo", "bevestigingsmails (SMTP)", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)
arrow(405, ROW3_Y + 64, 235, ROW4_Y + 12, MAIL, 2.5)
arrow(257, ROW4_Y + 43, 332, ROW4_Y + 43, MAIL, 2.5)

# Legenda
LEGEND_Y = 515
legend_items = [
    (40, GREEN, "Gegevensverrijking"),
    (200, RED, "Orders"),
    (290, BLUE, "Voorraad"),
    (395, ORANGE, "Boekgegevens"),
    (530, TEAL, "Betalingen"),
    (650, MAIL, "E-mail"),
    (770, SHIP, "Verzending"),
]
for x, color, label in legend_items:
    ax.plot([x, x + 20], [LEGEND_Y, LEGEND_Y], color=color, linewidth=2.5, solid_capstyle="round")
    ax.text(x + 28, LEGEND_Y, label, ha="left", va="center", fontsize=9.5, color="#2C2C2A")

plt.tight_layout()
plt.savefig("Architectuur.png", dpi=200, facecolor="white", bbox_inches="tight")
print("saved")

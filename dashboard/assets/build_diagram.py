import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle, Ellipse, Wedge

W, H = 680, 395
fig, ax = plt.subplots(figsize=(11.5, 11.5 * H / W))
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
BLUE_FILL = "#E6F1FB"; BLUE_EDGE = "#378ADD"; BLUE_TEXT = "#042C53"
PEOPLE = "#5F5E5A"


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
box(70, 40, 130, 56, "ISBNdb", "boekgegevens", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)
box(275, 40, 130, 56, "Google Books", "boekgegevens", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)
box(480, 40, 130, 56, "Open Library", "omslagfoto", PURPLE_FILL, PURPLE_EDGE, PURPLE_TEXT)

arrow(135, 96, 338, 159, GREEN, 2.5)
arrow(340, 96, 340, 159, GREEN, 2.5)
arrow(545, 96, 342, 159, GREEN, 2.5)

# Hoofdrij
box(40, 160, 170, 64, "Boekwinkeltjes", "voorraad en orders", CORAL_FILL, CORAL_EDGE, CORAL_TEXT)
box(250, 160, 180, 64, "Boekbeheersysteem", "Database en app", GRAY_FILL, GRAY_EDGE, GRAY_TEXT)
box(460, 160, 170, 64, "Bol", "voorraad en orders", BLUE_FILL, BLUE_EDGE, BLUE_TEXT)

# BW <-> App
arrow(210, 168, 248, 168, GREEN, 2.5)
arrow(210, 184, 248, 184, RED, 2.5)
arrow(210, 200, 250, 200, BLUE, 2.5, both=True)
arrow(210, 216, 250, 216, ORANGE, 2.5, both=True)

# App <-> Bol: groen/rood ongewijzigd, blauw nu bidirectioneel, oranje nu eenrichtings (App->Bol)
arrow(460, 168, 432, 168, GREEN, 2.5)
arrow(460, 184, 432, 184, RED, 2.5)
arrow(430, 200, 460, 200, BLUE, 2.5, both=True)
arrow(430, 216, 460, 216, ORANGE, 2.5)

# Onderste rij: boekenopslag en klanten, verbonden met dunne grijze tweerichtingspijlen
BOTTOM_Y = 264
box(250, BOTTOM_Y, 180, 64, "Boekenopslag", "Magazijn en orderpicking", GRAY_FILL, GRAY_EDGE, GRAY_TEXT)
arrow(340, 224, 340, BOTTOM_Y, THIN_GRAY, 1.2, both=True, scale=10)

cloud_with_people(125, BOTTOM_Y)
arrow(125, 224, 125, BOTTOM_Y, THIN_GRAY, 1.2, both=True, scale=10)
cloud_with_people(545, BOTTOM_Y)
arrow(545, 224, 545, BOTTOM_Y, THIN_GRAY, 1.2, both=True, scale=10)

# Legenda
LEGEND_Y = 375
legend_items = [
    (60, GREEN, "Gegevensverrijking"),
    (294, RED, "Orders"),
    (392, BLUE, "Voorraad"),
    (500, ORANGE, "Boekgegevens"),
]
for x, color, label in legend_items:
    ax.plot([x, x + 20], [LEGEND_Y, LEGEND_Y], color=color, linewidth=2.5, solid_capstyle="round")
    ax.text(x + 28, LEGEND_Y, label, ha="left", va="center", fontsize=9.5, color="#2C2C2A")

plt.tight_layout()
plt.savefig("Architectuur.png", dpi=200, facecolor="white", bbox_inches="tight")
print("saved")

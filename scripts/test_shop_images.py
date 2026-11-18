"""
Draw the test shop's product photos (``src/unfret/static/test-shop/*.jpg``): made-up products, no real one.

A star projector (the test shop and its sister shop) and an oak lamp (the clean shop). Run once and commit the
output: ``uv run scripts/test_shop_images.py``. The drawings are deterministic (fixed seed).
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "src" / "unfret" / "static" / "test-shop"
SIZE = 800


def gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """A vertical gradient."""
    image = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(image)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        draw.line([(0, y), (SIZE, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return image


def stars(image: Image.Image, rng: random.Random, count: int, box: tuple[int, int, int, int]) -> None:
    """Scatter small stars inside a box."""
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    for _ in range(count):
        x, y, r = rng.randint(x0, x1), rng.randint(y0, y1), rng.choice((1, 1, 2, 2, 3))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 250, 235))


def projector(image: Image.Image, rng: random.Random, cx: int, base_y: int, scale: float) -> None:
    """The dome projector: a dark base, a glowing translucent dome with stars."""
    w, h = int(300 * scale), int(80 * scale)
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - w, base_y - int(330 * scale), cx + w, base_y + int(40 * scale)], fill=(150, 90, 255, 90))
    image.paste(glow.filter(ImageFilter.GaussianBlur(60)), (0, 0), glow.filter(ImageFilter.GaussianBlur(60)))
    dome = Image.new("RGBA", image.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(dome)
    r = int(150 * scale)
    dd.pieslice([cx - r, base_y - h // 2 - r, cx + r, base_y - h // 2 + r], 180, 360, fill=(120, 170, 255, 150))
    dd.ellipse(
        [cx - r + 30, base_y - h // 2 - r + 20, cx - r + 90, base_y - h // 2 - r + 120], fill=(255, 255, 255, 70)
    )
    image.paste(dome, (0, 0), dome)
    stars(image, rng, int(40 * scale), (cx - r + 25, base_y - h // 2 - r + 30, cx + r - 25, base_y - h // 2 - 10))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [cx - w // 2 - 10, base_y - h // 2, cx + w // 2 + 10, base_y + h // 2], 18, fill=(36, 38, 52)
    )
    draw.rounded_rectangle(
        [cx - w // 2 - 10, base_y - h // 2, cx + w // 2 + 10, base_y - h // 2 + 8], 4, fill=(70, 74, 96)
    )
    for i in range(4):
        bx = cx - int(60 * scale) + i * int(40 * scale)
        draw.ellipse(
            [bx, base_y - 6, bx + int(14 * scale), base_y + 8], fill=(90, 200, 255) if i == 1 else (110, 112, 130)
        )


def lamp(image: Image.Image, cx: int, base_y: int) -> None:
    """The clean shop's bedside lamp: a turned oak base and a frosted glass globe, switched on."""
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 260, base_y - 520, cx + 260, base_y - 40], fill=(255, 214, 150, 110))
    blurred = glow.filter(ImageFilter.GaussianBlur(70))
    image.paste(blurred, (0, 0), blurred)
    draw = ImageDraw.Draw(image)
    draw.ellipse([cx - 210, base_y - 14, cx + 210, base_y + 22], fill=(196, 186, 172))  # its shadow on the table
    draw.rounded_rectangle([cx - 110, base_y - 90, cx + 110, base_y], 22, fill=(168, 122, 76))
    for i, y in enumerate(range(base_y - 80, base_y - 6, 14)):  # the oak's grain
        draw.arc([cx - 100, y - 6, cx + 100, y + 8], 0, 180, fill=(150, 106, 64) if i % 2 else (184, 138, 90), width=2)
    draw.rectangle([cx - 30, base_y - 118, cx + 30, base_y - 88], fill=(140, 100, 62))
    draw.ellipse([cx - 150, base_y - 410, cx + 150, base_y - 110], fill=(255, 244, 222))
    draw.ellipse([cx - 120, base_y - 380, cx + 60, base_y - 220], fill=(255, 250, 238))
    draw.line([(cx + 110, base_y - 30), (cx + 320, base_y + 70)], fill=(70, 66, 62), width=6)  # the cord


def main() -> None:
    """Write the five photos."""
    rng = random.Random(7)  # noqa: S311 - a fixed drawing, not a secret

    hero = gradient((18, 14, 48), (62, 30, 96))
    stars(hero, rng, 140, (0, 0, SIZE, 520))
    projector(hero, rng, 400, 600, 1.0)
    hero.save(OUT / "projector-hero.jpg", quality=82, optimize=True)
    # The sister test shop's copy of it, re-uploaded as a reseller does (cropped a little, smaller, saved again): the
    # same photo for the "same product, sold elsewhere" match (unfret.test_shop_photos).
    resold = hero.crop((24, 24, SIZE - 24, SIZE - 24)).resize((640, 640), Image.Resampling.LANCZOS)
    resold.save(OUT / "sister-hero.jpg", quality=72, optimize=True)

    ceiling = gradient((10, 12, 36), (30, 18, 60))
    nebula = Image.new("RGBA", ceiling.size, (0, 0, 0, 0))
    nd = ImageDraw.Draw(nebula)
    for color, box in (
        ((80, 220, 200, 140), (60, 80, 520, 360)),
        ((200, 80, 220, 130), (300, 180, 760, 520)),
        ((90, 120, 255, 120), (120, 380, 560, 700)),
    ):
        nd.ellipse(box, fill=color)
    blurred = nebula.filter(ImageFilter.GaussianBlur(70))
    ceiling.paste(blurred, (0, 0), blurred)
    stars(ceiling, rng, 260, (0, 0, SIZE, SIZE))
    ImageDraw.Draw(ceiling).polygon([(0, 690), (SIZE, 610), (SIZE, SIZE), (0, SIZE)], fill=(24, 20, 40))
    ceiling.save(OUT / "projector-ceiling.jpg", quality=82, optimize=True)

    studio = gradient((246, 244, 250), (226, 222, 236))
    projector(studio, rng, 330, 560, 0.85)
    draw = ImageDraw.Draw(studio)
    draw.rounded_rectangle([560, 420, 650, 640], 30, fill=(40, 42, 56))
    for row in range(4):
        for col in range(2):
            x, y = 578 + col * 34, 450 + row * 44
            draw.ellipse([x, y, x + 20, y + 20], fill=(150, 152, 170) if (row, col) != (0, 0) else (240, 90, 120))
    studio.save(OUT / "projector-remote.jpg", quality=82, optimize=True)

    room = gradient((236, 230, 220), (214, 204, 190))
    ImageDraw.Draw(room).rectangle([0, 620, SIZE, SIZE], fill=(201, 188, 170))  # the nightstand's top
    lamp(room, 400, 640)
    room.save(OUT / "lamp-hero.jpg", quality=82, optimize=True)


if __name__ == "__main__":
    main()

"""
Draw the test shop's product photos (``src/unfret/static/test-shop/*.jpg``): a made-up star projector, no real product.

Run once and commit the output: ``uv run scripts/test_shop_images.py``. The drawings are deterministic (fixed seed).
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


def main() -> None:
    """Write the three photos."""
    rng = random.Random(7)  # noqa: S311 - a fixed drawing, not a secret

    hero = gradient((18, 14, 48), (62, 30, 96))
    stars(hero, rng, 140, (0, 0, SIZE, 520))
    projector(hero, rng, 400, 600, 1.0)
    hero.save(OUT / "projector-hero.jpg", quality=82, optimize=True)

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


if __name__ == "__main__":
    main()

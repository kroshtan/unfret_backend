"""
Perceptual image hashes (dHash), so the same photo under different URLs or sizes maps to one cache key.

Images are untrusted: only common web formats are decoded, Pillow's decompression-bomb guard stays on, and the
image is reduced to a 9x8 grayscale thumbnail before anything else is done with it.
"""

import io

from PIL import Image

_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]
_MAX_PIXELS = 50_000_000


def dhash(data: bytes) -> str:
    """
    Compute a 64-bit difference hash of an image.

    :param data: The image file bytes.
    :return: 16 hex characters; visually identical images (resized, recompressed) give the same or a close hash.
    :raises ValueError: If the bytes are not a supported image or the image is unreasonably large.
    """
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as image:
            if image.width * image.height > _MAX_PIXELS:
                raise ValueError("image too large")
            image.draft("L", (64, 64))
            small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"not a supported image: {type(exc).__name__}") from exc
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return f"{bits:016x}"

"""
Unfret's own image index: whether a product photo is the same photo as one seen on another shop or listing.

Precision comes first: a false "this photo is used elsewhere" is worse than a missed one. So a match needs
independent fingerprints to agree:

1. perceptual hashes: a 64-bit pHash (DCT) for retrieval, with a dHash (gradients) that must agree as well,
2. an image embedding (DINOv2-small, Apache-2.0, run locally on the CPU via ONNX Runtime; no image leaves the
   server), compared by cosine similarity, and
3. a coarse colour signature, since the first two barely see colour: a recoloured product is another photo.

The thresholds (:data:`THRESHOLDS`) are tuned on synthetic pairs (``python -m evals.image_index_eval``): edited
copies of a photo (resized, recompressed, lightly cropped, padded, brightened, watermarked) must match, distinct
product photos must not, at a false-match rate below 0.1% per query against an index of 100,000 photos.

Photos that can't tell products apart are left out before any of that: small images, banners, blank or nearly
blank images. Images are untrusted input: only common web formats are decoded, Pillow's decompression-bomb guard
stays on.
"""

import hashlib
import io
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from PIL import Image, ImageOps

from unfret.config import settings

logger = structlog.get_logger("unfret")

_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]
_MAX_PIXELS = 50_000_000
MIN_SIDE = 200  # smaller images are icons and thumbnails, not product photos
MAX_ASPECT = 2.5  # wider or taller is a banner
MIN_CONTRAST = 8.0  # grayscale standard deviation below this is a blank image
MAX_BLANK_SHARE = 0.97  # an image this white (or uniform) shows too little to identify

EMBEDDING_DIM = 384
_INPUT = 224
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# DINOv2-small (facebook/dinov2-small, Apache-2.0) exported to ONNX and int8-quantised by onnx-community, pinned.
MODEL_URL = (
    "https://huggingface.co/onnx-community/dinov2-small/resolve/8b1f705a3a7f6f062f6bdd21986c1583d3ef105d"
    "/onnx/model_int8.onnx"
)
MODEL_SHA256 = "dfce54a839b491f395c516350ebb4a78f947e9170a6beac0f2bc5638e0f09d61"


@dataclass(frozen=True)
class Thresholds:
    """When two photos are the same photo: every condition must hold."""

    phash: int  # at most this many differing pHash bits
    dhash: int  # at most this many differing dHash bits
    cosine: float  # at least this embedding similarity
    colour: float = 3.0  # at most this mean difference of the colour grids (a recoloured photo is another photo)


# Tuned by evals/image_index_eval.py; see docs/image-index.md for the measured false-match rate and recall.
THRESHOLDS = Thresholds(phash=10, dhash=8, cosine=0.96, colour=3.0)
COLOUR_GRID = 4  # the colour signature: the photo's Lab colour averaged over a 4x4 grid


class NotDiscriminativeError(ValueError):
    """The image can't tell products apart (an icon, a banner, a blank image)."""


@dataclass(frozen=True)
class ImagePrint:
    """
    A photo's fingerprints.

    Two 64-bit perceptual hashes of its grayscale (signed, for BIGINT columns), an int8 embedding, and a colour
    signature, because the hashes and the embedding barely see colour or overall lightness: the same sofa in beige
    and in black would otherwise be "the same photo".
    """

    phash: int
    dhash: int
    embedding: bytes  # EMBEDDING_DIM int8 values of the L2-normalised embedding times 127
    colour: bytes  # 3 x COLOUR_GRID² int8 values per grid cell: Lab lightness minus 128, a and b

    def vector(self) -> np.ndarray:
        """
        The embedding as a float vector.

        :return: Shape ``(EMBEDDING_DIM,)``, unit length up to quantisation.
        """
        return np.frombuffer(self.embedding, dtype=np.int8).astype(np.float32) / 127.0


def _signed(bits: int) -> int:
    return bits - (1 << 64) if bits >= 1 << 63 else bits


def hamming(a: int, b: int) -> int:
    """
    The number of differing bits between two 64-bit hashes.

    :param a: One hash (signed or unsigned).
    :param b: The other.
    :return: 0–64.
    """
    return ((a ^ b) & (1 << 64) - 1).bit_count()


def _bits(flags: np.ndarray) -> int:
    value = 0
    for flag in flags.ravel():
        value = (value << 1) | int(bool(flag))
    return _signed(value)


_DCT = np.array(
    [[np.cos(np.pi * (2 * x + 1) * u / 64) for x in range(32)] for u in range(32)], dtype=np.float64
)  # 32-point DCT-II basis


def phash(gray: Image.Image) -> int:
    """
    The 64-bit DCT hash of a grayscale image: the signs of its 8x8 lowest frequencies against their median.

    :param gray: A grayscale image.
    :return: The hash, signed.
    """
    pixels = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    low = (_DCT @ pixels @ _DCT.T)[:8, :8]
    return _bits(low > np.median(low))


def dhash(gray: Image.Image) -> int:
    """
    The 64-bit difference hash of a grayscale image: whether each pixel is brighter than its right neighbour.

    :param gray: A grayscale image.
    :return: The hash, signed.
    """
    pixels = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    return _bits(pixels[:, :-1] > pixels[:, 1:])


def colour_signature(image: Image.Image) -> bytes:
    """
    The photo's colours, coarsely: its Lab colour averaged over a grid, on the white-padded square.

    :param image: An RGB image.
    :return: ``3 * COLOUR_GRID**2`` bytes (int8): lightness minus 128, then a and b as Pillow stores them (signed).
    """
    lab = np.asarray(_square(image).convert("LAB").resize((COLOUR_GRID, COLOUR_GRID), Image.Resampling.BOX))
    lightness = (lab[:, :, :1].astype(np.int16) - 128).astype(np.int8)
    chroma = lab[:, :, 1:].astype(np.uint8).view(np.int8)  # Pillow's a and b are signed bytes
    return np.concatenate([lightness, chroma], axis=2).tobytes()


def colour_distance(a: ImagePrint, b: ImagePrint) -> float:
    """
    How different two photos' colours are: the mean absolute difference of their colour signatures.

    :param a: One print.
    :param b: The other.
    :return: 0 for the same colours; a recoloured product scores tens.
    """
    x = np.frombuffer(a.colour, dtype=np.int8).astype(np.int16)
    y = np.frombuffer(b.colour, dtype=np.int8).astype(np.int16)
    return float(np.abs(x - y).mean())


def load(data: bytes, min_side: int = MIN_SIDE) -> Image.Image:
    """
    Decode an untrusted image and check that it can identify a product.

    :param data: The image file bytes.
    :param min_side: Smaller images are rejected (catalogue thumbnails of known product photos may go lower).
    :return: The image in RGB, transparency flattened onto white.
    :raises ValueError: If it isn't a supported image or is unreasonably large.
    :raises NotDiscriminativeError: If it is too small, a banner, or (nearly) blank.
    """
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as opened:
            if opened.width * opened.height > _MAX_PIXELS:
                raise ValueError("image too large")
            opened.draft("RGB", (1024, 1024))
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA", "P"}:
                image = image.convert("RGBA")
                flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
                image = Image.alpha_composite(flat, image)
            image = image.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"not a supported image: {type(exc).__name__}") from exc
    width, height = image.size
    if min(width, height) < min_side:
        raise NotDiscriminativeError("too small")
    if max(width, height) / min(width, height) > MAX_ASPECT:
        raise NotDiscriminativeError("a banner")
    gray = np.asarray(image.convert("L").resize((64, 64)), dtype=np.float32)
    if gray.std() < MIN_CONTRAST:
        raise NotDiscriminativeError("blank")
    if (np.abs(gray - np.median(gray)) < 6).mean() > MAX_BLANK_SHARE:
        raise NotDiscriminativeError("nearly blank")
    return image


def _square(image: Image.Image) -> Image.Image:
    # Pad to a white square rather than crop, so a reseller's re-padding or light crop changes little.
    side = max(image.size)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return canvas


class Embedder:
    """The image embedding model, loaded once per process on first use."""

    def __init__(self, path: Path) -> None:
        """
        Use the model at ``path`` (loaded lazily).

        :param path: The ONNX file.
        """
        self.path = path
        self._session: Any = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        """
        Whether the model file is there.

        :return: ``True`` if it can be loaded.
        """
        return self.path.is_file()

    def _load(self) -> Any:
        import onnxruntime as ort  # noqa: PLC0415 - heavy; only processes that embed images load it

        options = ort.SessionOptions()
        options.intra_op_num_threads = settings.image_threads
        options.inter_op_num_threads = 1
        return ort.InferenceSession(str(self.path), options, providers=["CPUExecutionProvider"])

    def embed(self, image: Image.Image) -> bytes:
        """
        The image's embedding: the class token of DINOv2, L2-normalised, as int8.

        :param image: An RGB image.
        :return: ``EMBEDDING_DIM`` bytes.
        """
        with self._lock:
            if self._session is None:
                self._session = self._load()
        resized = _square(image).resize((_INPUT, _INPUT), Image.Resampling.BICUBIC)
        x = (np.asarray(resized, dtype=np.float32) / 255.0 - _MEAN) / _STD
        hidden = self._session.run(None, {"pixel_values": x.transpose(2, 0, 1)[None].astype(np.float32)})[0]
        vector = hidden[0, 0]
        vector = vector / (np.linalg.norm(vector) or 1.0)
        return bytes(np.clip(np.round(vector * 127), -127, 127).astype(np.int8).tobytes())


_embedder: Embedder | None = None


def embedder() -> Embedder:
    """
    The process-wide embedder for ``UNFRET_IMAGE_MODEL_PATH``.

    :return: The embedder (possibly without a model file: check :meth:`Embedder.available`).
    """
    global _embedder  # noqa: PLW0603 - one model per process
    if _embedder is None or _embedder.path != Path(settings.image_model_path):
        _embedder = Embedder(Path(settings.image_model_path))
    return _embedder


def fingerprint(data: bytes, model: Embedder | None = None, min_side: int = MIN_SIDE) -> ImagePrint:
    """
    Both fingerprints of a photo. Raises like :func:`load` for unsupported or non-discriminative images.

    :param data: The image file bytes.
    :param model: The embedder (default: :func:`embedder`).
    :param min_side: See :func:`load`.
    :return: The fingerprints.
    """
    image = load(data, min_side)
    gray = _square(image).convert("L")
    return ImagePrint(phash(gray), dhash(gray), (model or embedder()).embed(image), colour_signature(image))


def cosine(a: ImagePrint, b: ImagePrint) -> float:
    """
    The embedding similarity of two photos.

    :param a: One print.
    :param b: The other.
    :return: −1 to 1.
    """
    return float(a.vector() @ b.vector())


def same_photo(a: ImagePrint, b: ImagePrint, thresholds: Thresholds = THRESHOLDS) -> bool:
    """
    Whether two prints are the same photo: both hashes close, the embeddings similar and the colours alike.

    :param a: One print.
    :param b: The other.
    :param thresholds: The limits.
    :return: ``True`` only if every fingerprint agrees.
    """
    return (
        hamming(a.phash, b.phash) <= thresholds.phash
        and hamming(a.dhash, b.dhash) <= thresholds.dhash
        and cosine(a, b) >= thresholds.cosine
        and colour_distance(a, b) <= thresholds.colour
    )


def ensure_model(path: Path) -> Path:
    """
    Download the pinned model to ``path`` unless it is already there, verifying its SHA-256.

    :param path: Where the model goes.
    :return: The path.
    :raises ValueError: If the download doesn't match the pinned hash.
    """
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(MODEL_URL, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("image model download doesn't match its pinned SHA-256")
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


if __name__ == "__main__":
    import sys

    print(ensure_model(Path(sys.argv[1] if len(sys.argv) > 1 else settings.image_model_path)))  # noqa: T201

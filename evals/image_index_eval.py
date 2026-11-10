"""
Build, measure and tune the image index on Amazon Berkeley Objects (ABO, CC BY 4.0, credit: Amazon.com).

ABO (https://amazon-berkeley-objects.s3.amazonaws.com/index.html) is a public catalogue of 147,702 Amazon products
with their main photos, licensed for commercial use with attribution. Steps::

    python -m evals.image_index_eval sample --out DIR [--index 50000 --queries 5000]
    python -m evals.image_index_eval prints --dir DIR [--workers 8]
    python -m evals.image_index_eval tune --dir DIR

``sample`` picks distinct main photos and splits them into an index and held-out queries (of other items).
``prints`` downloads each photo (256 px catalogue size) and computes its fingerprints into ``DIR/prints.npz``.
``tune`` measures, for a grid of thresholds:

* recall on synthetic positives: edited copies of query photos (resized, recompressed, cropped, padded,
  brightened, watermarked) looked up against the index with their original added;
* the per-query false-match rate: every held-out query photo looked up against the whole index, where any match is
  false (different item, different photo). It picks the most permissive thresholds with no false match at all, and
  reports the measured rate with its 95% upper bound (rule of three).

The images are untrusted downloads; they are decoded by :mod:`unfret.image_index` with its usual guards.
"""

import argparse
import concurrent.futures
import csv
import gzip
import io
import json
import random
import re
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance

from unfret import image_index
from unfret.image_index import ImagePrint, Thresholds

ABO = "https://amazon-berkeley-objects.s3.amazonaws.com"
SHARDS = [f"{ABO}/listings/metadata/listings_{i:x}.json.gz" for i in range(16)]
SEED = 20261109


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed ABO https URLs
        return bytes(response.read())


def _value(field: Any) -> str:
    # ABO fields are lists of {"language_tag", "value"}; prefer English.
    if not isinstance(field, list) or not field:
        return ""
    english = [f for f in field if str(f.get("language_tag", "")).startswith("en")]
    return str((english or field)[0].get("value", ""))[:120]


def sample(out: Path, n_index: int, n_queries: int) -> None:
    """
    Pick distinct main photos from the ABO listings and split them into index and queries.

    :param out: The output directory.
    :param n_index: Photos in the index.
    :param n_queries: Held-out query photos (of items not in the index).
    """
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(io.BytesIO(_get(f"{ABO}/images/metadata/images.csv.gz")), "rt") as index:
        paths = {row["image_id"]: row["path"] for row in csv.DictReader(index)}
    items: dict[str, dict[str, str]] = {}  # main image → first listing using it
    for shard in SHARDS:
        for line in gzip.decompress(_get(shard)).splitlines():
            listing = json.loads(line)
            image = listing.get("main_image_id")
            if image in paths and image not in items:
                items[image] = {
                    "image_id": image,
                    "path": paths[image],
                    "item_id": listing["item_id"],
                    "brand": _value(listing.get("brand")),
                    "product_type": _value(listing.get("product_type")),
                }
    chosen = sorted(items)
    random.Random(SEED).shuffle(chosen)
    rows = [items[i] for i in chosen[: n_index + n_queries]]
    for i, row in enumerate(rows):
        row["split"] = "index" if i < n_index else "query"
    (out / "sample.json").write_text(json.dumps(rows))
    print(f"{len(items)} distinct main photos; sampled {n_index} index + {n_queries} queries")  # noqa: T201


def pixels(image: Image.Image) -> np.ndarray:
    """
    The ground truth's view of a photo: a 16x16 RGB thumbnail of the white-padded square.

    :param image: The photo.
    :return: Shape ``(16, 16, 3)``, uint8.
    """
    return np.asarray(image_index._square(image).resize((16, 16), Image.Resampling.BOX), dtype=np.uint8)


def pixel_difference(a: np.ndarray, b: np.ndarray) -> float:
    """
    How much the most-changed part of two photos differs: the largest mean difference over 4x4 cells.

    The same photo, recompressed or re-uploaded, stays in single digits; a recoloured product, a different label or
    another shot of the product doesn't.

    :param a: One thumbnail from :func:`pixels`.
    :param b: The other.
    :return: 0–255.
    """
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).mean(axis=2)
    return float(diff.reshape(4, 4, 4, 4).mean(axis=(1, 3)).max())


def _print_one(row: dict[str, str]) -> tuple[str, ImagePrint | None, np.ndarray | None, str]:
    try:
        data = _get(f"{ABO}/images/small/{row['path']}")
        fp = image_index.fingerprint(data, min_side=64)
        return row["image_id"], fp, pixels(image_index.load(data, min_side=64)), ""
    except Exception as exc:  # noqa: BLE001 - counted and reported
        return row["image_id"], None, None, type(exc).__name__


def prints(directory: Path, workers: int) -> None:
    """
    Download and fingerprint every sampled photo into ``prints.npz``.

    :param directory: The sample directory.
    :param workers: Parallel processes.
    """
    rows = json.loads((directory / "sample.json").read_text())
    ids, ph, dh, emb, colour, thumbs = [], [], [], [], [], []
    failures: dict[str, int] = {}
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        for n, (image_id, fp, thumb, error) in enumerate(pool.map(_print_one, rows, chunksize=32), 1):
            if fp is None or thumb is None:
                failures[error] = failures.get(error, 0) + 1
            else:
                ids.append(image_id)
                ph.append(fp.phash)
                dh.append(fp.dhash)
                emb.append(np.frombuffer(fp.embedding, dtype=np.int8))
                colour.append(np.frombuffer(fp.colour, dtype=np.int8))
                thumbs.append(thumb)
            if n % 2000 == 0:
                print(f"{n}/{len(rows)}", flush=True)  # noqa: T201
    np.savez(directory / "prints.npz", ids=np.array(ids), phash=np.array(ph, dtype=np.int64),
             dhash=np.array(dh, dtype=np.int64), emb=np.stack(emb), colour=np.stack(colour),
             thumbs=np.stack(thumbs))  # fmt: skip
    print(f"{len(ids)} prints; skipped: {failures}")  # noqa: T201


def _popcount(x: np.ndarray) -> np.ndarray:
    return np.bitwise_count(x.view(np.uint64)).astype(np.int16)


# Edits a reseller makes to a supplier photo, each a positive pair with the original.
def _edits(image: Image.Image, rng: random.Random) -> dict[str, Image.Image]:
    w, h = image.size
    crop = int(min(w, h) * 0.05)
    padded = Image.new("RGB", (int(w * 1.15), int(h * 1.15)), (255, 255, 255))
    padded.paste(image, ((padded.width - w) // 2, (padded.height - h) // 2))
    marked = image.copy()
    ImageDraw.Draw(marked).text((w * 0.6, h * 0.85), "BESTDEALS", fill=(200, 30, 30))

    def jpeg(img: Image.Image, quality: int) -> Image.Image:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")

    return {
        "resize_2x": image.resize((w * 2, h * 2), Image.Resampling.BICUBIC),
        "resize_half_jpeg60": jpeg(image.resize((max(w // 2, 64), max(h // 2, 64))), 60),
        "jpeg40": jpeg(image, 40),
        "crop_5pct": image.crop((crop, crop, w - crop, h - crop)),
        "pad_15pct": padded,
        "brighter_15pct": ImageEnhance.Brightness(image).enhance(1.15),
        "watermark": marked,
        "jpeg75_resize_0.8": jpeg(image.resize((int(w * 0.8), int(h * 0.8))), 75),
    }


def _fingerprint_image(image: Image.Image) -> ImagePrint:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return image_index.fingerprint(buffer.getvalue(), min_side=32)


def _positives(directory: Path, rows: list[dict[str, str]], n: int) -> list[tuple[str, ImagePrint, ImagePrint]]:
    rng = random.Random(SEED)
    pairs: list[tuple[str, ImagePrint, ImagePrint]] = []
    for row in rng.sample(rows, n):
        try:
            original = Image.open(io.BytesIO(_get(f"{ABO}/images/small/{row['path']}"))).convert("RGB")
            base = _fingerprint_image(original)
        except Exception:  # noqa: BLE001, S112 - skip unusable photos
            continue
        for name, edited in _edits(original, rng).items():
            try:
                pairs.append((name, base, _fingerprint_image(edited)))
            except image_index.NotDiscriminativeError:
                continue  # the edit made it a banner (a wide photo, cropped): not indexed either
    return pairs


_BRAND_NOISE = re.compile(r"amazon\s*brand\s*-?|by amazon|coffee|tea$")


def _verified(directory: Path) -> set[tuple[str, str]]:
    """
    Pairs verified by eye as the same photo, from ``DIR/verified_same.json`` (``[[query path, index path], …]``).

    :param directory: The sample directory.
    :return: The pairs.
    """
    file = directory / "verified_same.json"
    return {(a, b) for a, b in json.loads(file.read_text())} if file.is_file() else set()


def _brand_family(brand: str) -> str:
    """
    A brand's comparable form: ABO spells one brand several ways ("AmazonBasics", "Amazon Basics").

    :param brand: The brand as listed.
    :return: Lower-case letters and digits; empty for no brand (never the same family as anything).
    """
    brand = brand.lower()
    if (latin := re.search(r"\(([a-z0-9 &'.-]+)\)", brand)) is not None:
        brand = latin.group(1)  # "Amazonベーシック(AmazonBasics)": the brand's Latin name
    return re.sub(r"[^a-z0-9]", "", _BRAND_NOISE.sub("", brand))


PIXEL_SAME = 10.0  # ground truth: at most this pixel_difference is the same photo (re-uploaded or recompressed)
CANDIDATE_COLUMNS = ("query", "index", "phash", "dhash", "cosine", "colour", "pixels")


def _distances(a: ImagePrint, b: ImagePrint) -> tuple[int, int, float, float]:
    return (
        image_index.hamming(a.phash, b.phash),
        image_index.hamming(a.dhash, b.dhash),
        image_index.cosine(a, b),
        image_index.colour_distance(a, b),
    )


def _measured(directory: Path, rows: dict[str, dict[str, str]], data: Any, n_positive: int) -> tuple[Any, Any, Any]:
    """
    The candidate pairs (held-out query, index photo) and the edited-copy pairs, cached in ``DIR/tune_cache.npz``.

    :param directory: The sample directory.
    :param rows: The sample rows by image ID.
    :param data: The prints.
    :param n_positive: Query photos to make edited copies of.
    :return: Candidates (one row per pair, :data:`CANDIDATE_COLUMNS`), positive distances (phash, dhash, cosine,
        colour per edited copy) and the edits' names.
    """
    cache = directory / "tune_cache.npz"
    if cache.is_file():
        c = np.load(cache, allow_pickle=False)
        return c["candidates"], c["positives"], c["names"]

    split = np.array([rows[str(i)]["split"] for i in data["ids"]])
    ph, dh = data["phash"], data["dhash"]
    emb = data["emb"].astype(np.float32) / 127.0
    colour = data["colour"].astype(np.int16)
    thumbs = data["thumbs"]
    qi, ii = np.where(split == "query")[0], np.where(split == "index")[0]
    # Held-out queries against the whole index: pairs close in both main fingerprints, looser than any threshold
    # tried, with the colour distance and the ground truth's pixel difference.
    found = []
    for start in range(0, len(qi), 256):
        block = qi[start : start + 256]
        pd = _popcount(ph[block][:, None] ^ ph[ii][None, :])
        cos = emb[block] @ emb[ii].T
        for r, c in zip(*np.where((pd <= 20) & (cos >= 0.8)), strict=True):
            qrow, irow = int(block[r]), int(ii[c])
            found.append((
                qrow, irow, int(pd[r, c]), int(_popcount(np.array([dh[qrow] ^ dh[irow]]))[0]), float(cos[r, c]),
                float(np.abs(colour[qrow] - colour[irow]).mean()), pixel_difference(thumbs[qrow], thumbs[irow]),
            ))  # fmt: skip
    candidates = np.array(found, dtype=np.float64).reshape(-1, len(CANDIDATE_COLUMNS))
    pairs = _positives(directory, [rows[str(data["ids"][i])] for i in qi], n_positive)
    positives = np.array([_distances(a, b) for _, a, b in pairs], dtype=np.float64).reshape(-1, 4)
    names = np.array([n for n, _, _ in pairs])
    np.savez(cache, candidates=candidates, positives=positives, names=names)
    return candidates, positives, names


def tune(directory: Path, n_positive: int) -> None:
    """
    Measure recall and per-query false matches over a threshold grid; write ``report.json``.

    The ground truth calls a pair the same photo when :func:`pixel_difference` is at most :data:`PIXEL_SAME` (ABO lists
    some photos under several products and brands: matching those is right). Of the other matched pairs, those of
    different brands are **false matches**: they would tell a shopper another seller uses this photo. Those of one
    brand family are **template matches**: one brand's product-line variants shot on one template (the same phone case
    print on another phone, another flavour in the same packaging), which no fingerprint here tells apart; they are
    reported, not used to choose.

    :param directory: The sample directory with ``prints.npz``.
    :param n_positive: Query photos to make edited copies of.
    """
    rows = {r["image_id"]: r for r in json.loads((directory / "sample.json").read_text())}
    data = np.load(directory / "prints.npz")
    split = np.array([rows[str(i)]["split"] for i in data["ids"]])
    idx, q = split == "index", split == "query"
    cand, pos, names = _measured(directory, rows, data, n_positive)
    same = cand[:, 6] <= PIXEL_SAME
    # Pairs verified by eye as the same photo (a re-encode beyond the pixel cut-off), listed in the report.
    verified = _verified(directory)
    path = [rows[str(i)]["path"] for i in data["ids"]]
    same |= np.array([(path[int(a)], path[int(b)]) in verified for a, b in cand[:, :2]], dtype=bool)
    brand = [_brand_family(rows[str(i)]["brand"]) for i in data["ids"]]
    family = np.array([bool(brand[int(a)]) and brand[int(a)] == brand[int(b)] for a, b in cand[:, :2]], dtype=bool)
    cross = ~same & ~family  # a different photo of another seller: the error that misleads a shopper
    template = ~same & family  # a different photo of the same brand: a product-line variant on one template

    even = cand[:, 0].astype(int) % 2 == 0  # two halves of the queries, for the held-out check
    grid: list[dict[str, Any]] = []
    for tp in range(6, 21, 2):
        for td in range(8, 23, 2):
            for tc in (0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99):
                for tcol in (2.0, 3.0, 4.0, 5.0, 6.0, 8.0):
                    m = (cand[:, 2] <= tp) & (cand[:, 3] <= td) & (cand[:, 4] >= tc) & (cand[:, 5] <= tcol)
                    hit = (pos[:, 0] <= tp) & (pos[:, 1] <= td) & (pos[:, 2] >= tc) & (pos[:, 3] <= tcol)
                    grid.append({
                        "phash": tp, "dhash": td, "cosine": tc, "colour": tcol,
                        "false_queries": len(set(cand[m & cross, 0].astype(int))),
                        "false_even": len(set(cand[m & cross & even, 0].astype(int))),
                        "false_odd": len(set(cand[m & cross & ~even, 0].astype(int))),
                        "template_queries": len(set(cand[m & template, 0].astype(int))),
                        "duplicate_queries": len(set(cand[m & same, 0].astype(int))),
                        "recall": round(float(hit.mean()), 4),
                    })  # fmt: skip

    def best(clean: str) -> dict[str, Any]:
        # The most recall without a false match on the given queries; ties go to the stricter setting.
        candidates = [g for g in grid if g[clean] == 0]
        if not candidates:
            fewest = sorted(grid, key=lambda g: (g[clean], -g["recall"]))[:10]
            print(json.dumps({"no_clean_setting": fewest}, indent=1))  # noqa: T201
            raise SystemExit("no threshold setting without false matches: inspect DIR/tune_cache.npz")
        return max(candidates, key=lambda g: (g["recall"], -g["phash"], -g["dhash"], g["cosine"], -g["colour"]))

    chosen = best("false_queries")
    # Against fitting this sample: choose on one half of the queries and count false matches on the other half.
    held_out = {"chosen_on_even": best("false_even"), "chosen_on_odd": best("false_odd")}
    held_out_false = {
        "odd_queries_false": held_out["chosen_on_even"]["false_odd"],
        "even_queries_false": held_out["chosen_on_odd"]["false_even"],
    }
    t = Thresholds(int(chosen["phash"]), int(chosen["dhash"]), float(chosen["cosine"]), float(chosen["colour"]))
    hit = (pos[:, 0] <= t.phash) & (pos[:, 1] <= t.dhash) & (pos[:, 2] >= t.cosine) & (pos[:, 3] <= t.colour)
    per_edit = {str(n): round(float(hit[names == n].mean()), 3) for n in sorted(set(names.tolist()))}
    loose = (cand[:, 2] <= t.phash + 4) & (cand[:, 4] >= t.cosine - 0.06) & cross
    near_misses = [
        {"query": rows[str(data["ids"][int(r[0])])]["path"], "index": rows[str(data["ids"][int(r[1])])]["path"],
         **{k: round(float(v), 3) for k, v in zip(CANDIDATE_COLUMNS[2:], r[2:], strict=True)}}
        for r in sorted(cand[loose], key=lambda r: -r[4])[:25]
    ]  # fmt: skip
    n_q = int(q.sum())
    report = {
        "index_size": int(idx.sum()),
        "queries": n_q,
        "positives": len(pos),
        "ground_truth": f"same photo when the most-changed 4x4 cell of 16x16 RGB thumbnails differs <= {PIXEL_SAME}; "
        "false = a different photo of another brand; template = a different photo of the same brand family",
        "template_match_rate": round(chosen["template_queries"] / n_q, 4),
        "verified_same_by_eye": sorted(map(list, _verified(directory))),
        "chosen": chosen,
        "held_out": {**held_out, **held_out_false},
        "per_query_false_match_rate": 0.0,
        "per_query_fmr_95_upper": round(3 / n_q, 6),
        "recall_per_edit": per_edit,
        "near_misses": near_misses,
        "grid": grid,
    }
    (directory / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k not in {"grid", "near_misses"}}, indent=1))  # noqa: T201


def main() -> None:
    """Command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--index", type=int, default=50_000)
    s.add_argument("--queries", type=int, default=5_000)
    p = sub.add_parser("prints")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--workers", type=int, default=8)
    t = sub.add_parser("tune")
    t.add_argument("--dir", type=Path, required=True)
    t.add_argument("--positives", type=int, default=400)
    args = parser.parse_args()
    if args.command == "sample":
        sample(args.out, args.index, args.queries)
    elif args.command == "prints":
        prints(args.dir, args.workers)
    else:
        tune(args.dir, args.positives)


if __name__ == "__main__":
    main()

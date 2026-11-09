"""
Seeding the image index with catalogue photos: an operator command, run by hand against a database.

The seed is the main photos of the public Amazon Berkeley Objects catalogue (ABO,
https://amazon-berkeley-objects.s3.amazonaws.com/index.html, © Amazon.com, CC BY 4.0), as fingerprints. A seed file
holds only derived data: the fingerprints, each product's ASIN and brand, and the path of ABO's own copy of the photo
(for thumbnails). It is built offline (``python -m evals.image_index_eval``, then ``build``), is never published or
baked into the container image, and is imported into a database by an operator (docs/image-index.md)::

    python -m veridex.image_seed import abo-seed.npz --source abo --sha256 <expected hex>
    python -m veridex.image_seed remove --source abo

Importing is idempotent (photos already there are skipped) and every row is marked with its source, so a seed can be
removed again without touching the photos seen in analyses. There is no published dataset of AliExpress or Temu
photos that may be used commercially, so supplier photos enter the index only from analyses.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

import asyncpg
import numpy as np
import structlog

from veridex.config import settings

logger = structlog.get_logger("veridex")

SeedSource = Literal["abo"]
SOURCES: dict[str, str] = {"abo": "https://amazon-berkeley-objects.s3.amazonaws.com/images/small/"}  # photo base URL
DOMAIN = "amazon"  # ABO products are Amazon listings: the same shop family as analysed Amazon pages


def listing_key(asin: str) -> bytes:
    """
    The listing key of an Amazon product, as :func:`veridex.image_store.listing` computes it for an Amazon page.

    :param asin: The ASIN.
    :return: 32 bytes.
    """
    return hashlib.sha256(f"{DOMAIN}|asin:{asin.upper()}".encode()).digest()


def sha256_of(path: Path) -> str:
    """
    A file's SHA-256.

    :param path: The file.
    :return: Lower-case hex.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(directory: Path, out: Path) -> int:
    """
    Turn the evaluation's prints into a seed file.

    :param directory: The ``evals.image_index_eval`` directory (``sample.json`` and ``prints.npz``).
    :param out: The seed file to write.
    :return: The number of photos.
    """
    rows = {r["image_id"]: r for r in json.loads((directory / "sample.json").read_text())}
    with np.load(directory / "prints.npz", allow_pickle=False) as npz:
        data = {name: npz[name] for name in npz.files}
    keep: dict[str, int] = {}  # one photo per ASIN (a few ABO listings share one), the first
    for n, image_id in enumerate(data["ids"]):
        keep.setdefault(rows[str(image_id)]["item_id"].upper(), n)
    order = sorted(keep.values())
    meta = [rows[str(data["ids"][n])] for n in order]
    np.savez_compressed(
        out,
        asin=np.array([m["item_id"] for m in meta]),
        path=np.array([m["path"] for m in meta]),
        brand=np.array([m["brand"] for m in meta]),
        phash=data["phash"][order],
        dhash=data["dhash"][order],
        emb=data["emb"][order],
        colour=data["colour"][order],
    )
    return len(meta)


async def import_seed(pool: asyncpg.Pool, path: Path, source: SeedSource, expected_sha256: str | None = None) -> int:
    """
    Import a seed file's photos that aren't in the index yet, marked with their source.

    :param pool: The connection pool.
    :param path: The seed file.
    :param source: Where the photos come from (``abo``).
    :param expected_sha256: The file's expected SHA-256; checked before anything is read when given.
    :return: The number of rows of this source in the index afterwards.
    :raises ValueError: If the file doesn't match the expected hash.
    """
    if expected_sha256 and sha256_of(path) != expected_sha256.lower():
        raise ValueError(f"{path} doesn't match the expected SHA-256")
    with np.load(path, allow_pickle=False) as npz:
        data = {name: npz[name] for name in npz.files}  # an NpzFile decompresses an array on every access
    base = SOURCES[source]
    records = [
        (
            DOMAIN,
            listing_key(str(data["asin"][i])),
            int(data["phash"][i]),
            int(data["dhash"][i]),
            data["emb"][i].astype(np.int8).tobytes(),
            data["colour"][i].astype(np.int8).tobytes(),
            base + str(data["path"][i]),
            str(data["brand"][i])[:120] or None,
        )
        for i in range(len(data["asin"]))
    ]
    columns = ["domain", "listing_key", "phash", "dhash", "embedding", "colour", "image_url", "label"]
    async with pool.acquire() as conn, conn.transaction():
        # COPY into a scratch table, then insert what isn't there yet: about a second for 50,000 photos.
        await conn.execute(
            "CREATE TEMPORARY TABLE seed_prints (domain TEXT, listing_key BYTEA, phash BIGINT, dhash BIGINT,"
            " embedding BYTEA, colour BYTEA, image_url TEXT, label TEXT) ON COMMIT DROP"
        )
        await conn.copy_records_to_table("seed_prints", records=records, columns=columns)
        await conn.execute(
            "INSERT INTO image_prints (source, domain, listing_key, phash, dhash, embedding, colour, image_url,"
            " label, first_seen, last_seen) SELECT $1, domain, listing_key, phash, dhash, embedding, colour,"
            " image_url, LEFT(label, 120), CURRENT_DATE, CURRENT_DATE FROM seed_prints"
            " ON CONFLICT (domain, listing_key) DO NOTHING",
            source,
        )
        count: int = await conn.fetchval("SELECT COUNT(*) FROM image_prints WHERE source = $1", source)
    logger.info("image_seed_imported", source=source, rows=count)
    return count


async def remove_seed(pool: asyncpg.Pool, source: SeedSource) -> int:
    """
    Delete every photo of a seed source (photos seen in analyses stay).

    :param pool: The connection pool.
    :param source: The source (``abo``).
    :return: The number of rows deleted.
    """
    status: str = await pool.execute("DELETE FROM image_prints WHERE source = $1", source)
    removed = int(status.rsplit(maxsplit=1)[-1])
    logger.info("image_seed_removed", source=source, rows=removed)
    return removed


def main(argv: list[str] | None = None) -> None:
    """
    Command line: ``build``, ``import`` and ``remove``.

    :param argv: The arguments (default: the process's).
    """
    parser = argparse.ArgumentParser(description="Seed the image index (ABO, © Amazon.com, CC BY 4.0).")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a seed file from an evals.image_index_eval directory")
    b.add_argument("--dir", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    i = sub.add_parser("import", help="import a seed file into VERIDEX_POSTGRES_URI")
    i.add_argument("file", type=Path)
    i.add_argument("--source", choices=sorted(SOURCES), required=True)
    i.add_argument("--sha256", help="the file's expected SHA-256 (hex)")
    r = sub.add_parser("remove", help="delete a seed source's photos from VERIDEX_POSTGRES_URI")
    r.add_argument("--source", choices=sorted(SOURCES), required=True)
    args = parser.parse_args(argv)

    if args.command == "build":
        print(build(args.dir, args.out))  # noqa: T201
        return
    if args.command == "import" and args.sha256 and sha256_of(args.file) != args.sha256.lower():
        sys.exit(f"{args.file} doesn't match the expected SHA-256; nothing was imported")

    async def run() -> int:
        pool = await asyncpg.create_pool(settings.postgres_uri, min_size=1, max_size=2)
        try:
            if args.command == "import":
                return await import_seed(pool, args.file, args.source, args.sha256)
            return await remove_seed(pool, args.source)
        finally:
            await pool.close()

    try:
        print(asyncio.run(run()))  # noqa: T201
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()

# Unfret's own image index

`reverse_image` asks one question: is the main product photo **the same photo** as on another shop or listing? It
answers from Unfret's own index, on our own server. No image or page goes to an outside service.

## How a match is decided

Precision comes first: a false "this photo is used elsewhere" is worse than a missed one.

1. **Filters.** Images that can't identify a product are left out: smaller than 200 px, wider or taller than 2.5:1
   (banners), blank or nearly blank. Only the main photo of a listing is used.
2. **Independent fingerprints must all agree** (`unfret.image_index`):
   - perceptual hashes: a 64-bit pHash (DCT) *and* a 64-bit dHash (gradients), each within a Hamming distance;
   - an image embedding: the class token of [DINOv2-small](https://huggingface.co/facebook/dinov2-small)
     (Apache-2.0), int8-quantised ONNX, run on the CPU (about 120 ms per photo on one core), compared by cosine;
   - a colour signature: the photo's Lab colour averaged over a 4x4 grid. The hashes work on grayscale and the
     embedding barely sees colour, so without it the same sofa in black and in beige, or one packaging design in
     two flavours' colours, would be "the same photo". The measurement found exactly those.
3. **What doesn't count** (`unfret.image_store`): the photo's own shop (shops reuse their own photos), the same
   listing (a changed listing is an update, not a duplicate; Amazon country sites are one shop), and a photo found
   on more than 50 shops (a stock image, logo or placeholder, not evidence). Matches are photo to photo, never chained.
4. **Context.** The finding is factual ("the same product photo is on 2 other shops: …"). The shopper verdict turns it
   into a warning (`photo_reused`) only for an unknown brand with other signs of a resold supplier product, never for
   a recognised brand. Up to three thumbnails are served by `GET /v1/images/{id}/thumbnail.jpg`, so the shopper's
   browser never contacts the other shops.

Retrieval is a Postgres scan with a popcount filter on the pHash (about 5 ms for 53,000 rows); the dHash and the
embedding are checked in Python. pgvector isn't needed: with both fingerprints required, the pHash gate loses
nothing. An approximate nearest-neighbour index would only matter for a later, more edit-tolerant mode.

## Thresholds and measurement

Shipped (`unfret.image_index.THRESHOLDS`): **pHash ≤ 10 bits, dHash ≤ 8 bits, cosine ≥ 0.96, colour ≤ 3.0**.
Measured with `python -m evals.image_index_eval` (the full report, without its grid, is
[image-index.json](image-index.json)):

| | |
| --- | --- |
| Index | 48,777 ABO main photos (256 px) |
| Held-out queries | 4,881 photos of other products, each looked up against the whole index |
| **False matches** (another brand's different photo) | **0 of 4,881 queries: under 0.061% per query (95% upper bound)** |
| Held-out check | chosen on the even half, 0 false on the odd half; chosen on the odd half (a looser setting, cosine 0.92), 4 false on the even half (0.16%), so the shipped setting stays on the strict side of that edge |
| Template matches (the same brand's product-line variant) | 683 queries (14%): see below |
| Same photo matched (ABO re-lists photos) | 1,018 queries |
| Recall, 3,187 edited copies | 54% overall: 2x resize 96%, watermark 95%, JPEG q75 + 80% 72%, JPEG q40 74%, 15% brighter 65%, 5% crop 20%, half size + JPEG q60 7%, 15% white padding 3% |

How "false" is decided. A pair is the same photo when its 16x16 RGB thumbnails differ by at most 10 in their
most-changed 4x4 cell, plus two pairs verified by eye where a re-encode went past that cut-off: one face-mask photo
listed by two unrelated brands, twice ([image-index-verified-same.json](image-index-verified-same.json); exactly the
case the index exists for). Every other matched pair is either a **false match**, when the brands differ (spellings
normalised: "AmazonBasics" = "Amazon Basics"), or a **template match**, when they are one brand.

**Known limitation: template matches.** One brand's product-line variants shot on one template (the same phone-case
print on another phone model, another flavour in the same packaging, the same room photo with another rug) are a
different product but the same picture to every fingerprint here, colour included. ABO is full of them (Amazon's own
brands), so 14% of queries hit one. In use they mostly don't count: a shop's own photos are ignored, and the shopper
warning never fires for a recognised brand. Across two unrelated shops it reads "the same product photo", which for a
supplier's template is what it is.

**What the colour check is for.** Without it the false matches were Amazon's studio photos: the same sofa frame in
black and in beige, the same shoe pose in two colours. The hashes work on grayscale and the embedding barely sees
colour or lightness.

**Recall is deliberately modest.** Exact and lightly edited copies (resized, recompressed, watermarked) are found;
crops and padding mostly aren't, because the pHash moves. The measurement uses 256 px catalogue thumbnails, which
recompression hurts more than full-size shop photos. `tests/unit/test_image_model.py` checks in CI, with the real
model, that the shipped thresholds are the measured ones, that 40 committed ABO photos of different product types
never match each other, and that edited copies still match at least 50% of the time.

## Data: the ABO seed

The index can start with the main photos of **Amazon Berkeley Objects** (ABO,
<https://amazon-berkeley-objects.s3.amazonaws.com/index.html>), © Amazon.com, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which allows commercial use with attribution ("Amazon.com").
The seed holds only derived data: the fingerprints, each product's ASIN and brand, and the path of ABO's own copy of
the photo (for thumbnails). Seed rows are stored as the `amazon` shop with the listing key of the ASIN, so an analysed
Amazon page of the same product is the same listing, not a duplicate, and they are marked `source = 'abo'`, so they
can be removed without touching the photos seen in analyses.

Amazon Reviews 2023 was considered and not used: its data has no license (only its code is MIT). No published
dataset of AliExpress or Temu photos may be used commercially, so supplier photos enter the index only from analyses.

The seed file is **never published and never part of the container image**. Without it the index fills from
analyses only.

### Building the seed

```bash
python -m evals.image_index_eval sample --out DIR      # pick 50,000 index + 5,000 query photos from ABO
python -m evals.image_index_eval prints --dir DIR      # download (256 px) and fingerprint them
python -m evals.image_index_eval tune --dir DIR        # measure and pick the thresholds (DIR/report.json)
python -m unfret.image_seed build --dir DIR --out abo-seed.npz
```

The current seed, `abo-seed.npz`: **53,553 photos** (one per ASIN), 16 MB, SHA-256
`44e30c5337c4c2ff4cfbc8c930eccec27527b4d59833b3dafe37fdb29b50a5d3`.

### Importing it into a database (operator step)

`python -m unfret.image_seed import FILE --source abo --sha256 HEX` checks the file's hash before anything else
(a mismatch imports nothing), then copies the photos into `UNFRET_POSTGRES_URI` in one transaction (0.8 s for 53,553 photos locally).
It is idempotent: photos already there are skipped, so running it again, or after a partial attempt, is safe. It
prints the number of `abo` photos in the index afterwards. `python -m unfret.image_seed remove --source abo` deletes
them again.

On the server (the app container already has `UNFRET_POSTGRES_URI`; Kamal names it
`unfret-web-<destination>-<version>`):

```bash
# 1. copy the file to the server
scp abo-seed.npz <server>:/tmp/abo-seed.npz

# 2. on the server: find the app container, copy the file in, import with the expected hash
APP=$(docker ps --format '{{.Names}}' | grep '^unfret-web-production-' | head -1)   # or unfret-web-dev-
docker cp /tmp/abo-seed.npz "$APP":/tmp/abo-seed.npz
docker exec "$APP" python -m unfret.image_seed import /tmp/abo-seed.npz --source abo \
  --sha256 44e30c5337c4c2ff4cfbc8c930eccec27527b4d59833b3dafe37fdb29b50a5d3
# prints 53553

# 3. verify the count in the database
docker exec unfret-prod-db psql -U unfret -d unfret -tAc "SELECT COUNT(*) FROM image_prints WHERE source = 'abo'"
# 53553   (unfret-dev-db for dev)

# 4. delete the copies
docker exec -u root "$APP" rm /tmp/abo-seed.npz   # docker cp made it root-owned
rm /tmp/abo-seed.npz
```

Each database is seeded separately: repeat steps 1–4 for every database that should have the seed (`unfret-dev-db`
and `unfret-prod-db`, each with its own app containers). A new deploy doesn't
remove the seed (it lives in the database, not the image); a fresh database needs the import again.

## Privacy

Rows hold the shop (registrable domain, or `amazon`/`bol.com`), a hash of the listing (shop and public product ID or
page path), the fingerprints, the photo's own address, and the first and last day seen. No page address, caller,
score or verdict. Photos seen in analyses are deleted 400 days after they were last seen. Evaluation runs never
write to the index (`fingerprints` off).

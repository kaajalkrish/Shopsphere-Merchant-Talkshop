"""
Build the ShopSphere catalog photos (Demo 1, Phase 1.2) — one per product colour.

    python scripts/fetch_catalog_photos.py             # make photos that are missing
    python scripts/fetch_catalog_photos.py --refresh   # rebuild every photo
    python scripts/fetch_catalog_photos.py --only SSP004,SSP012

Each colour in data/shopsphere_catalog.json names a hand-picked Pixabay photo
(free Pixabay Content License, no attribution required; key in .env as
PIXABAY_API_KEY). This downloads it and gives every product the same "studio"
look: background removed, centred on a soft neutral backdrop with a floor
shadow, 800x800 WebP at frontend/public/catalog/<slug>/<colour>.webp.
Sources are recorded in data/catalog_images.json.

Processing modes per photo:
  studio   remove the background (rembg)
  largest  same, then keep only the largest shape (drops separate props)
  diff     for a photo shoot repeated in several colours: the pixels that differ
           from the reference photo are exactly the product — no guessing
  onblack  product on a plain black backdrop: separate by brightness

Run it in its OWN virtual environment — rembg needs numpy 2, which conflicts
with the app's packages (pandas/scikit-learn/pyarrow need numpy 1.x):

    python -m venv .venv-photos
    .venv-photos/Scripts/pip install rembg onnxruntime scipy pillow httpx python-dotenv
    .venv-photos/Scripts/python scripts/fetch_catalog_photos.py

(First run downloads a ~170 MB model.) Afterwards, with the normal Python,
`python -m backend.db.init_db` links the photos in the database.
"""
import io
import json
import os
import sys
import time
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "shopsphere_catalog.json"
RECORD = ROOT / "data" / "catalog_images.json"
OUT_DIR = ROOT / "frontend" / "public" / "catalog"
SIZE = 800
BACKDROP = (243, 242, 239)   # soft warm grey — reads well in light and dark themes
SHADOW = (150, 148, 144)

_session = None


def rembg_cut(img: Image.Image) -> Image.Image:
    global _session
    from rembg import new_session, remove
    if _session is None:
        _session = new_session("isnet-general-use")
    return remove(img, session=_session)


def keep_largest(mask: np.ndarray) -> np.ndarray:
    lab, n = ndimage.label(mask)
    if n <= 1:
        return mask
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    return lab == (int(np.argmax(sizes)) + 1)


def diff_cut(img: Image.Image, ref: Image.Image) -> Image.Image:
    """Same shoot in another colour: where the two photos differ is the product."""
    ref = ref.resize(img.size)
    a, b = np.asarray(img, dtype=np.int16), np.asarray(ref, dtype=np.int16)
    mask = np.abs(a - b).max(axis=2) > 38
    mask = ndimage.binary_opening(mask, iterations=2)
    mask = keep_largest(ndimage.binary_closing(mask, iterations=4))
    # Shots aren't perfectly identical, so slivers of background can differ too:
    # keep only pixels close to the garment's own colour (drops wood, cables, props).
    garment = np.median(a[mask], axis=0)
    mask &= np.abs(a - garment).max(axis=2) < 70
    mask = keep_largest(ndimage.binary_fill_holes(ndimage.binary_closing(mask, iterations=3)))
    # Props lying on the hem hide the bottom of the shirt. Below the sleeves a
    # flat-laid tee is a straight-sided body, so rebuild it: take the body's
    # width at 60% height, fill down to the hem line with each column's typical
    # fabric colour from the band just above (smooth, so no repeated wrinkles).
    pixels = np.asarray(img).copy()
    rows = np.where(mask.any(axis=1))[0]
    top, bottom = rows[0], rows[-1]
    y60 = int(top + 0.6 * (bottom - top))
    cols = np.nonzero(mask[y60])[0]
    if cols.size:
        left, right = cols.min(), cols.max()
        third = (right - left) // 3
        centre = mask[:, left + third: right - third]
        hem = int(np.percentile(np.nonzero(centre.any(axis=1))[0], 98)) if centre.any() else bottom
        band_px = pixels[max(top, y60 - 60): y60, left:right + 1].astype(np.float32)
        band_mask = mask[max(top, y60 - 60): y60, left:right + 1]
        fallback = np.median(band_px[band_mask], axis=0)
        col_colour = np.array([np.median(band_px[:, i][band_mask[:, i]], axis=0) if band_mask[:, i].any() else fallback
                               for i in range(right - left + 1)])
        col_colour = ndimage.uniform_filter1d(col_colour, size=25, axis=0)  # soften column-to-column change
        for y in range(y60 + 1, hem + 1):
            missing = ~mask[y, left:right + 1]
            pixels[y, left:right + 1][missing] = col_colour[missing].astype(np.uint8)
            mask[y, left:right + 1] = True
        mask[hem + 1:, left:right + 1] = False  # clean straight hem
    alpha = Image.fromarray((mask * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.2))
    out = Image.fromarray(pixels).convert("RGBA")
    out.putalpha(alpha)
    return out


def black_cut(img: Image.Image, kernel: int) -> Image.Image:
    """Product shot on a plain black backdrop: anything clearly brighter than
    black is the product. Openings in both directions drop the thin
    stretched-edge streaks some sources have."""
    a = np.asarray(img, dtype=np.int16)
    mask = a.max(axis=2) > 45
    mask = ndimage.binary_opening(mask, structure=np.ones((kernel, 1)))
    mask = ndimage.binary_opening(mask, structure=np.ones((1, kernel)))
    mask = keep_largest(ndimage.binary_fill_holes(ndimage.binary_closing(mask, iterations=3)))
    alpha = Image.fromarray((mask * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.0))
    out = img.convert("RGBA")
    out.putalpha(alpha)
    return out


def compose(cut: Image.Image) -> Image.Image:
    """Centre the cut-out on the studio backdrop with a soft floor shadow."""
    bbox = cut.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    cut = cut.crop(bbox)
    scale = min(SIZE * 0.80 / cut.width, SIZE * 0.72 / cut.height)
    cut = cut.resize((max(1, int(cut.width * scale)), max(1, int(cut.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (SIZE, SIZE), BACKDROP)
    x, y = (SIZE - cut.width) // 2, (SIZE - cut.height) // 2 - 10
    shadow = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(shadow).ellipse([x + cut.width * 0.08, y + cut.height - 14,
                                    x + cut.width * 0.92, y + cut.height + 22], fill=70)
    canvas.paste(Image.new("RGB", (SIZE, SIZE), SHADOW), (0, 0), shadow.filter(ImageFilter.GaussianBlur(18)))
    canvas.paste(cut, (x, y), cut)
    return canvas


def fetch(client, key, image_id):
    r = client.get("https://pixabay.com/api/", params={"key": key, "id": image_id}, timeout=20)
    r.raise_for_status()
    time.sleep(0.7)  # stay well under Pixabay's 100 requests/minute
    hit = r.json()["hits"][0]
    img = Image.open(io.BytesIO(client.get(hit["largeImageURL"], timeout=60).content)).convert("RGB")
    return hit, img


def main():
    load_dotenv(ROOT / ".env")
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        sys.exit("PIXABAY_API_KEY is not set in .env")
    refresh = "--refresh" in sys.argv
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else {}
    with httpx.Client(headers={"User-Agent": "ShopSphere-demo-catalog/1.0"}) as client:
        for product in catalog["products"]:
            if only and product["product_id"] not in only:
                continue
            for c in product["colors"]:
                photo = c.get("photo")
                if not photo:                                    # no hand-picked photo yet (e.g. kids')
                    continue
                dest = OUT_DIR / product["slug"] / f"{'-'.join(c['name'].lower().split())}.webp"
                done = record.get(product["slug"], {}).get(c["name"], {})
                if dest.exists() and not refresh and done.get("pixabay_id") == photo["pixabay_id"]:
                    continue
                hit, img = fetch(client, key, photo["pixabay_id"])
                if photo["mode"] == "onblack":
                    cut = black_cut(img, photo.get("kernel", 25))
                elif photo["mode"] == "diff":
                    _, ref = fetch(client, key, photo["ref_pixabay_id"])
                    cut = diff_cut(img, ref)
                else:
                    cut = rembg_cut(img)
                    if photo["mode"] == "largest":
                        # Opening with a tall element erases thin horizontal streaks
                        # (stretched-edge artefacts in some sources) before keeping
                        # the single largest shape.
                        solid = ndimage.binary_opening(np.asarray(cut.getchannel("A")) > 40,
                                                       structure=np.ones((photo.get("kernel", 9), 1)))
                        mask = keep_largest(solid)
                        cut.putalpha(Image.fromarray((np.asarray(cut.getchannel("A")) * mask).astype("uint8")))
                dest.parent.mkdir(parents=True, exist_ok=True)
                compose(cut).save(dest, "WEBP", quality=84, method=6)
                record.setdefault(product["slug"], {})[c["name"]] = {
                    "pixabay_id": photo["pixabay_id"], "mode": photo["mode"], "page_url": hit["pageURL"],
                    "photographer": hit.get("user"), "license": "Pixabay Content License",
                }
                RECORD.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
                print(f"  {product['product_id']} {product['name']} / {c['name']}  ({photo['mode']})")
    print("done")


if __name__ == "__main__":
    main()

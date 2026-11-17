"""Image compression / conversion engine (Pillow).

Inputs: JPG, JPEG, PNG, WebP.  Outputs: same format (compress only) or WebP / AVIF.

Strategy, per image, kept deliberately short so it is fast:
  1. Encode once at a high "normal" quality. Most images are done here.
  2. If a size limit is set and the file is too big, try two slightly lower qualities.
  3. If still too big, shrink dimensions at most twice (never below 60 % or 2000 px
     on the long edge), each time with a still-good quality.
  4. If it still doesn't fit, keep the best-quality version and report it.
The original file is only read, never written.
"""
from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageChops, ImageOps, ImageStat, features

Image.MAX_IMAGE_PIXELS = 250_000_000  # allow very large local photos (default warns at ~89 MP)

INPUT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONVERT_TARGETS = {"webp": "WEBP", "avif": "AVIF"}
EXT_FOR = {"WEBP": ".webp", "AVIF": ".avif"}
LABEL = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP", "AVIF": "AVIF"}

AVIF_AVAILABLE = features.check("avif")
WEBP_AVAILABLE = features.check("webp")

# Quality ladders. The first value is the normal quality; the others are used only
# when needed to meet the size limit. All values stay in the "looks good" range.
LADDER = {"JPEG": [85, 80, 75], "WEBP": [82, 77, 72], "AVIF": [64, 58, 52]}
RESIZED_LADDER = {"JPEG": [82, 76], "WEBP": [80, 74], "AVIF": [60, 54]}

# PNG colour reduction (256-colour palette + dithering) is accepted only when it is
# measured to look the same. PSNR is computed on 2x2-averaged pixels (how dithering
# is perceived). Below the floor it is never used, even to meet the size limit.
PNG_GOOD_PSNR = 40.0
PNG_FLOOR_PSNR = 37.0
FAST_PNG_PIXELS = 8_000_000        # above this, use zlib level 6 instead of the slow max setting
SKIP_TRUECOLOR_PIXELS = 4_000_000

MIN_SCALE = 0.60          # never shrink below 60 % of the original dimensions
MIN_LONG_EDGE = 2000      # ...or below 2000 px on the long edge
MAX_RESIZE_STEPS = 2

NOT_REACHED = "Could not reach {limit} without significant quality loss."


class UnsupportedImage(Exception):
    pass


@dataclass
class Result:
    data: bytes
    fmt: str                 # JPEG / PNG / WEBP / AVIF
    width: int
    height: int
    orig_width: int
    orig_height: int
    status: str              # "done" | "warn"
    note: str
    detail: str              # e.g. "quality 82" / "lossless" / "256 colours"
    kept_original: bool = False


def fmt_mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{n / 1000:.0f} KB"


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------

def _load(data: bytes):
    """Decode, apply EXIF orientation, normalise to RGB / RGBA.

    Returns (work_image, original_mode_image, source_format, icc_profile, has_alpha).
    """
    try:
        im = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedImage("Not a readable image") from exc
    src_fmt = "JPEG" if im.format == "MPO" else im.format
    if src_fmt not in ("JPEG", "PNG", "WEBP"):
        raise UnsupportedImage(f"Unsupported format ({im.format})")
    if getattr(im, "is_animated", False):
        raise UnsupportedImage("Animated images are not supported")
    im.load()
    icc = im.info.get("icc_profile")

    im = ImageOps.exif_transpose(im)  # bake in camera orientation

    if im.mode == "CMYK":
        if icc:
            try:
                from PIL import ImageCms
                im = ImageCms.profileToProfile(
                    im, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"),
                    outputMode="RGB")
                icc = None
            except Exception:  # noqa: BLE001
                im = im.convert("RGB")
                icc = None
        else:
            im = im.convert("RGB")
    elif im.mode.startswith("I;16") or im.mode in ("I", "F"):
        im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")

    original = im
    has_alpha = im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info
    if has_alpha:
        work = im.convert("RGBA")
        if work.getchannel("A").getextrema()[0] == 255:  # alpha present but fully opaque
            work, has_alpha = work.convert("RGB"), False
    else:
        work = im.convert("RGB")
    return work, original, src_fmt, icc, has_alpha


# ----------------------------------------------------------------------------
# Encoding helpers
# ----------------------------------------------------------------------------

def _encode(img: Image.Image, fmt: str, quality: int, icc: Optional[bytes]) -> bytes:
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if fmt == "JPEG":
        if img.mode != "RGB":
            img = _flatten(img)
        try:
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **kw)
        except OSError:  # rare libjpeg buffer issue with optimize on huge images
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, progressive=True, **kw)
    elif fmt == "WEBP":
        img.save(buf, "WEBP", quality=quality, method=4, **kw)
    elif fmt == "AVIF":
        img.save(buf, "AVIF", quality=quality, speed=6, **kw)
    else:
        raise ValueError(fmt)
    return buf.getvalue()


def _flatten(img: Image.Image) -> Image.Image:
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.getchannel("A") if img.mode == "RGBA" else None)
    return bg


def _resize(img: Image.Image, scale: float) -> Image.Image:
    w = max(1, round(img.width * scale))
    h = max(1, round(img.height * scale))
    if img.mode == "RGBA":  # premultiply so transparent edges don't get dark fringes
        return img.convert("RGBa").resize((w, h), Image.LANCZOS, reducing_gap=3.0).convert("RGBA")
    return img.resize((w, h), Image.LANCZOS, reducing_gap=3.0)


def _min_scale(w: int, h: int) -> float:
    long_edge = max(w, h)
    if long_edge <= MIN_LONG_EDGE:
        return 1.0
    return max(MIN_SCALE, MIN_LONG_EDGE / long_edge)


def _next_scale(current: float, size: int, limit: int, min_scale: float) -> Optional[float]:
    """Estimate the scale needed (file size ~ pixel area), bounded and gradual."""
    if size * (min_scale / current) ** 2 > limit * 1.6:
        return None  # even the smallest allowed size can't fit: don't waste time resizing
    est = current * math.sqrt(limit / size * 0.9)
    est = min(est, current - 0.08)
    est = max(est, min_scale)
    est = math.floor(est * 100) / 100
    if est >= current - 0.005:
        return None
    return est


# ----------------------------------------------------------------------------
# PNG
# ----------------------------------------------------------------------------

def _psnr(a: Image.Image, b: Image.Image) -> float:
    """PSNR on 2x2-averaged pixels; transparent areas compared over mid-grey + alpha channel."""
    if a.mode == "RGBA":
        grey = Image.new("RGBA", a.size, (128, 128, 128, 255))
        b = b.convert("RGBA")
        pairs = [(Image.alpha_composite(grey, a).convert("RGB"), Image.alpha_composite(grey, b).convert("RGB")),
                 (a.getchannel("A"), b.getchannel("A"))]
    else:
        pairs = [(a, b.convert("RGB"))]
    sq, n = 0.0, 0
    for x, y in pairs:
        if x.width >= 2 and x.height >= 2:
            x, y = x.reduce(2), y.reduce(2)
        rms = ImageStat.Stat(ImageChops.difference(x, y)).rms
        sq += sum(r * r for r in rms)
        n += len(rms)
    mse = sq / n
    return 99.0 if mse <= 1e-12 else 10 * math.log10(255 * 255 / mse)


def _png_bytes(img: Image.Image, icc: Optional[bytes]) -> bytes:
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if img.width * img.height > FAST_PNG_PIXELS:
        kw["compress_level"] = 6      # ~2 % larger, several times faster on big images
    else:
        kw["optimize"] = True
    img.save(buf, "PNG", **kw)
    return buf.getvalue()


def _png_lossless(work: Image.Image, original: Optional[Image.Image], icc, truecolor: bool = True) -> Optional[bytes]:
    best = _png_bytes(work, icc) if truecolor else None
    # Keep compact source modes (palette / greyscale) when they're smaller.
    if original is not None and original.mode in ("P", "L", "LA", "1") and original.size == work.size:
        b = _png_bytes(original, icc)
        if best is None or len(b) < len(best):
            best = b
    # <= 256 distinct colours: an exact palette is lossless and much smaller.
    if work.getcolors(256) is not None:
        colours = len(work.getcolors(256))
        method = Image.Quantize.FASTOCTREE if work.mode == "RGBA" else Image.Quantize.MEDIANCUT
        pal = work.quantize(colors=max(2, colours), method=method, dither=Image.Dither.NONE)
        if _psnr(work, pal) >= 99:
            b = _png_bytes(pal, icc)
            if best is None or len(b) < len(best):
                best = b
    return best


def _png_palettes(work: Image.Image, icc) -> List[Tuple[bytes, float]]:
    """256-colour dithered candidates with their measured quality."""
    out = []
    methods = [Image.Quantize.FASTOCTREE]
    if work.mode == "RGB":
        methods.append(Image.Quantize.MEDIANCUT)
    for m in methods:
        pal = work.quantize(colors=256, method=m, dither=Image.Dither.FLOYDSTEINBERG)
        q = _psnr(work, pal)
        if q >= PNG_FLOOR_PSNR:
            out.append((_png_bytes(pal, icc), q))
        if q >= PNG_GOOD_PSNR:
            break  # good enough; skip the slower method
    return out


def _optimize_png(work, original, icc, limit) -> Tuple[bytes, int, int, str, bool]:
    """Returns (data, width, height, detail, limit_met)."""
    W, H = work.size
    fits = (lambda b: True) if limit is None else (lambda b: len(b) <= limit)

    def candidates(img, orig):
        if img.getcolors(256) is not None:      # few colours: exact palette, nothing to approximate
            return [(_png_lossless(img, orig, icc), 99.0)]
        pals = _png_palettes(img, icc)
        good_pal = any(q >= PNG_GOOD_PSNR for _, q in pals)
        # Big photo with a good 256-colour version: a truecolor PNG would be many times
        # larger and slow to compress, so skip it.
        skip_truecolor = good_pal and img.width * img.height > SKIP_TRUECOLOR_PIXELS
        lossless = _png_lossless(img, orig, icc, truecolor=not skip_truecolor)
        return ([(lossless, 99.0)] if lossless else []) + pals

    cands = candidates(work, original)
    palettes = [(b, q) for b, q in cands if q < 99]
    good = [(b, q) for b, q in cands if q >= PNG_GOOD_PSNR]
    # Smallest version that looks the same; otherwise the highest-quality one.
    best = min(good, key=lambda t: len(t[0])) if good else max(cands, key=lambda t: t[1])
    detail = lambda q: "lossless" if q >= 99 else "256 colours"  # noqa: E731
    if fits(best[0]):
        return best[0], W, H, detail(best[1]), True

    # Size limit: accept a slightly lower (still floor-checked) palette version...
    fitting = [(b, q) for b, q in palettes if fits(b)]
    if fitting:
        b, q = max(fitting, key=lambda t: t[1])
        return b, W, H, detail(q), True

    # ...then shrink dimensions a little.
    scale, min_s = 1.0, _min_scale(W, H)
    last_size = min(len(b) for b, _ in cands)
    for _ in range(MAX_RESIZE_STEPS):
        new = _next_scale(scale, last_size, limit, min_s)
        if new is None:
            break
        scale = new
        small = _resize(work, scale)
        cands = candidates(small, None)
        fitting = [(b, q) for b, q in cands if fits(b)]
        if fitting:
            b, q = max(fitting, key=lambda t: (t[1], -len(t[0])))
            return b, small.width, small.height, detail(q), True
        last_size = min(len(b) for b, _ in cands)

    return best[0], W, H, detail(best[1]), False


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------

def optimize(data: bytes, convert_to: Optional[str] = None, max_bytes: Optional[int] = None) -> Result:
    """Optimize one image.

    convert_to: None (keep format), "webp" or "avif".
    max_bytes:  size limit in bytes, or None for no limit.
    """
    work, original, src_fmt, icc, has_alpha = _load(data)
    W, H = work.size
    out_fmt = CONVERT_TARGETS[convert_to] if convert_to else src_fmt
    if out_fmt == "AVIF" and not AVIF_AVAILABLE:
        raise UnsupportedImage("AVIF encoding is not available in this Pillow build")
    if out_fmt == "WEBP" and not WEBP_AVAILABLE:
        raise UnsupportedImage("WebP encoding is not available in this Pillow build")
    if out_fmt == "JPEG" and has_alpha:  # only possible for unusual JPEGs; JPEG has no alpha
        work, has_alpha = _flatten(work), False

    fits = (lambda n: True) if max_bytes is None else (lambda n: n <= max_bytes)

    if out_fmt == "PNG":
        out, w, h, detail, met = _optimize_png(work, original, icc, max_bytes)
    else:
        ladder = LADDER[out_fmt]
        first = _encode(work, out_fmt, ladder[0], icc)
        out, w, h, detail, met = first, W, H, f"quality {ladder[0]}", fits(len(first))
        if not met:
            last = first
            # Step 2: a little lower quality.
            for q in ladder[1:]:
                b = _encode(work, out_fmt, q, icc)
                last = b
                if fits(len(b)):
                    out, detail, met = b, f"quality {q}", True
                    break
            # Step 3: modest resize, at most twice.
            scale, min_s = 1.0, _min_scale(W, H)
            for _ in range(MAX_RESIZE_STEPS if not met else 0):
                new = _next_scale(scale, len(last), max_bytes, min_s)
                if new is None:
                    break
                scale = new
                small = _resize(work, scale)
                for q in RESIZED_LADDER[out_fmt]:
                    b = _encode(small, out_fmt, q, icc)
                    last = b
                    if fits(len(b)):
                        out, w, h, detail, met = b, small.width, small.height, f"quality {q}", True
                        break
                if met:
                    break
            if not met:  # keep the best-quality version
                out, w, h, detail = first, W, H, f"quality {ladder[0]}"

    status, note, kept = "done", "", False
    if (w, h) != (W, H):
        note = f"Resized to {w}×{h} ({round(w / W * 100)}%) to fit the size limit."

    # Same format and the original is already smaller: keep the original data (as a new file).
    if out_fmt == src_fmt and len(data) <= len(out) and (fits(len(data)) or not met):
        out, w, h, detail, kept = data, W, H, "original kept", True
        met = fits(len(data))
        note = "Already well optimized. Re-encoding would make it larger, so the original image data is kept."

    if not met:
        status = "warn"
        note = NOT_REACHED.format(limit=fmt_mb(max_bytes)) + (" " + note if kept else "")
    return Result(out, out_fmt, w, h, W, H, status, note, detail, kept)


def output_extension(filename: str, convert_to: Optional[str]) -> str:
    if convert_to:
        return EXT_FOR[CONVERT_TARGETS[convert_to]]
    return os.path.splitext(filename)[1]  # keep .jpg / .jpeg / .png / .webp exactly


# ----------------------------------------------------------------------------
# Command line (optional): python optimizer.py IMAGES_OR_FOLDERS... [--convert webp|avif]
# ----------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    import time
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Compress JPG/PNG/WebP images, optionally converting to WebP/AVIF.")
    ap.add_argument("inputs", nargs="+", help="image files or folders")
    ap.add_argument("--convert", choices=["webp", "avif"], help="convert to this format (default: keep format)")
    ap.add_argument("--max-mb", type=float, default=3.0, help="maximum output size in MB (0 = no limit)")
    ap.add_argument("--out", default="output", help="output folder (default: ./output)")
    args = ap.parse_args()

    files = []
    for p in map(Path, args.inputs):
        if p.is_dir():
            base = p.parent
            files += [(f, f.relative_to(base)) for f in sorted(p.rglob("*"))
                      if f.is_file() and f.suffix.lower() in INPUT_EXTENSIONS]
        elif p.suffix.lower() in INPUT_EXTENSIONS:
            files.append((p, Path(p.name)))
    out_root = Path(args.out).resolve()
    limit = int(args.max_mb * 1_000_000) if args.max_mb > 0 else None
    tot_in = tot_out = 0
    for src, rel in files:
        dest = out_root / rel.parent / (rel.stem + output_extension(rel.name, args.convert))
        if dest.resolve() == src.resolve():
            print(f"skip {src}: output would overwrite the original")
            continue
        t = time.time()
        data = src.read_bytes()
        try:
            r = optimize(data, args.convert, limit)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {src}: {exc}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.data)
        tot_in += len(data)
        tot_out += len(r.data)
        saved = (1 - len(r.data) / len(data)) * 100
        print(f"{str(rel):40} {fmt_mb(len(data)):>9} -> {fmt_mb(len(r.data)):>9}  {saved:5.1f}%  "
              f"{LABEL[r.fmt]:5} {r.width}x{r.height} {r.detail:15} {time.time() - t:5.2f}s  {r.status} {r.note}")
    if tot_in:
        print(f"\nTotal: {fmt_mb(tot_in)} -> {fmt_mb(tot_out)}  saved {fmt_mb(tot_in - tot_out)} "
              f"({(1 - tot_out / tot_in) * 100:.1f}%)  -> {out_root}")


if __name__ == "__main__":
    _cli()

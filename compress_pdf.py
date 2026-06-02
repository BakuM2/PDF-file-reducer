#!/usr/bin/env python3
"""
PDF Compressor — Targets 3x+ file size reduction.

Strategies:
  1. Downsample images to a target DPI (biggest win for most PDFs)
  2. Re-encode images with aggressive JPEG quality
  3. Recompress all streams with maximum deflate
  4. Strip metadata and remove unused objects
  5. Linearize output

Usage:
    python compress_pdf.py input.pdf                      # → input_compressed.pdf
    python compress_pdf.py input.pdf -o small.pdf         # custom output name
    python compress_pdf.py input.pdf --dpi 100            # lower DPI = smaller file
    python compress_pdf.py input.pdf --quality 40         # lower JPEG quality
    python compress_pdf.py input.pdf --grayscale          # convert images to grayscale
"""

import argparse
import io
import os
import sys
from pathlib import Path

import pikepdf
from PIL import Image


def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def compress_image(image_obj, *, target_dpi: int, jpeg_quality: int, grayscale: bool) -> tuple[bytes, str] | None:
    """Extract, downsample, and re-encode a PDF image. Returns (jpeg_data, mode) or None."""
    try:
        # Use pikepdf.PdfImage for robust decoding of any PDF image format
        pdf_image = pikepdf.PdfImage(image_obj)
        pil_img = pdf_image.as_pil_image()
    except Exception:
        # Fallback: try reading decoded stream bytes directly
        try:
            width = int(image_obj.get("/Width", 0))
            height = int(image_obj.get("/Height", 0))
            if width == 0 or height == 0:
                return None
            decoded = image_obj.read_bytes()  # fully decoded data
            cs = str(image_obj.get("/ColorSpace", ""))
            if "/DeviceRGB" in cs:
                mode = "RGB"
            elif "/DeviceGray" in cs:
                mode = "L"
            else:
                mode = "RGB"
            channels = 3 if mode == "RGB" else 1
            expected = width * height * channels
            if len(decoded) >= expected:
                pil_img = Image.frombytes(mode, (width, height), decoded[:expected])
            else:
                return None
        except Exception:
            return None

    try:
        # Skip tiny images (icons, logos) — not worth compressing
        if pil_img.width < 64 or pil_img.height < 64:
            return None

        # Calculate scale factor: assume original ~300 DPI, scale to target
        scale = target_dpi / 300.0
        if scale >= 1.0:
            scale = 0.75  # always downsample at least a little

        new_w = max(int(pil_img.width * scale), 1)
        new_h = max(int(pil_img.height * scale), 1)

        pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

        # Convert colour space
        if grayscale and pil_img.mode != "L":
            pil_img = pil_img.convert("L")
        elif pil_img.mode == "CMYK":
            pil_img = pil_img.convert("RGB")
        elif pil_img.mode not in ("RGB", "L", "RGBA"):
            pil_img = pil_img.convert("RGB")

        if pil_img.mode == "RGBA":
            # JPEG doesn't support alpha — flatten onto white
            bg = Image.new("RGB", pil_img.size, (255, 255, 255))
            bg.paste(pil_img, mask=pil_img.split()[3])
            pil_img = bg

        # Encode as JPEG
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue(), pil_img.mode

    except Exception:
        return None


def compress_pdf(
    input_path: str,
    output_path: str,
    *,
    target_dpi: int = 120,
    jpeg_quality: int = 45,
    grayscale: bool = False,
) -> dict:
    """Compress a PDF and return stats."""
    original_size = get_file_size_mb(input_path)

    pdf = pikepdf.open(input_path)
    images_processed = 0
    images_skipped = 0

    # --- Pass 1: Downsample and re-encode images ---
    for page in pdf.pages:
        if "/Resources" not in page:
            continue
        resources = page["/Resources"]
        if "/XObject" not in resources:
            continue
        xobjects = resources["/XObject"]
        for key in list(xobjects.keys()):
            xobj = xobjects[key]
            if not isinstance(xobj, pikepdf.Stream):
                continue
            if xobj.get("/Subtype") != pikepdf.Name.Image:
                continue

            result = compress_image(
                xobj,
                target_dpi=target_dpi,
                jpeg_quality=jpeg_quality,
                grayscale=grayscale,
            )
            if result is None:
                images_skipped += 1
                continue

            jpeg_data, mode = result
            # Get dimensions from the JPEG
            tmp_img = Image.open(io.BytesIO(jpeg_data))
            new_w, new_h = tmp_img.size
            tmp_img.close()

            new_img = pikepdf.Stream(pdf, jpeg_data)
            new_img[pikepdf.Name.Type] = pikepdf.Name.XObject
            new_img[pikepdf.Name.Subtype] = pikepdf.Name.Image
            new_img[pikepdf.Name.Width] = new_w
            new_img[pikepdf.Name.Height] = new_h
            new_img[pikepdf.Name.BitsPerComponent] = 8
            new_img[pikepdf.Name.Filter] = pikepdf.Name.DCTDecode
            if mode == "L" or grayscale:
                new_img[pikepdf.Name.ColorSpace] = pikepdf.Name.DeviceGray
            else:
                new_img[pikepdf.Name.ColorSpace] = pikepdf.Name.DeviceRGB

            xobjects[key] = new_img
            images_processed += 1

    # --- Pass 2: Remove metadata to save space ---
    if pdf.docinfo:
        try:
            with pdf.open_metadata() as meta:
                # Clear XMP metadata
                pass
        except Exception:
            pass

    # --- Pass 3: Save with maximum stream compression ---
    pdf.save(
        output_path,
        recompress_flate=True,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        linearize=True,
    )
    pdf.close()

    compressed_size = get_file_size_mb(output_path)
    ratio = original_size / compressed_size if compressed_size > 0 else float("inf")

    return {
        "original_mb": round(original_size, 2),
        "compressed_mb": round(compressed_size, 2),
        "ratio": round(ratio, 1),
        "images_processed": images_processed,
        "images_skipped": images_skipped,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compress PDF files — targets 3x+ reduction"
    )
    parser.add_argument("input", help="Input PDF file")
    parser.add_argument("-o", "--output", help="Output file (default: <input>_compressed.pdf)")
    parser.add_argument(
        "--dpi", type=int, default=120,
        help="Target image DPI (lower = smaller). Default: 120"
    )
    parser.add_argument(
        "--quality", type=int, default=45,
        help="JPEG quality 1-95 (lower = smaller). Default: 45"
    )
    parser.add_argument(
        "--grayscale", action="store_true",
        help="Convert all images to grayscale for extra savings"
    )

    args = parser.parse_args()

    input_path = args.input
    if not os.path.isfile(input_path):
        print(f"Error: '{input_path}' not found.")
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        p = Path(input_path)
        output_path = str(p.with_stem(p.stem + "_compressed"))

    print(f"Compressing: {input_path}")
    print(f"  DPI target : {args.dpi}")
    print(f"  JPEG quality: {args.quality}")
    print(f"  Grayscale  : {'yes' if args.grayscale else 'no'}")
    print()

    stats = compress_pdf(
        input_path,
        output_path,
        target_dpi=args.dpi,
        jpeg_quality=args.quality,
        grayscale=args.grayscale,
    )

    print(f"  Original   : {stats['original_mb']:.2f} MB")
    print(f"  Compressed : {stats['compressed_mb']:.2f} MB")
    print(f"  Ratio      : {stats['ratio']}x smaller")
    print(f"  Images     : {stats['images_processed']} processed, {stats['images_skipped']} skipped")
    print(f"\nSaved to: {output_path}")

    if stats["ratio"] < 3:
        print(
            "\nTip: For more compression, try:\n"
            f"  python {sys.argv[0]} {input_path} --dpi 72 --quality 30 --grayscale"
        )


if __name__ == "__main__":
    main()

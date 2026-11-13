#!/usr/bin/env python3
"""Build the ADAPT application icon (vector master + raster exports + .ico).

Run from anywhere:

    python scripts/build_icon.py           # generate everything, write manifest
    python scripts/build_icon.py --check   # regenerate in memory, compare to disk

Outputs (all inside docs/assets/icon/, nothing else is touched):

    adapt.svg            master vector, transparent background, 1024 viewBox
    adapt.png            1024 x 1024 transparent raster of the master
    adapt-256.png ... adapt-16.png   size exports (256, 128, 64, 48, 32, 16)
    adapt.ico            multi-resolution Windows icon (16, 24, 32, 48, 64, 128, 256)
    adapt-preview.png    single presentation tile on a dark background
    manifest.json        size + sha256 of every generated file

Design: one geometric capital A. The left stroke is solid green; across the
crossbar and the right stroke it thins out through a few slanted slices into
an inset wireframe carrying a handful of drafting marks (centre line, hidden
edges, anchor nodes, two dimension lines, one registration mark).

One shape list drives both writers: ``to_svg`` serialises it and ``rasterize``
draws the very same polygons and segments with Pillow (supersampled, then
box-filtered). No SVG renderer, font, network access or other image model is
involved. Small sizes use the same letter geometry with less drafting detail.

Only the standard library and Pillow are needed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import struct
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "assets" / "icon"

CANVAS = 1024
PNG_SIZES = (256, 128, 64, 48, 32, 16)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Palette: the terminal identity's greens, white and gray. No blue.
GREEN = "#35d373"      # solid stroke + wireframe
DEEP = "#1c8c4e"       # construction lines, small-size second tone (brand accent)
WHITE = "#ffffff"      # anchor nodes
GRAY = "#7f8c85"       # dimension lines (terminal dim text)
TILE_BG = "#080d0a"
TILE = "#0b110e"
TILE_EDGE = "#223028"
TILE_GRID = "#1b2d22"

# ---------------------------------------------------------------- geometry --
# The letter, in base units on a 1024 square. Both legs lean 0.4 x per 1 y,
# legs are 176 wide, the crossbar runs from y=600 to y=720.
SLOPE = 0.4
BASE_Y = 912.0
OUTER = [(112, 912), (432, 112), (592, 112), (912, 912),
         (736, 912), (659.2, 720), (364.8, 720), (288, 912)]
COUNTER = [(512, 352), (611.2, 600), (412.8, 600)]

# "k" measures distance across the letter along lines parallel to the left
# leg: k = x - SLOPE * (BASE_Y - y). The left leg is exactly 112 <= k <= 288.
SOLID_K = 336.0

VARIANTS = {
    # name: scale about the centre, shift, wire width, body tint, slices, detail
    "full": dict(scale=0.92, dx=-14, dy=-18, wire=9, tint=(GREEN, 0.10),
                 slices=[(358, 388), (418, 438), (478, 490)], detail=True),
    "mid": dict(scale=1.06, dx=0, dy=0, wire=24, tint=(GREEN, 0.16),
                slices=[(362, 402), (434, 458)], detail=False),
    "small": dict(scale=1.12, dx=0, dy=0, wire=40, tint=(GREEN, 0.30),
                  slices=[], detail=False),
    "tiny": dict(scale=1.14, dx=0, dy=0, wire=0, tint=(DEEP, 1.0),
                 slices=[], detail=False),
}


def variant_for(size: int) -> str:
    if size >= 200:
        return "full"
    if size >= 48:
        return "mid"
    if size >= 24:
        return "small"
    return "tiny"


def offset_polygon(poly, d):
    """Offset a simple polygon by d (positive = inward) with mitred corners."""
    n = len(poly)
    area = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1]
               for i in range(n))
    sign = 1.0 if area > 0 else -1.0
    lines = []
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        ex, ey = x1 - x0, y1 - y0
        length = math.hypot(ex, ey)
        nx, ny = -ey / length * sign, ex / length * sign
        lines.append(((x0 + nx * d, y0 + ny * d), (ex, ey)))
    out = []
    for i in range(n):
        (px, py), (rx, ry) = lines[i - 1]
        (qx, qy), (sx, sy) = lines[i]
        den = rx * sy - ry * sx
        t = ((qx - px) * sy - (qy - py) * sx) / den
        out.append((px + rx * t, py + ry * t))
    return out


def dash(p0, p1, pattern):
    """Split p0->p1 into explicit dash segments (on, off, on, off, ...)."""
    (x0, y0), (x1, y1) = p0, p1
    total = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    segs, pos, i = [], 0.0, 0
    while pos < total - 1e-9:
        step = pattern[i % len(pattern)]
        end = min(pos + step, total)
        if i % 2 == 0:
            segs.append(((x0 + ux * pos, y0 + uy * pos), (x0 + ux * end, y0 + uy * end)))
        pos, i = end, i + 1
    return segs


def k_band(k0, k1):
    """Parallelogram covering k0 <= k <= k1 over the whole canvas height."""
    top, bottom = -200.0, 1224.0
    return [(k0 + SLOPE * (BASE_Y - top), top), (k1 + SLOPE * (BASE_Y - top), top),
            (k1 + SLOPE * (BASE_Y - bottom), bottom), (k0 + SLOPE * (BASE_Y - bottom), bottom)]


def build_shapes(variant: str):
    """Return (clip_contours, shapes) in final 1024-canvas coordinates."""
    v = VARIANTS[variant]
    s, dx, dy = v["scale"], v["dx"], v["dy"]

    def T(p):
        return (512 + (p[0] - 512) * s + dx, 512 + (p[1] - 512) * s + dy)

    def TT(points):
        return [T(p) for p in points]

    def tseg(p0, p1):
        return (T(p0), T(p1))

    outer, counter = TT(OUTER), TT(COUNTER)
    letter = [outer, counter]
    shapes = []

    def poly(group, contours, fill, op=1.0, clip=False):
        shapes.append(dict(kind="poly", group=group, contours=contours, fill=fill, op=op, clip=clip))

    def segs(group, items, width, stroke, op=1.0):
        shapes.append(dict(kind="segs", group=group, segs=items, w=width, stroke=stroke, op=op))

    tint_color, tint_op = v["tint"]
    poly("body-tint", letter, tint_color, tint_op)

    if v["detail"]:
        # Construction lines, drawn under the wireframe: right-leg centre line
        # and the crossbar edges carried through the right leg as hidden edges.
        def leg_centre(t):
            return (824 - SLOPE * t, BASE_Y - t)

        construction = dash(tseg(leg_centre(-44), leg_centre(430))[0],
                            tseg(leg_centre(-44), leg_centre(430))[1], [46, 12, 9, 12])
        construction += dash(T((620, 600)), T((787.2, 600)), [18, 12])
        construction += dash(T((668, 720)), T((835.2, 720)), [18, 12])
        segs("construction", construction, 4.5, DEEP)

    if v["wire"]:
        w = v["wire"]
        ring = [outer, counter, offset_polygon(outer, w), offset_polygon(counter, -w)]
        poly("wireframe", ring, GREEN)

    for k0, k1 in v["slices"]:
        poly("solid", [TT(k_band(k0, k1))], GREEN, clip=True)
    poly("solid", [TT(k_band(-400, SOLID_K))], GREEN, clip=True)

    if v["detail"]:
        # Dimension lines: crossbar depth (right) and right-foot width (below).
        x_dim, y_dim, tick = 952, 962, 13
        dims = [
            tseg((803, 600), (x_dim + 16, 600)),
            tseg((851, 720), (x_dim + 16, 720)),
            tseg((x_dim, 600), (x_dim, 720)),
            tseg((x_dim - tick, 600 + tick), (x_dim + tick, 600 - tick)),
            tseg((x_dim - tick, 720 + tick), (x_dim + tick, 720 - tick)),
            tseg((736, 930), (736, y_dim + 16)),
            tseg((912, 930), (912, y_dim + 16)),
            tseg((736, y_dim), (912, y_dim)),
            tseg((736 - tick, y_dim + tick), (736 + tick, y_dim - tick)),
            tseg((912 - tick, y_dim + tick), (912 + tick, y_dim - tick)),
        ]
        segs("dimensions", dims, 4.5, GRAY)

        # One registration mark in the open corner beside the apex.
        cx, cy = T((812, 214))
        shapes.append(dict(kind="ring", group="registration", c=(cx, cy), r=27, w=4.5, stroke=GRAY))
        segs("registration", [((cx - 46, cy), (cx + 46, cy)), ((cx, cy - 46), (cx, cy + 46))], 4.5, GRAY)

        # Anchor nodes on the wireframe corners.
        for p in [(912, 912), (736, 912), (659.2, 720), (611.2, 600)]:
            x, y = T(p)
            for half, color in ((15, GREEN), (9, WHITE)):
                poly("nodes", [[(x - half, y - half), (x + half, y - half),
                                (x + half, y + half), (x - half, y + half)]], color)

    return letter, shapes


# --------------------------------------------------------------------- svg --
def _n(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _path(contours) -> str:
    return " ".join("M" + " L".join(f"{_n(x)} {_n(y)}" for x, y in c) + " Z" for c in contours)


def to_svg(variant: str = "full") -> str:
    letter, shapes = build_shapes(variant)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" '
        f'width="{CANVAS}" height="{CANVAS}" role="img" aria-labelledby="adapt-title adapt-desc">',
        '  <title id="adapt-title">ADAPT icon</title>',
        '  <desc id="adapt-desc">A geometric capital letter A. Its left stroke is solid green; '
        'toward the right it thins into slanted slices and becomes a wireframe engineering '
        'drawing with a centre line, anchor nodes and dimension ticks.</desc>',
        '  <defs>',
        f'    <clipPath id="letter"><path clip-rule="evenodd" d="{_path(letter)}"/></clipPath>',
        '  </defs>',
    ]
    group = None
    for sh in shapes:
        if sh["group"] != group:
            if group is not None:
                out.append("  </g>")
            group = sh["group"]
            out.append(f'  <g id="{group}">')
        op = "" if sh.get("op", 1.0) >= 1.0 else f' opacity="{_n(sh["op"])}"'
        if sh["kind"] == "poly":
            clip = ' clip-path="url(#letter)"' if sh["clip"] else ""
            rule = ' fill-rule="evenodd"' if len(sh["contours"]) > 1 else ""
            out.append(f'    <path fill="{sh["fill"]}"{rule}{op}{clip} d="{_path(sh["contours"])}"/>')
        elif sh["kind"] == "segs":
            d = " ".join(f"M{_n(a[0])} {_n(a[1])} L{_n(b[0])} {_n(b[1])}" for a, b in sh["segs"])
            out.append(f'    <path fill="none" stroke="{sh["stroke"]}" stroke-width="{_n(sh["w"])}"{op} d="{d}"/>')
        else:
            out.append(f'    <circle fill="none" stroke="{sh["stroke"]}" stroke-width="{_n(sh["w"])}" '
                       f'cx="{_n(sh["c"][0])}" cy="{_n(sh["c"][1])}" r="{_n(sh["r"])}"/>')
    out += ["  </g>", "</svg>", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ raster --
def _rgb(color: str):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _mask(size, contours, f):
    """Even-odd fill of contours as a binary L mask at scale f."""
    mask = None
    for contour in contours:
        layer = Image.new("L", (size, size), 0)
        ImageDraw.Draw(layer).polygon([(x * f, y * f) for x, y in contour], fill=255)
        mask = layer if mask is None else ImageChops.difference(mask, layer)
    return mask


def _quad(a, b, width):
    (x0, y0), (x1, y1) = a, b
    length = math.hypot(x1 - x0, y1 - y0)
    nx, ny = -(y1 - y0) / length * width / 2, (x1 - x0) / length * width / 2
    return [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]


def rasterize(size: int, variant: str | None = None) -> Image.Image:
    """Draw the icon at size x size (RGBA, transparent) from the shape list."""
    variant = variant or variant_for(size)
    ss = 4 if size >= 512 else 8 if size >= 128 else 16
    big = size * ss
    f = big / CANVAS
    letter, shapes = build_shapes(variant)
    clip = _mask(big, letter, f)
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    for sh in shapes:
        if sh["kind"] == "poly":
            mask = _mask(big, sh["contours"], f)
            if sh["clip"]:
                mask = ImageChops.multiply(mask, clip)
            color = sh["fill"]
        elif sh["kind"] == "segs":
            mask = Image.new("L", (big, big), 0)
            draw = ImageDraw.Draw(mask)
            for a, b in sh["segs"]:
                draw.polygon([(x * f, y * f) for x, y in _quad(a, b, sh["w"])], fill=255)
            color = sh["stroke"]
        else:
            (cx, cy), r, w = sh["c"], sh["r"], sh["w"]
            mask = Image.new("L", (big, big), 0)
            draw = ImageDraw.Draw(mask)
            for radius, fill in ((r + w / 2, 255), (r - w / 2, 0)):
                draw.ellipse([(cx - radius) * f, (cy - radius) * f,
                              (cx + radius) * f, (cy + radius) * f], fill=fill)
            color = sh["stroke"]
        op = sh.get("op", 1.0)
        if op < 1.0:
            mask = mask.point(lambda value, op=op: round(value * op))
        layer = Image.new("RGBA", (big, big), _rgb(color) + (0,))
        layer.putalpha(mask)
        image = Image.alpha_composite(image, layer)
    # Premultiplied box filter = exact coverage antialiasing, no colour fringes.
    return image.convert("RGBa").resize((size, size), Image.BOX).convert("RGBA")


def preview() -> Image.Image:
    """One dark presentation tile with the full-detail icon. No text."""
    side, ss = 1280, 2
    big = side * ss
    img = Image.new("RGB", (big, big), _rgb(TILE_BG))
    draw = ImageDraw.Draw(img)
    m, radius = 128 * ss, 176 * ss
    tile_mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(tile_mask).rounded_rectangle([m, m, big - m, big - m], radius=radius, fill=255)
    tile = Image.new("RGB", (big, big), _rgb(TILE))
    grid = ImageDraw.Draw(tile)
    for pos in range(m, big - m + 1, 64 * ss):
        grid.line([(pos, m), (pos, big - m)], fill=_rgb(TILE_GRID), width=ss)
        grid.line([(m, pos), (big - m, pos)], fill=_rgb(TILE_GRID), width=ss)
    img.paste(tile, (0, 0), tile_mask)
    draw.rounded_rectangle([m, m, big - m, big - m], radius=radius, outline=_rgb(TILE_EDGE), width=2 * ss)
    img = img.resize((side, side), Image.LANCZOS).convert("RGBA")
    icon = rasterize(896, "full")
    img.alpha_composite(icon, ((side - 896) // 2, (side - 896) // 2))
    return img.convert("RGB")


# ----------------------------------------------------------------- writers --
def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def ico_bytes(images: dict[int, Image.Image]) -> bytes:
    """Multi-resolution .ico: 32-bit DIB entries below 256, PNG for 256."""
    blobs = []
    for size in sorted(images):
        image = images[size]
        if size >= 256:
            blobs.append(png_bytes(image))
            continue
        b, g, r, a = (lambda c: (c[2], c[1], c[0], c[3]))(image.split())
        rows = Image.merge("RGBA", (b, g, r, a)).transpose(Image.FLIP_TOP_BOTTOM).tobytes()
        stride = ((size + 31) // 32) * 4
        and_rows = []
        alpha = image.getchannel("A").transpose(Image.FLIP_TOP_BOTTOM).tobytes()
        for y in range(size):
            row = bytearray(stride)
            for x in range(size):
                if alpha[y * size + x] == 0:
                    row[x // 8] |= 0x80 >> (x % 8)
            and_rows.append(bytes(row))
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(rows) + stride * size, 0, 0, 0, 0)
        blobs.append(header + rows + b"".join(and_rows))
    out = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    for size, blob in zip(sorted(images), blobs):
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    return out + b"".join(blobs)


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {"adapt.svg": to_svg("full").encode("utf-8")}
    rendered = {size: rasterize(size) for size in sorted(set(PNG_SIZES) | set(ICO_SIZES) | {CANVAS})}
    files["adapt.png"] = png_bytes(rendered[CANVAS])
    for size in PNG_SIZES:
        files[f"adapt-{size}.png"] = png_bytes(rendered[size])
    files["adapt.ico"] = ico_bytes({size: rendered[size] for size in ICO_SIZES})
    files["adapt-preview.png"] = png_bytes(preview())
    manifest = {
        "generator": "scripts/build_icon.py",
        "ico_sizes": list(ICO_SIZES),
        "detail_levels": {str(size): variant_for(size) for size in sorted(rendered)},
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(files.items())},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true",
                        help="regenerate in memory and compare with the files on disk")
    args = parser.parse_args()
    files = build()
    if args.check:
        bad = [name for name, data in files.items()
               if not (OUT_DIR / name).is_file() or (OUT_DIR / name).read_bytes() != data]
        for name in bad:
            print(f"MISMATCH docs/assets/icon/{name}")
        print("icon assets: " + ("OUT OF DATE" if bad else f"{len(files)} files match"))
        return 1 if bad else 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (OUT_DIR / name).write_bytes(data)
        print(f"wrote docs/assets/icon/{name}  {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

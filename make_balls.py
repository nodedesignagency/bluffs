#!/usr/bin/env python3
"""Generate team-coloured basketballs from one master render.

The master image is a matte, pebbled, solid red basketball with black seams on
a white background. For every row of the balls CSV (abbr, ball_hex, plus
optional team, text_hex and outline_hex) this writes

    <out>/<ABBR>.png      transparent PNG, same canvas, size and position for all
    <out>/preview.png     grid of every ball on the app's dark card colour

The red takes the team colour with the master's shading and pebble texture
kept; the black seams are left as they are. The abbreviation is printed big
across the middle seam: a bold condensed block font in off-white with a
subtle top-to-bottom gradient, a thin outline in a darker shade of the ball
colour, wrapped onto the sphere, with the pebble texture multiplied over it
and a soft inner shadow so it reads as printed on the rubber. The master's
faint pink edge halo is trimmed off.

    python make_balls.py
    python make_balls.py --teams balls.csv --out output/balls --size 1024
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from make_helmets import (
    HERE, TextLayout, analyse_master, blur, components_by_run, compress_highlights, contact_sheet,
    dilate, fit_to_canvas, grow_distance, linear_to_srgb, load_font, paint_runs, parse_hex,
    place_lettering, shoulder, slugify, smoothstep, srgb_to_linear, to_hex,
)

EDGE_ERODE = 2                   # px trimmed off the master's edge (removes the pink halo)
EDGE_FEATHER = 1.0               # px, softness of the new edge

TEXT_CAP = 0.35                  # cap height, fraction of the ball's height
TEXT_WIDTH = 0.75                # text width, fraction of the ball's width
STRETCH_LIMITS = (0.7, 1.7)      # how far the font may be widened or narrowed to hit TEXT_WIDTH
TEXT_TRACKING = 0.05             # letter spacing, fraction of cap height
BULGE_RADIUS = 1.25              # sphere the text wraps on, x ball radius (1 = full curve, larger = flatter)

TEXT_FILL = (0xF2, 0xF2, 0xEE)
FILL_GRADIENT = 0.05             # top this much lighter, bottom this much darker
OUTLINE_PX = 3.0                 # outline width in output pixels at the default 1024 canvas
OUTLINE_DARKEN = 0.45            # outline = ball colour this much darker
TEXTURE_AMOUNT = 0.20            # pebble texture multiplied over the print
INNER_SHADOW = 0.35              # darkening along the top inner edge (pressed in, light from above)
INNER_SHADOW_OFFSET = 0.035      # fraction of cap height
INNER_SHADOW_SOFTNESS = 0.05     # fraction of cap height

PREVIEW_BG = (0x1A, 0x1A, 0x22)


def darker(rgb, amount):
    return tuple(int(round(c * (1.0 - amount))) for c in rgb)


def fill_holes(mask):
    """Fill regions of ~mask that do not touch the image border."""
    H, W = mask.shape
    rows, starts, ends, labels = components_by_run(~mask)
    _, comp = np.unique(labels, return_inverse=True)
    touches = np.bincount(comp, weights=(rows == 0) | (rows == H - 1) | (starts == 0) | (ends == W)) > 0
    hole = ~touches[comp]
    return mask | paint_runs(mask.shape, rows[hole], starts[hole], ends[hole])


def fit_circle(mask):
    ys, xs = np.nonzero(mask & dilate(~mask, 1))
    A = np.c_[2 * xs, 2 * ys, np.ones(len(xs))]
    cx, cy, c = np.linalg.lstsq(A, xs ** 2 + ys ** 2, rcond=None)[0]
    return float(cx), float(cy), float(math.sqrt(c + cx * cx + cy * cy))


class Ball:
    """Everything derived from the master once: matte, paint/seam split,
    shading, pebble texture and the ball's geometry."""

    def __init__(self, path):
        self.maps, info = analyse_master(path, smooth_shading=False, id_hues=(0.0,), part_names=("ball",))
        x0, y0, x1, y1 = info["crop"]
        self.crop_size = (x1 - x0, y1 - y0)
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)[y0:y1, x0:x1] / 255.0
        mx, mn = rgb.max(-1), rgb.min(-1)
        sat = (mx - mn) / np.maximum(mx, 1e-4)

        # Matte: the solid ball with holes filled, eroded and feathered so the
        # master's pink halo goes.
        solid = fill_holes(self.maps.alpha > 0.5)
        core = ~dilate(~solid, EDGE_ERODE)
        self.alpha = np.clip(blur(core.astype(np.float32), EDGE_FEATHER), 0.0, 1.0)

        # Seams are dark and grey; everything else is ball, including the
        # washed-out, nearly white top.
        seam = smoothstep(0.25, 0.12, sat) * smoothstep(0.62, 0.48, mx)
        self.paint = np.clip(blur(1.0 - seam, 0.7), 0.0, 1.0)
        self.seam = (seam > 0.5) & solid

        self.cx, self.cy, self.r = fit_circle(core)
        # Middle seam: the band of seam rows closest to the centre.
        H, W = core.shape
        cols = slice(int(self.cx - 0.3 * self.r), int(self.cx + 0.3 * self.r))
        count = self.seam[:, cols].sum(1).astype(float)
        count[np.abs(np.arange(H) - self.cy) > 0.2 * self.r] = 0
        band = np.nonzero(count > 0.6 * count.max())[0]
        self.seam_y = float(band.mean())
        self.seam_thickness = int(band.max() - band.min() + 1)

        self.texture = self._pebble_texture(rgb, solid)

    def _pebble_texture(self, rgb, solid):
        """Pebble detail in 0..1 (1 = pebble tops), with the seams filled in
        from just above so no seam shows through the print."""
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        ball = (solid & ~self.seam).astype(np.float32)
        sigma = 4.0 * self.r / 555.0
        local = blur(lum * ball, sigma) / np.maximum(blur(ball, sigma), 1e-6)
        ratio = np.where(ball > 0, lum / np.maximum(local, 1e-4), 1.0)
        tex = np.clip(ratio / np.percentile(ratio[ball > 0], 95), 0.0, 1.0)
        # Replace the seams (and their raised lips) with a block of pebbles
        # copied from far enough above, so the copy keeps its structure.
        lip = max(2, round(5 * self.r / 555.0))
        hidden = dilate(self.seam, lip) & solid
        step = self.seam_thickness + 2 * lip + 4
        pending = hidden.copy()
        source_ok = solid & ~hidden
        for m in range(1, 12):
            if not pending.any():
                break
            k = m * step
            shifted = np.ones_like(tex)
            shifted[k:] = tex[:-k]
            ok = np.zeros_like(pending)
            ok[k:] = source_ok[:-k]
            take = pending & ok
            tex[take] = shifted[take]
            pending &= ~take
        tex[pending] = 1.0
        return tex

    def render(self, ball_rgb):
        """The recoloured ball in linear light."""
        team = srgb_to_linear(np.asarray(ball_rgb, dtype=np.float32) / 255.0)
        lit = compress_highlights(shoulder(self.maps.shade[..., None] * team) + self.maps.gloss[..., None])
        p = self.paint[..., None]
        return p * lit + (1.0 - p) * self.maps.neutral[..., None]


def text_masks(text, font_path, cap_flat, width_flat, outline_px, ss):
    """Flat masks at ss x master scale: (fill, letters+outline, inner shadow,
    centre column, centre row)."""
    probe = load_font(font_path, 400)
    cap_ratio = -probe.getbbox("H", anchor="ls")[1] / 400.0
    font = load_font(font_path, max(8, round(cap_flat * ss / cap_ratio)))
    cap = cap_ratio * font.size
    track = TEXT_TRACKING * cap
    pad = int(cap * 0.5) + 8
    advance = [font.getlength(text[:i]) + i * track for i in range(len(text) + 1)]
    W, H = int(math.ceil(advance[-1])) + 2 * pad, int(math.ceil(cap)) + 2 * pad
    canvas = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(canvas)
    baseline = pad + cap
    for i, ch in enumerate(text):
        draw.text((pad + advance[i], baseline), ch, font=font, fill=255, anchor="ls")
    l, t, r, b = canvas.getbbox()
    # Widen or narrow to the target width; if the text is too wide even at
    # the narrowest allowed, the whole word shrinks.
    stretch = width_flat * ss / (r - l)
    scale_all = 1.0
    if stretch < STRETCH_LIMITS[0]:
        scale_all = stretch / STRETCH_LIMITS[0]
    stretch = min(max(stretch, STRETCH_LIMITS[0]), STRETCH_LIMITS[1])
    sx, sy = stretch * scale_all, scale_all
    canvas = canvas.resize((max(1, round(W * sx)), max(1, round(H * sy))), Image.LANCZOS)
    fill = np.asarray(canvas, dtype=np.float32) / 255.0
    cx, cy = (l + r) / 2 * sx, (baseline - cap / 2) * sy
    cap *= sy

    # Outline: grow the letters by the outline width (after the stretch, so
    # it stays even all round).
    radius = outline_px * ss
    margin = int(math.ceil(radius)) + 2
    fill = np.pad(fill, margin)
    cx, cy = cx + margin, cy + margin
    dist = grow_distance(fill > 0.5, np.ones(fill.shape, bool), limit=margin + 2)
    outer = np.maximum(fill, np.clip(radius + 0.5 - dist, 0.0, 1.0))

    # Inner shadow: the outside of the letters, nudged down and blurred, as
    # seen through the letters, so the top inner edge is shaded.
    off = max(1, round(INNER_SHADOW_OFFSET * cap))
    outside = np.pad(1.0 - fill, ((off, 0), (0, 0)), constant_values=1.0)[:fill.shape[0]]
    soft = Image.fromarray(np.round(outside * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(INNER_SHADOW_SOFTNESS * cap))
    shadow = fill * (np.asarray(soft, dtype=np.float32) / 255.0)
    return fill, outer, shadow, cx, cy


def print_text(ball, base_lin, text, ball_rgb, text_rgb, outline_rgb, font_path, outline_px, ss=4):
    """Composite the printed abbreviation onto the rendered ball (linear)."""
    rv = BULGE_RADIUS * ball.r
    # Flat sizes whose projection onto the sphere has the target screen size.
    cap_flat = 2 * rv * math.asin(min(TEXT_CAP * ball.r / rv, 1.0))
    width_flat = 2 * rv * math.asin(min(TEXT_WIDTH * ball.r / rv, 1.0))
    fill_m, outer_m, shadow_m, mcx, mcy = text_masks(text, font_path, cap_flat, width_flat, outline_px, ss)
    layout = TextLayout(center=(ball.cx, ball.seam_y), max_width=1e9, cap_height=cap_flat, tilt_deg=0.0,
                        sphere_center=(ball.cx, ball.cy), sphere_radius=rv)
    warp = lambda m: place_lettering(m, mcx, mcy, ss, layout, base_lin.shape[:2], style="3d")
    fill, outer, shadow = warp(fill_m), warp(outer_m), warp(shadow_m)

    rows = np.nonzero(outer.max(1) > 0.05)[0]
    t = np.zeros(outer.shape[0], np.float32)
    if len(rows):
        t = np.clip((np.arange(outer.shape[0]) - rows[0]) / max(rows[-1] - rows[0], 1), 0.0, 1.0)
    grad = (1.0 + FILL_GRADIENT * (1.0 - 2.0 * t))[:, None, None]
    fill_srgb = np.clip(np.asarray(text_rgb, dtype=np.float32) / 255.0 * grad, 0.0, 1.0)
    fill_lin = srgb_to_linear(fill_srgb) * (1.0 - INNER_SHADOW * shadow[..., None])
    outline_lin = srgb_to_linear(np.asarray(outline_rgb, dtype=np.float32) / 255.0)
    printed = (1.0 - TEXTURE_AMOUNT * (1.0 - ball.texture))[..., None]

    out = base_lin + outer[..., None] * (outline_lin * printed - base_lin)
    return out + fill[..., None] * (fill_lin * printed - out)


def read_balls(path: Path):
    teams = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = {f.strip().lower(): f for f in (reader.fieldnames or [])}
        missing = [c for c in ("abbr", "ball_hex") if c not in fields]
        if missing:
            raise SystemExit(f"{path}: missing column(s): {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            get = lambda key: (row.get(fields.get(key, ""), "") or "").strip()
            if not any((v or "").strip() for v in row.values()):
                continue
            abbr = get("abbr")
            if not abbr:
                raise SystemExit(f"{path}:{line}: empty abbr")
            try:
                ball = parse_hex(get("ball_hex"))
                teams.append({
                    "abbr": abbr, "team": get("team") or abbr, "ball": ball,
                    "text": parse_hex(get("text_hex")) if get("text_hex") else TEXT_FILL,
                    "outline": parse_hex(get("outline_hex")) if get("outline_hex") else darker(ball, OUTLINE_DARKEN),
                })
            except ValueError as exc:
                raise SystemExit(f"{path}:{line}: {exc}")
    if not teams:
        raise SystemExit(f"{path}: no teams found")
    return teams


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", type=Path, default=HERE / "master_ball.png")
    ap.add_argument("--teams", type=Path, default=HERE / "balls.csv")
    ap.add_argument("--out", type=Path, default=HERE / "output" / "balls")
    ap.add_argument("--font", type=Path, default=HERE / "fonts" / "Anton-Regular.ttf",
                    help="TTF/OTF for the abbreviation (default: Anton, bold condensed block)")
    ap.add_argument("--size", type=int, default=1024, help="square canvas size in px (default 1024)")
    ap.add_argument("--margin", type=float, default=0.04, help="empty border, fraction of the canvas")
    ap.add_argument("--no-preview", action="store_true", help="skip preview.png")
    args = ap.parse_args(argv)

    teams = read_balls(args.teams)
    print(f"analysing {args.master} ...")
    ball = Ball(args.master)
    print(f"  ball centre ({ball.cx:.0f}, {ball.cy:.0f}) radius {ball.r:.0f}, middle seam y={ball.seam_y:.0f}")
    # Outline width is given in output pixels; convert to master pixels.
    to_canvas = args.size * (1 - 2 * args.margin) / max(ball.crop_size)
    outline_px = OUTLINE_PX * (args.size / 1024) / to_canvas

    args.out.mkdir(parents=True, exist_ok=True)
    entries, seen = [], {}
    for team in teams:
        slug = slugify(team["abbr"])
        if slug in seen:
            print(f"  warning: {team['abbr']!r} overwrites an earlier {slug}.png", file=sys.stderr)
        seen[slug] = True
        lin = ball.render(team["ball"])
        lin = print_text(ball, lin, team["abbr"], team["ball"], team["text"], team["outline"], args.font, outline_px)
        rgb8 = np.round(linear_to_srgb(lin) * 255).astype(np.uint8)
        a8 = np.round(ball.alpha * 255).astype(np.uint8)
        img = fit_to_canvas(Image.fromarray(np.dstack([rgb8, a8]), "RGBA"), args.size, args.margin)
        img.save(args.out / f"{slug}.png")

        swatches = [("ball", team["ball"]), ("text", team["text"]), ("outline", team["outline"])]
        entries.append((team["team"], [img], swatches))
        print(f"  {team['abbr']}: ball {to_hex(team['ball'])}, text {to_hex(team['text'])}, "
              f"outline {to_hex(team['outline'])} -> {slug}.png")

    if not args.no_preview:
        contact_sheet(entries, args.out / "preview.png", HERE / "fonts" / "Oswald-Bold.ttf", thumb=380,
                      background=PREVIEW_BG, checker_tiles=False)
        print(f"wrote {args.out / 'preview.png'}")


if __name__ == "__main__":
    main()

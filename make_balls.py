#!/usr/bin/env python3
"""Generate team-coloured basketballs from one master render.

The master image is a pebbled, solid red basketball with black seams on a
white background. For every row of the balls CSV (abbr, ball_hex, plus
optional team, text_hex and outline_hex) this writes

    <out>/<ABBR>.png      transparent PNG, same canvas, size and position for all
    <out>/preview.png     grid of every ball on the app's dark card colour

The red takes the team colour with the master's shading kept (the pebbling
softened to a satin finish, with a light rim around the edge); the black
seams are left as they are. The abbreviation is printed big across the
middle in a heavy collegiate block letter: white, a thin outline in the
team's accent colour, wrapped onto the sphere and lit like the ball, with
the middle seam running across it. The master's faint pink edge halo is
trimmed off.

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
from PIL import Image, ImageDraw

from make_helmets import (
    HERE, TextLayout, analyse_master, blur, components_by_run, compress_highlights, contact_sheet,
    dilate, fit_to_canvas, grow_distance, linear_to_srgb, load_font, paint_runs, parse_hex,
    place_lettering, shoulder, slugify, smoothstep, srgb_to_linear, to_hex,
)

EDGE_INSET = 4                   # the matte is a clean circle this far (master px) inside the edge
PEBBLE_SMOOTH = 0.7              # 0 = full pebble texture, 1 = smooth satin
RIM_LIGHT = 0.40                 # thin light rim around the ball's edge (additive, linear light)
RIM_WIDTH = 12                   # rim light width, master px at the 555 px reference radius
SPECK_AREA = 60                  # seam specks smaller than this (master px) are cleaned out

TEXT_CAP = 0.45                  # cap height, fraction of the ball's height
TEXT_WIDTH = 0.72                # text width, fraction of the ball's width
STRETCH_LIMITS = (0.80, 1.25)    # how far the letters may be narrowed / widened to hit TEXT_WIDTH
TEXT_TRACKING = 0.03             # letter spacing, fraction of cap height
EMBOLDEN = 0.04                  # extra stroke weight, fraction of cap height (Graduate is light)
# Graduate's own M fills in when it is bolded, so the M is drawn from its
# measurements (font units, cap height 750) with thinner diagonals and a
# little more width, which keeps its notches open.
M_WIDTH = 1130                   # advance width (Graduate: 946)
M_DIAGONAL = 92                  # diagonal thickness before bolding, same as the stems (Graduate: 83)
M_FOOT = 282                     # inner foot serifs end here (Graduate: 312)
M_V_BOTTOM = 96                  # flat bottom of the middle V, on the baseline
BULGE_RADIUS = 1.0               # sphere the text wraps on, x ball radius (1 = full curve)

TEXT_FILL = (0xF4, 0xF4, 0xF2)
OUTLINE = 0.03                   # outline thickness, fraction of cap height
OUTLINE_DARKEN = 0.45            # default outline = ball colour this much darker
LETTER_SHADING = 1.0             # how much of the ball's light falls on the letters
LETTER_LEVEL = 0.80              # letter brightness in the middle of the text (highlights still reach white)
LETTER_GRADIENT = 0.15           # letters darker towards the bottom
EDGE_SHADE = 0.35                # letters darken softly towards their edges (inlaid look)
EDGE_SHADE_SOFTNESS = 5.0
BEVEL = 0.6                      # engraved edge: dark along the top inner edge, light along the bottom
BEVEL_SOFTNESS = 2.5             # bevel width (master px at the 555 px reference radius)
GROOVE_SHADOW = 0.45             # shadow on the letters beside the middle seam groove
GROOVE_SOFTNESS = 2.5

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


def drop_small(mask, min_area):
    """Remove connected regions of mask smaller than min_area pixels."""
    rows, starts, ends, labels = components_by_run(mask)
    if len(rows) == 0:
        return mask
    _, comp = np.unique(labels, return_inverse=True)
    keep = (np.bincount(comp, weights=ends - starts) >= min_area)[comp]
    return paint_runs(mask.shape, rows[keep], starts[keep], ends[keep])


def smooth_over(values, weight, sigma):
    return blur(values * weight, sigma) / np.maximum(blur(weight, sigma), 1e-6)


class Ball:
    """Everything derived from the master once: matte, paint/seam split,
    shading and the ball's geometry."""

    def __init__(self, path):
        self.maps, info = analyse_master(path, smooth_shading=False, id_hues=(0.0,), part_names=("ball",))
        x0, y0, x1, y1 = info["crop"]
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)[y0:y1, x0:x1] / 255.0
        mx, mn = rgb.max(-1), rgb.min(-1)
        sat = (mx - mn) / np.maximum(mx, 1e-4)

        # Matte: a clean circle just inside the ball's edge. The master's edge
        # has a faint pink halo and the seam ends poke out; the circle drops both.
        solid = fill_holes(self.maps.alpha > 0.5)
        self.cx, self.cy, self.r = fit_circle(solid)
        H, W = solid.shape
        ys, xs = np.mgrid[0:H, 0:W]
        dist = np.hypot(xs - self.cx, ys - self.cy)
        k = self.r / 555.0
        edge = self.r - EDGE_INSET * k
        self.alpha = np.clip(edge - dist + 0.5, 0.0, 1.0).astype(np.float32)
        rho = dist / edge

        # Seams are dark and grey; everything else is ball, including the
        # washed-out, nearly white top. Specks are cleaned out so the seam
        # edges stay smooth.
        seam = (smoothstep(0.25, 0.12, sat) * smoothstep(0.62, 0.48, mx)) > 0.5
        # Drop specks by size, not thickness: the curved seams get very thin
        # where they run edge-on along the sides and must survive.
        seam = drop_small(seam, SPECK_AREA * k * k)
        seam = ~drop_small(~seam, SPECK_AREA * k * k)    # fill pinholes
        seam_soft = np.clip(blur(seam.astype(np.float32), 0.8 * k), 0.0, 1.0)
        self.paint = 1.0 - seam_soft

        # Satin finish: soften the pebbling, add a light rim.
        shade, gloss = self.maps.shade, self.maps.gloss
        self.shade = shade + PEBBLE_SMOOTH * (smooth_over(shade, self.paint, 2.5 * k) - shade)
        self.gloss = gloss + PEBBLE_SMOOTH * (smooth_over(gloss, self.paint, 2.5 * k) - gloss)
        self.gloss = self.gloss + RIM_LIGHT * smoothstep(1.0 - RIM_WIDTH * k / self.r, 1.0, rho) ** 2

        # How a white surface would be lit at each point, for the letters.
        self.light = smooth_over(shade, self.paint, 3 * k) + smooth_over(self.gloss, self.paint, 3 * k)

        # Middle seam: the band of seam rows closest to the centre. Where it
        # crosses the letters it is drawn as a straight band with clean edges.
        cols = slice(int(self.cx - 0.3 * self.r), int(self.cx + 0.3 * self.r))
        count = seam[:, cols].sum(1).astype(float)
        count[np.abs(np.arange(H) - self.cy) > 0.2 * self.r] = 0
        band = np.nonzero(count > 0.5 * count.max())[0]
        self.seam_y = float(band.mean())
        top, bottom = band.min() - 0.5, band.max() + 0.5
        y = np.arange(H, dtype=np.float32)[:, None]
        band_cov = np.clip(np.minimum(y - top, bottom - y) + 0.5, 0.0, 1.0) * np.ones((1, W), np.float32)
        zone = (np.abs(y - self.seam_y) < (bottom - top) / 2 + 6 * k) & (np.abs(xs - self.cx) < 0.9 * self.r)
        self.text_cut = np.where(zone, band_cov, seam_soft)          # where seams cut through the print
        self.groove = blur(np.where(zone, band_cov, 0.0), GROOVE_SOFTNESS * k)

    def render(self, ball_rgb):
        """The recoloured ball in linear light."""
        team = srgb_to_linear(np.asarray(ball_rgb, dtype=np.float32) / 255.0)
        lit = compress_highlights(shoulder(self.shade[..., None] * team) + self.gloss[..., None])
        p = self.paint[..., None]
        return p * lit + (1.0 - p) * self.maps.neutral[..., None]


def m_outline(width=None, diagonal=None, foot=None, v_bottom=None):
    """Collegiate M as a polygon in font units (y up, baseline 0, cap 750),
    matching Graduate's stems, slab serifs and feet: diagonals as thick as
    the stems meet in a V that comes down to the baseline, with a V notch
    from the top and narrow notches between each foot and the V."""
    W = M_WIDTH if width is None else width
    d = M_DIAGONAL if diagonal is None else diagonal
    foot = M_FOOT if foot is None else foot          # inner foot serifs end here
    vb = M_V_BOTTOM if v_bottom is None else v_bottom  # width of the V's flat bottom
    cx = W / 2
    # Inner edge of the left diagonal: from the stem (Graduate's 242, 617)
    # down to the left corner of the V's flat bottom on the baseline. The
    # outer edge runs parallel to it, d further along the stroke's normal.
    x1, y1 = 242.0, 617.0
    x2, y2 = cx - vb / 2, 0.0
    slope = (x2 - x1) / (y2 - y1)                  # dx per unit of y along the diagonal
    shift = d * math.hypot(1.0, slope)             # horizontal offset of the outer edge
    y_v = (x2 + shift - cx) / -slope               # outer edges meet: bottom of the top notch
    x_top = cx + (750.0 - y_v) * slope             # outer edge meets the top
    pts = [(80, 750), (x_top, 750), (cx, y_v), (W - x_top, 750), (W - 80, 750), (W - 80, 670),
           (W - 150, 670), (W - 150, 80), (W - 80, 80), (W - 80, 0), (W - foot, 0), (W - foot, 80),
           (W - 242, 80), (W - 242, y1), (W - x2, y2), (x2, y2), (x1, y1), (242, 80), (foot, 80),
           (foot, 0), (80, 0), (80, 80), (150, 80), (150, 670), (80, 670)]
    return pts, W


def draw_custom(img, ch, x, baseline, scale, stroke, supersample=2):
    """Draw a custom glyph onto an L image at pen position x, grown by
    `stroke` px with round joins, like a stroked font glyph."""
    pts, _ = m_outline()
    k = supersample
    xs = [x + px * scale for px, _ in pts]
    ys = [baseline - py * scale for _, py in pts]
    x0, y0 = int(min(xs) - stroke - 2), int(min(ys) - stroke - 2)
    x1, y1 = int(max(xs) + stroke + 3), int(max(ys) + stroke + 3)
    big = Image.new("L", ((x1 - x0) * k, (y1 - y0) * k), 0)
    draw = ImageDraw.Draw(big)
    poly = [((px - x0) * k, (py - y0) * k) for px, py in zip(xs, ys)]
    draw.polygon(poly, fill=255)
    if stroke > 0:
        r = stroke * k
        draw.line(poly + [poly[0]], fill=255, width=int(round(2 * r)), joint="curve")
        for px, py in poly:
            draw.ellipse((px - r, py - r, px + r, py + r), fill=255)
    small = big.resize((x1 - x0, y1 - y0), Image.BOX)
    region = (x0, y0, x1, y1)
    img.paste(Image.fromarray(np.maximum(np.asarray(img.crop(region)), np.asarray(small))), region)


CUSTOM_GLYPHS = {"M": m_outline}
# Letter pairs that touch at the default spacing get this much extra room
# (fraction of cap height): the L's foot runs into the A's left leg.
PAIR_SPACE = {"LA": 0.08}


def text_masks(text, font_path, cap_flat, width_flat, ss):
    """Flat masks at ss x master scale: (fill, letters+outline, centre column,
    centre row)."""
    probe = load_font(font_path, 400)
    cap_ratio = -probe.getbbox("H", anchor="ls")[1] / 400.0
    # The letters are made heavier by stroking them in their own colour; the
    # stroke adds to the cap height, so start the font a little smaller.
    cap_target = cap_flat * ss
    font = load_font(font_path, max(8, round(cap_target / (cap_ratio + 2 * EMBOLDEN))))
    bold = max(1, round(EMBOLDEN * cap_target))
    cap = cap_ratio * font.size + 2 * bold
    track = TEXT_TRACKING * cap + 2 * bold
    pad = int(cap * 0.4) + bold + 8
    units = font.size / 1000.0                     # px per font unit (Graduate: 1000 per em)
    if any(ch in CUSTOM_GLYPHS for ch in text):
        widths = [CUSTOM_GLYPHS[ch]()[1] * units if ch in CUSTOM_GLYPHS else font.getlength(ch) for ch in text]
        advance = [sum(widths[:i]) + i * track for i in range(len(text) + 1)]
    else:
        advance = [font.getlength(text[:i]) + i * track for i in range(len(text) + 1)]
    advance = [a + sum(PAIR_SPACE.get(text[j - 1:j + 1], 0.0) for j in range(1, i + 1)) * cap
               for i, a in enumerate(advance)]
    W, H = int(math.ceil(advance[-1])) + 2 * pad, int(math.ceil(cap)) + 2 * pad
    baseline = pad + cap - bold

    canvas = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(canvas)
    for i, ch in enumerate(text):
        x = pad + advance[i] + bold
        if ch in CUSTOM_GLYPHS:
            draw_custom(canvas, ch, x, baseline, units, bold)
        else:
            draw.text((x, baseline), ch, font=font, fill=255, anchor="ls", stroke_width=bold, stroke_fill=255)
    l, t, r, b = canvas.getbbox()

    # Narrow or widen a little to hit the target width; a word still too wide
    # shrinks as a whole.
    sx = width_flat * ss / (r - l)
    k = 1.0
    if sx < STRETCH_LIMITS[0]:
        k = sx / STRETCH_LIMITS[0]
    sx = min(max(sx, STRETCH_LIMITS[0]), STRETCH_LIMITS[1]) * k
    canvas = canvas.resize((max(1, round(W * sx)), max(1, round(H * k))), Image.LANCZOS)
    fill = np.asarray(canvas, dtype=np.float32) / 255.0
    cx, cy = (l + r) / 2 * sx, (t + b) / 2 * k
    cap *= k

    radius = OUTLINE * cap
    margin = int(math.ceil(radius)) + 2
    fill = np.pad(fill, margin)
    dist = grow_distance(fill > 0.5, np.ones(fill.shape, bool), limit=margin + 2)
    outer = np.maximum(fill, np.clip(radius + 0.5 - dist, 0.0, 1.0))
    return fill, outer, cx + margin, cy + margin


def print_text(ball, base_lin, text, text_rgb, outline_rgb, font_path, ss=4):
    """Composite the printed abbreviation onto the rendered ball (linear)."""
    rv = BULGE_RADIUS * ball.r
    # Flat sizes whose projection onto the sphere has the target screen size.
    cap_flat = 2 * rv * math.asin(min(TEXT_CAP * ball.r / rv, 1.0))
    width_flat = 2 * rv * math.asin(min(TEXT_WIDTH * ball.r / rv, 1.0))
    fill_m, outer_m, mcx, mcy = text_masks(text, font_path, cap_flat, width_flat, ss)
    layout = TextLayout(center=(ball.cx, ball.seam_y), max_width=1e9, cap_height=cap_flat, tilt_deg=0.0,
                        sphere_center=(ball.cx, ball.cy), sphere_radius=rv)
    warp = lambda m: place_lettering(m, mcx, mcy, ss, layout, base_lin.shape[:2], style="3d")
    # The seams run across the print.
    cut = 1.0 - ball.text_cut
    fill, outer = warp(fill_m) * cut, warp(outer_m) * cut

    # Light the letters like the ball: how a white surface is lit at each
    # point, relative to the middle of the text.
    inside = fill > 0.5
    ref = np.median(ball.light[inside]) if inside.any() else 1.0
    light = LETTER_LEVEL * np.clip(ball.light / max(ref, 1e-6), 0.35, 1.4) ** LETTER_SHADING
    rows = np.nonzero(outer.max(1) > 0.05)[0]
    if len(rows):
        t = np.clip((np.arange(outer.shape[0]) - rows[0]) / max(rows[-1] - rows[0], 1), 0.0, 1.0)
        light = light * (1.0 - LETTER_GRADIENT * t)[:, None]

    # Engraved edges, light from the top left: the recessed print is shaded
    # along its top inner edge and catches light along its bottom one.
    k = ball.r / 555.0
    soft = BEVEL_SOFTNESS * k
    gy, gx = np.gradient(blur(outer, soft))
    bevel = np.clip(-(0.45 * gx + 0.89 * gy) * (soft / 0.4), -1.0, 1.0)
    edge = blur(1.0 - outer, EDGE_SHADE_SOFTNESS * k)
    light = light * (1.0 + BEVEL * bevel) * (1.0 - GROOVE_SHADOW * ball.groove) * (1.0 - EDGE_SHADE * edge)

    lin = lambda rgb: srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    fill_lin = compress_highlights(lin(text_rgb) * light[..., None])
    outline_lin = compress_highlights(lin(outline_rgb) * light[..., None])

    out = base_lin + outer[..., None] * (outline_lin - base_lin)
    return out + fill[..., None] * (fill_lin - out)


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
    ap.add_argument("--font", type=Path, default=HERE / "fonts" / "Graduate-Regular.ttf",
                    help="TTF/OTF for the abbreviation (default: Graduate, collegiate block, emboldened)")
    ap.add_argument("--size", type=int, default=1024, help="square canvas size in px (default 1024)")
    ap.add_argument("--margin", type=float, default=0.04, help="empty border, fraction of the canvas")
    ap.add_argument("--no-preview", action="store_true", help="skip preview.png")
    args = ap.parse_args(argv)

    teams = read_balls(args.teams)
    print(f"analysing {args.master} ...")
    ball = Ball(args.master)
    print(f"  ball centre ({ball.cx:.0f}, {ball.cy:.0f}) radius {ball.r:.0f}, middle seam y={ball.seam_y:.0f}")

    args.out.mkdir(parents=True, exist_ok=True)
    entries, seen = [], set()
    for team in teams:
        slug = slugify(team["abbr"])
        if slug in seen:
            print(f"  warning: {team['abbr']!r} overwrites an earlier {slug}.png", file=sys.stderr)
        seen.add(slug)
        lin = ball.render(team["ball"])
        lin = print_text(ball, lin, team["abbr"], team["text"], team["outline"], args.font)
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

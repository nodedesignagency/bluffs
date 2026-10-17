#!/usr/bin/env python3
"""Generate team-coloured basketballs from one ID-coloured master render.

The master image is a glossy, front-facing basketball on a light background,
painted with ID colours:

    red = ball surface        blue = seams

For every row of the balls CSV (city, abbr, primary_hex, secondary_hex, plus
optional seam_hex, text_hex and outline_hex) this writes

    <out>/<City>.png      transparent PNG, same canvas, size and position for all
    <out>/preview.png     contact sheet with every team

The ball takes the primary colour with the master's gloss and shading; the
seams default to the primary 35% darker. The abbreviation sits on the
horizontal seam, nudged left of centre to clear the vertical seam, wrapped
around the ball in a chunky rounded font: secondary fill, thin darker outline
and a soft inner shadow so it reads as printed. The master's soft edge glow is
trimmed off and replaced by a rim glow in the team colour. Leave abbr blank
for no text. Shares the recolouring code with make_helmets.py.

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
from PIL import Image

from make_helmets import (
    HERE, TextLayout, analyse_master, blur, components_by_run, compress_highlights, contact_sheet,
    dilate, fit_to_canvas, linear_to_srgb, paint_runs, parse_hex, place_lettering, render_lettering,
    shoulder, slugify, smoothstep, srgb_to_linear, to_hex,
)

ID_HUES = (0.0, 240.0)               # red ball, blue seams
PART_NAMES = ("ball", "seam")

SEAM_DARKEN = 0.35                   # default seam = primary this much darker
EDGE_ERODE = 3                       # px trimmed off the master's edge (removes its glow)
EDGE_FEATHER = 1.0                   # px, softness of the new edge

TEXT_CAP = 0.36                      # starting cap height, fraction of ball radius
TEXT_MIN_SHIFT = 0.05                # always nudge the text at least this far left (x radius)
TEXT_MARGIN = 0.03                   # clearance from other seams and the rim (x radius)
TEXT_OUTLINE = 0.045                 # outline thickness, fraction of cap height
TEXT_TRACKING = 0.02
INNER_SHADOW = 0.45                  # darkening at the shadowed inner edge of the letters
INNER_SHADOW_OFFSET = 0.045          # fraction of cap height, towards bottom-right (light is top-left)
INNER_SHADOW_SOFTNESS = 0.06         # blur, fraction of cap height
TEXT_GLOSS_CUT = 0.25                # the gloss mostly carries over the print
PRINT_SMOOTH = 0.015                 # how far (x image width) to borrow ball shading across the seam

GLOW_STRENGTH = 0.85                 # rim glow opacity at the ball's edge is about half of this
GLOW_SIZE = 0.014                    # glow blur, fraction of the canvas size


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


def ball_matte(maps):
    """New alpha from the ball's saturated paint only (drops the soft glow the
    master has around its edge), eroded a little and feathered."""
    core = fill_holes((maps.alpha > 0.5) & (maps.paint > 0.5))
    core = ~dilate(~core, EDGE_ERODE)
    return np.clip(blur(core.astype(np.float32), EDGE_FEATHER), 0.0, 1.0) * maps.alpha


def fit_circle(alpha):
    solid = alpha > 0.5
    ys, xs = np.nonzero(solid & dilate(~solid, 1))
    A = np.c_[2 * xs, 2 * ys, np.ones(len(xs))]
    cx, cy, c = np.linalg.lstsq(A, xs ** 2 + ys ** 2, rcond=None)[0]
    return float(cx), float(cy), float(math.sqrt(c + cx * cx + cy * cy))


class BallText:
    """Finds the horizontal seam and the vertical seam on the right, and fits
    the abbreviation between them."""

    def __init__(self, maps, alpha, font):
        self.font = font
        self.shape = alpha.shape
        cx, cy, r = fit_circle(alpha)
        self.cx, self.cy, self.r = cx, cy, r
        H, W = self.shape
        ys, xs = np.mgrid[0:H, 0:W]
        seam = (alpha > 0.5) & (maps.weights[..., 1] > 0.5)

        # Horizontal seam: the row band near the centre with the most seam.
        rows = np.arange(H)
        near = np.abs(rows - cy) < 0.3 * r
        count = (seam & (np.abs(xs - cx) < 0.5 * r)).sum(1) * near
        band = np.nonzero(count > 0.6 * count.max())[0]
        self.seam_y = float(band.mean())
        half = (band.max() - band.min()) / 2 + 3

        # Vertical seam on the right, just above and below the horizontal one.
        probe = (np.abs(np.abs(ys - self.seam_y) - (half + 0.08 * r)) < 3) & (xs > cx) & seam
        self.seam_x = float(xs[probe].min()) if probe.any() else cx + 0.6 * r

        # Text may not touch any other seam, nor come near the rim.
        self.margin = TEXT_MARGIN * r
        other = seam & (np.abs(ys - self.seam_y) > half)
        rim = np.hypot(xs - cx, ys - cy) > r - self.margin
        self.forbidden = dilate(other, max(1, round(self.margin))) | rim

    def layout(self, cap, x):
        return TextLayout(center=(x, self.seam_y), max_width=1e9, cap_height=cap, tilt_deg=0.0,
                          tracking=TEXT_TRACKING, min_squeeze=1.0, outline=TEXT_OUTLINE,
                          sphere_center=(self.cx, self.cy), sphere_radius=self.r)

    def _place(self, text, cap, x, ss):
        layout = self.layout(cap, x)
        fill_m, outer_m, mcx, mcy = render_lettering(text, self.font, layout, ss=ss, outline=True)
        # Inner shadow: the outside of the letters, nudged down-right and
        # blurred, as seen through the letters (light comes from top-left).
        c = cap * ss
        off = max(1, round(INNER_SHADOW_OFFSET * c))
        outside = np.pad(1.0 - fill_m, ((off, 0), (off, 0)), constant_values=1.0)[:fill_m.shape[0], :fill_m.shape[1]]
        shadow_m = fill_m * blur(outside, INNER_SHADOW_SOFTNESS * c)
        warp = lambda m: place_lettering(m, mcx, mcy, ss, layout, self.shape, style="3d")
        return warp(fill_m), warp(outer_m), warp(shadow_m)

    def place(self, text, ss=4, shrink=0.94, tolerance=0.002):
        """(fill, letters+outline, inner shadow) coverage maps."""
        cap = TEXT_CAP * self.r
        limit = self.seam_x - self.margin
        for _ in range(40):
            x = self.cx - TEXT_MIN_SHIFT * self.r
            for _ in range(4):           # slide left until clear of the vertical seam
                fill, outer, shadow = self._place(text, cap, x, ss)
                cols = np.nonzero(outer.max(0) > 0.05)[0]
                over = (cols.max() - limit) if len(cols) else 0
                if over <= 0:
                    break
                x -= over + 1
            total = outer.sum()
            if total > 0 and (outer * self.forbidden).sum() / total <= tolerance:
                return fill, outer, shadow
            cap *= shrink
        return fill, outer, shadow


def render_ball(maps, alpha, colors, text=None):
    """colors: (primary, seam, text fill, outline) as sRGB tuples. text:
    (fill, outer, shadow) coverage maps or None."""
    lin = lambda rgb: srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    primary, seam, fill_rgb, outline_rgb = colors
    albedo = maps.weights @ np.stack([lin(primary), lin(seam)])
    shade, gloss, paint = maps.shade, maps.gloss, maps.paint
    if text is not None:
        fill, outer, shadow = text
        albedo = albedo + outer[..., None] * (lin(outline_rgb) - albedo)
        ink = lin(fill_rgb) * (1.0 - INNER_SHADOW * shadow[..., None])
        albedo = albedo + fill[..., None] * (ink - albedo)
        # The print lies flat over the seam: under it, take the light of the
        # smooth ball surface either side instead of the seam's ridges.
        ball = maps.weights[..., 0] * (alpha > 0.5)
        sigma = PRINT_SMOOTH * alpha.shape[1]
        norm = np.maximum(blur(ball, sigma), 1e-6)
        flat_shade = blur(shade * ball, sigma) / norm
        flat_gloss = blur(gloss * ball, sigma) / norm
        shade = shade + outer * (flat_shade - shade)
        gloss = (gloss + outer * (flat_gloss - gloss)) * (1.0 - TEXT_GLOSS_CUT * outer)
        paint = np.maximum(paint, outer)
    lit = compress_highlights(shoulder(shade[..., None] * albedo) + gloss[..., None])
    out = paint[..., None] * lit + (1.0 - paint[..., None]) * maps.neutral[..., None]
    rgb8 = np.round(linear_to_srgb(out) * 255).astype(np.uint8)
    a8 = np.round(np.clip(alpha, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb8, a8]), "RGBA")


def add_rim_glow(img, rgb):
    """Soft glow in the team colour behind the ball's edge."""
    a = np.asarray(img, dtype=np.float32) / 255.0
    color, alpha = a[..., :3], a[..., 3]
    glow = GLOW_STRENGTH * np.clip(blur(alpha, GLOW_SIZE * img.size[0]), 0.0, 1.0)
    out_a = alpha + glow * (1.0 - alpha)
    glow_rgb = np.asarray(rgb, dtype=np.float32) / 255.0
    out_rgb = (color * alpha[..., None] + glow_rgb * (glow * (1.0 - alpha))[..., None]) / np.maximum(out_a, 1e-6)[..., None]
    return Image.fromarray(np.round(np.dstack([out_rgb, out_a]) * 255).astype(np.uint8), "RGBA")


def read_balls(path: Path):
    teams = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = {f.strip().lower(): f for f in (reader.fieldnames or [])}
        missing = [c for c in ("city", "abbr", "primary_hex", "secondary_hex") if c not in fields]
        if missing:
            raise SystemExit(f"{path}: missing column(s): {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):
            get = lambda key: (row.get(fields.get(key, ""), "") or "").strip()
            if not any((v or "").strip() for v in row.values()):
                continue
            city = get("city")
            if not city:
                raise SystemExit(f"{path}:{line}: empty city")
            try:
                primary, secondary = parse_hex(get("primary_hex")), parse_hex(get("secondary_hex"))
                seam = parse_hex(get("seam_hex")) if get("seam_hex") else darker(primary, SEAM_DARKEN)
                team = {"city": city, "text": get("abbr"), "primary": primary, "secondary": secondary,
                        "seam": seam,
                        "text_color": parse_hex(get("text_hex")) if get("text_hex") else secondary,
                        "outline": parse_hex(get("outline_hex")) if get("outline_hex") else seam}
            except ValueError as exc:
                raise SystemExit(f"{path}:{line}: {exc}")
            teams.append(team)
    if not teams:
        raise SystemExit(f"{path}: no teams found")
    return teams


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", type=Path, default=HERE / "master_ball.png")
    ap.add_argument("--teams", type=Path, default=HERE / "balls.csv")
    ap.add_argument("--out", type=Path, default=HERE / "output" / "balls")
    ap.add_argument("--font", type=Path, default=HERE / "fonts" / "Fredoka-Bold.ttf",
                    help="TTF/OTF for the abbreviation (default: Fredoka Bold, chunky rounded)")
    ap.add_argument("--size", type=int, default=1024, help="square canvas size in px (default 1024)")
    ap.add_argument("--margin", type=float, default=0.07, help="empty border (room for the glow), fraction of the canvas")
    ap.add_argument("--no-preview", action="store_true", help="skip preview.png")
    args = ap.parse_args(argv)

    teams = read_balls(args.teams)
    print(f"analysing {args.master} ...")
    maps, info = analyse_master(args.master, id_hues=ID_HUES, part_names=PART_NAMES)
    print("  ID hues (deg): " + ", ".join(f"{p}={h:.0f}" for p, h in zip(PART_NAMES, info["hues"])))
    alpha = ball_matte(maps)
    placer = BallText(maps, alpha, args.font)
    print(f"  ball centre ({placer.cx:.0f}, {placer.cy:.0f}) radius {placer.r:.0f}, "
          f"horizontal seam y={placer.seam_y:.0f}, vertical seam x={placer.seam_x:.0f}")

    args.out.mkdir(parents=True, exist_ok=True)
    entries, seen = [], {}
    for team in teams:
        slug = slugify(team["city"])
        if slug in seen:
            print(f"  warning: {team['city']!r} overwrites {seen[slug]!r} ({slug}.png)", file=sys.stderr)
        seen[slug] = team["city"]
        text = placer.place(team["text"]) if team["text"] else None
        img = render_ball(maps, alpha, (team["primary"], team["seam"], team["text_color"], team["outline"]), text)
        img = add_rim_glow(fit_to_canvas(img, args.size, args.margin), team["primary"])
        img.save(args.out / f"{slug}.png")

        swatches = [("ball", team["primary"]), ("seam", team["seam"])]
        if team["text"]:
            swatches += [("text", team["text_color"]), ("outline", team["outline"])]
        entries.append((team["city"], [img], swatches))
        note = (f", text {team['text']!r} {to_hex(team['text_color'])} outline {to_hex(team['outline'])}"
                if team["text"] else ", no text")
        print(f"  {team['city']}: ball {to_hex(team['primary'])}, seam {to_hex(team['seam'])}{note} -> {slug}.png")

    if not args.no_preview:
        contact_sheet(entries, args.out / "preview.png", HERE / "fonts" / "Oswald-Bold.ttf", thumb=380)
        print(f"wrote {args.out / 'preview.png'}")


if __name__ == "__main__":
    main()

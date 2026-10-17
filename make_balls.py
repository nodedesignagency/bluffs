#!/usr/bin/env python3
"""Generate team-coloured basketballs from one ID-coloured master render.

The master image is a basketball on a plain or softly graded light background,
painted with ID colours:

    red panels = primary      green panels = secondary      blue seams = seams

For every row of the balls CSV (city, abbr, primary_hex, secondary_hex, plus
optional seam_hex, text_hex and outline_hex) this writes

    <out>/<City>.png      transparent PNG, same canvas, size and position for all
    <out>/preview.png     contact sheet with every team

The pebbled texture and shading of the master are kept. The abbreviation is
wrapped onto the largest primary panel facing the camera, sized to stay clear
of the seams; leave abbr blank for no text. Shares the recolouring and
lettering code with make_helmets.py.

    python make_balls.py
    python make_balls.py --teams balls.csv --out output/balls --size 1024
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

from make_helmets import (
    HERE, TextLayout, analyse_master, components_by_run, contact_sheet, dilate, fit_to_canvas,
    grow_distance, paint_runs, parse_hex, pick_text_color, place_lettering, render_helmet,
    render_lettering, save_debug, slugify, to_hex,
)

DEFAULT_SEAM = (26, 26, 26)
TEXT_TILT_DEG = 0.0          # text direction; 0 = level with the image
TEXT_MARGIN = 0.03           # clearance from seams and the rim, fraction of ball radius
TEXT_START = 1.15            # first try: cap height = this x panel's inscribed radius


def fit_circle(alpha):
    """Least-squares circle through the ball's silhouette edge: (cx, cy, r)."""
    solid = alpha > 0.5
    edge = solid & dilate(~solid, 1)
    ys, xs = np.nonzero(edge)
    A = np.c_[2 * xs, 2 * ys, np.ones(len(xs))]
    cx, cy, c = np.linalg.lstsq(A, xs ** 2 + ys ** 2, rcond=None)[0]
    return float(cx), float(cy), float(math.sqrt(c + cx * cx + cy * cy))


def front_panel(maps):
    """The largest visible primary panel, as a bool mask."""
    panel = (maps.alpha > 0.5) & (maps.weights[..., 0] > 0.5)
    rows, starts, ends, labels = components_by_run(panel)
    if len(rows) == 0:
        raise SystemExit("master has no primary (red) panels")
    _, comp = np.unique(labels, return_inverse=True)
    biggest = np.argmax(np.bincount(comp, weights=ends - starts))
    keep = comp == biggest
    return paint_runs(panel.shape, rows[keep], starts[keep], ends[keep])


class BallText:
    """Where and how big the abbreviation can go on this master."""

    def __init__(self, maps, font, tilt_deg=TEXT_TILT_DEG):
        self.maps, self.font = maps, font
        cx, cy, r = fit_circle(maps.alpha)
        panel = front_panel(maps)
        # Distance from each panel pixel to the nearest seam or rim; the
        # deepest point is the middle of the panel.
        self.depth = grow_distance(~panel, panel)
        self.depth[~panel] = 0
        y, x = np.unravel_index(np.argmax(self.depth), self.depth.shape)
        self.inner = float(self.depth[y, x])
        self.margin = TEXT_MARGIN * r
        self.base = TextLayout(center=(float(x), float(y)), max_width=1e9, cap_height=self.inner,
                               tilt_deg=tilt_deg, sphere_center=(cx, cy), sphere_radius=r)

    def place(self, text, outline, ss=4, shrink=0.93, tolerance=0.003):
        """Coverage maps (fill, letters+outline or None), shrunk until the text
        sits inside the panel with a margin."""
        safe = self.depth >= self.margin
        cap = TEXT_START * self.inner
        for _ in range(40):
            layout = replace(self.base, cap_height=cap)
            fill_mask, outer_mask, mcx, mcy = render_lettering(text, self.font, layout, ss=ss, outline=outline)
            fill = place_lettering(fill_mask, mcx, mcy, ss, layout, self.maps.alpha.shape, style="3d")
            outer = (place_lettering(outer_mask, mcx, mcy, ss, layout, self.maps.alpha.shape, style="3d")
                     if outer_mask is not None else None)
            ink = outer if outer is not None else fill
            total = ink.sum()
            if total > 0 and (ink * ~safe).sum() / total <= tolerance:
                return fill, outer
            cap *= shrink
        return fill, outer


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
                team = {"city": city,
                        "primary": parse_hex(get("primary_hex")),
                        "secondary": parse_hex(get("secondary_hex")),
                        "seam": parse_hex(get("seam_hex")) if get("seam_hex") else DEFAULT_SEAM,
                        "text": get("abbr")}
                team["text_color"] = team["outline"] = None
                if team["text"]:
                    team["text_color"] = (parse_hex(get("text_hex")) if get("text_hex")
                                          else pick_text_color(team["primary"], team["secondary"], team["seam"]))
                    if get("outline_hex"):
                        team["outline"] = parse_hex(get("outline_hex"))
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
    ap.add_argument("--font", type=Path, default=HERE / "fonts" / "Anton-Regular.ttf",
                    help="TTF/OTF for the abbreviation (default: Anton, heavy condensed)")
    ap.add_argument("--size", type=int, default=1024, help="square canvas size in px (default 1024)")
    ap.add_argument("--margin", type=float, default=0.04, help="empty border, fraction of the canvas")
    ap.add_argument("--no-preview", action="store_true", help="skip preview.png")
    ap.add_argument("--debug", action="store_true", help="also write debug_masks.png")
    args = ap.parse_args(argv)

    teams = read_balls(args.teams)
    print(f"analysing {args.master} ...")
    maps, info = analyse_master(args.master, smooth_shading=False)   # keep the pebble texture
    print("  ID hues (deg): " + ", ".join(f"{p}={h:.0f}" for p, h in zip(("primary", "secondary", "seam"), info["hues"])))
    text = BallText(maps, args.font)

    args.out.mkdir(parents=True, exist_ok=True)
    if args.debug:
        save_debug(maps, args.out / "debug_masks.png")

    entries, seen = [], {}
    for team in teams:
        slug = slugify(team["city"])
        if slug in seen:
            print(f"  warning: {team['city']!r} overwrites {seen[slug]!r} ({slug}.png)", file=sys.stderr)
        seen[slug] = team["city"]
        fill = outer = None
        if team["text"]:
            fill, outer = text.place(team["text"], outline=team["outline"] is not None)
        colors = (team["primary"], team["secondary"], team["seam"])
        img = render_helmet(maps, colors, team["text_color"], fill, team["outline"], outer)
        img = fit_to_canvas(img, args.size, args.margin)
        img.save(args.out / f"{slug}.png")

        swatches = [("primary", team["primary"]), ("secondary", team["secondary"]), ("seam", team["seam"])]
        if team["text"]:
            swatches.append(("text", team["text_color"]))
        if team["outline"]:
            swatches.append(("outline", team["outline"]))
        entries.append((team["city"], [img], swatches))
        note = f", text {team['text']!r} {to_hex(team['text_color'])}" if team["text"] else ", no text"
        if team["outline"]:
            note += f" outline {to_hex(team['outline'])}"
        print(f"  {team['city']}: primary {to_hex(team['primary'])}, secondary {to_hex(team['secondary'])}, "
              f"seam {to_hex(team['seam'])}{note} -> {slug}.png")

    if not args.no_preview:
        contact_sheet(entries, args.out / "preview.png", HERE / "fonts" / "Oswald-Bold.ttf", thumb=380)
        print(f"wrote {args.out / 'preview.png'}")


if __name__ == "__main__":
    main()

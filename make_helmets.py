#!/usr/bin/env python3
"""Generate team-coloured football helmets from one ID-coloured master render.

The master image is a side-profile helmet facing right on a plain light
background, painted with ID colours:

    shell = red        top stripe = green        facemask = blue

For every row of the teams CSV (city, shell_hex, stripe_hex, facemask_hex,
plus optional text, text_hex and outline_hex) this writes

    <out>/<City>_right.png    helmet facing right
    <out>/<City>_left.png     helmet facing left (lettering placed after the flip)
    <out>/preview.png         contact sheet with every team

Every PNG has a transparent background and shares one canvas size, position and
scale, so they line up in an app.  Text goes on the shell only when the row's
`text` column has something in it; it is drawn exactly as written.  Only
Pillow and numpy are required.

    python make_helmets.py
    python make_helmets.py --teams teams.csv --out output --size 1024
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
PARTS = ("shell", "stripe", "facemask")

# Nominal ID hues (degrees). The real hues are measured from the master, these
# are only the starting points for that search.
ID_HUES = (0.0, 120.0, 240.0)

# Where the helmet text goes on the right-facing master, in master pixels. Tuned
# on a 1536 px wide master and scaled automatically for other sizes.
REFERENCE_WIDTH = 1536
TEXT_GLOSS_CUT = 0.6   # how much of the shell's glossy highlight is removed on the text


@dataclass(frozen=True)
class TextLayout:
    center: tuple[float, float] = (575.0, 450.0)  # middle of the text, upper side of the shell
    max_width: float = 350.0     # text never gets wider than this
    cap_height: float = 210.0    # letter height; longer text shrinks to fit
    arc_radius: float = 1500.0   # radius of the arc the baseline follows
    tilt_deg: float = 4.0        # > 0 rises toward the facemask
    tracking: float = 0.04       # extra letter spacing, fraction of cap height
    min_squeeze: float = 0.82    # condense this far before shrinking the size
    outline: float = 0.05        # outline thickness (if used), fraction of cap height
    # "3d" text style: the shell is modelled as a sphere fitted to its outline
    # and the text is wrapped onto it like a decal.
    sphere_center: tuple[float, float] = (671.0, 663.0)
    sphere_radius: float = 463.0

    def scaled(self, k: float) -> TextLayout:
        return replace(
            self,
            center=(self.center[0] * k, self.center[1] * k),
            max_width=self.max_width * k,
            cap_height=self.cap_height * k,
            arc_radius=self.arc_radius * k,
            sphere_center=(self.sphere_center[0] * k, self.sphere_center[1] * k),
            sphere_radius=self.sphere_radius * k,
        )

    def shifted(self, dx: float, dy: float) -> TextLayout:
        return replace(self, center=(self.center[0] + dx, self.center[1] + dy),
                       sphere_center=(self.sphere_center[0] + dx, self.sphere_center[1] + dy))

    def mirrored(self, width: float) -> TextLayout:
        """Same spot on the flipped helmet. The geometry is mirrored, the
        letters are not: they are drawn fresh, reading left to right."""
        return replace(self, center=(width - self.center[0], self.center[1]), tilt_deg=-self.tilt_deg,
                       sphere_center=(width - self.sphere_center[0], self.sphere_center[1]))


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------

def parse_hex(value: str) -> tuple[int, int, int]:
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise ValueError(f"not a hex colour: {value!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def parse_percent(value: str, name: str) -> float:
    """'67', '67%' -> 0.67; blank -> 1.0. Must be 1..100."""
    s = value.strip().rstrip("%").strip()
    if not s:
        return 1.0
    try:
        pct = float(s)
    except ValueError:
        raise ValueError(f"{name} must be a percentage like 67 or 67%, got {value!r}")
    if not 0 < pct <= 100:
        raise ValueError(f"{name} must be between 1 and 100, got {value!r}")
    return pct / 100.0


def to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055).astype(np.float32)


def relative_luminance(rgb) -> float:
    lin = srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    return float(lin @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32))


def contrast_ratio(a, b) -> float:
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def pick_text_color(shell, stripe, facemask):
    """A team colour if one stands out against the shell, else white or black."""
    best = max((stripe, facemask), key=lambda c: contrast_ratio(c, shell))
    if contrast_ratio(best, shell) >= 3.0:
        return best
    white, black = (255, 255, 255), (16, 16, 16)
    return white if contrast_ratio(white, shell) >= contrast_ratio(black, shell) else black


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def hue_degrees(rgb):
    """HSV hue in degrees for an (..., 3) array in 0..1."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = rgb.max(-1), rgb.min(-1)
    c = np.maximum(mx - mn, 1e-6)
    h = np.where(mx == r, (g - b) / c, np.where(mx == g, 2.0 + (b - r) / c, 4.0 + (r - g) / c))
    return (h * 60.0) % 360.0


def hue_distance(h, center):
    d = np.abs(h - center) % 360.0
    return np.minimum(d, 360.0 - d)


# --------------------------------------------------------------------------
# Small image-processing helpers (no SciPy / OpenCV needed)
# --------------------------------------------------------------------------

def blur(a, sigma):
    """Separable Gaussian blur over the first two axes."""
    out = np.asarray(a, dtype=np.float32)
    if sigma <= 0:
        return out
    r = max(1, math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k = (k / k.sum()).astype(np.float32)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        n = out.shape[axis]
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            idx = [slice(None)] * out.ndim
            idx[axis] = slice(i, i + n)
            acc += w * p[tuple(idx)]
        out = acc
    return out


def dilate(mask, radius):
    """Binary dilation with a (2r+1)-wide square."""
    out = mask
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius, radius)
        p = np.pad(out, pad)
        n = out.shape[axis]
        acc = np.zeros_like(out)
        for i in range(2 * radius + 1):
            acc |= p[i:i + n] if axis == 0 else p[:, i:i + n]
        out = acc
    return out


def components_by_run(mask):
    """4-connected components of a bool mask via run-length union-find.

    Returns per-run arrays (row, start, end) and a component id per run.
    """
    H, W = mask.shape
    padded = np.zeros((H, W + 2), np.int8)
    padded[:, 1:-1] = mask
    edges = np.diff(padded, axis=1)
    rows, starts = np.nonzero(edges == 1)
    _, ends = np.nonzero(edges == -1)

    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    bounds = np.searchsorted(rows, np.arange(H + 1)).tolist()
    s, e = starts.tolist(), ends.tolist()
    for y in range(1, H):
        i, i_end = bounds[y - 1], bounds[y]
        j, j_end = bounds[y], bounds[y + 1]
        while i < i_end and j < j_end:
            if s[i] < e[j] and s[j] < e[i]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
            if e[i] < e[j]:
                i += 1
            else:
                j += 1
    labels = np.array([find(i) for i in range(len(rows))], dtype=np.int64)
    return rows, starts, ends, labels


def paint_runs(shape, rows, starts, ends):
    H, W = shape
    acc = np.zeros((H, W + 1), np.int32)
    np.add.at(acc, (rows, starts), 1)
    np.add.at(acc, (rows, ends), -1)
    return np.cumsum(acc, axis=1)[:, :W] > 0


def bilinear(img, rows, cols):
    """Sample a 2-D array at fractional pixel-centre coordinates (zero outside)."""
    H, W = img.shape
    p = np.pad(img, 1)
    r, c = rows - 0.5 + 1, cols - 0.5 + 1
    r0, c0 = np.floor(r), np.floor(c)
    fr, fc = r - r0, c - c0
    r0 = np.clip(r0.astype(np.int64), 0, H + 1)
    c0 = np.clip(c0.astype(np.int64), 0, W + 1)
    r1, c1 = np.minimum(r0 + 1, H + 1), np.minimum(c0 + 1, W + 1)
    top = p[r0, c0] * (1 - fc) + p[r0, c1] * fc
    bot = p[r1, c0] * (1 - fc) + p[r1, c1] * fc
    return top * (1 - fr) + bot * fr


# --------------------------------------------------------------------------
# Master analysis: background matte, part masks, shading
# --------------------------------------------------------------------------

def border_background(rgb, sigma=4):
    """Per-pixel background for a soft gradient backdrop, blended in from the
    four image edges (a Coons patch)."""
    H, W, _ = rgb.shape
    strip = lambda a: blur(a[:, None, :], sigma)[:, 0]
    top, bottom = strip(np.median(rgb[:4], axis=0)), strip(np.median(rgb[-4:], axis=0))
    left, right = strip(np.median(rgb[:, :4], axis=1)), strip(np.median(rgb[:, -4:], axis=1))
    u = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    v = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None, None]
    corners = ((1 - u) * (1 - v) * top[0] + u * (1 - v) * top[-1]
               + (1 - u) * v * bottom[0] + u * v * bottom[-1])
    bg = ((1 - u) * left[:, None] + u * right[:, None]
          + (1 - v) * top[None] + v * bottom[None] - corners)
    return bg.astype(np.float32)


def remove_background(rgb, bg_tol=10 / 255, min_hole_area=20, band=2):
    """Matte the subject off a plain or softly graded background.

    Returns (alpha, foreground colours with the background unmixed, bg colour).
    """
    H, W, _ = rgb.shape
    edge = np.concatenate([rgb[:4].reshape(-1, 3), rgb[-4:].reshape(-1, 3),
                           rgb[:, :4].reshape(-1, 3), rgb[:, -4:].reshape(-1, 3)])
    lo, hi = np.percentile(edge, [1, 99], axis=0)
    if (hi - lo).max() > bg_tol:
        bg_color = border_background(rgb)      # gradient backdrop
    else:
        bg_color = np.median(edge, axis=0).astype(np.float32)
    dist = np.abs(rgb - bg_color).max(-1)

    # Background-coloured regions count as background if they touch the image
    # border or are big enough to be a real see-through gap (between facemask
    # bars, through the ear hole). Small specks are highlights on the helmet.
    rows, starts, ends, labels = components_by_run(dist <= bg_tol)
    _, comp = np.unique(labels, return_inverse=True)
    size = np.bincount(comp, weights=ends - starts)
    touches = np.bincount(comp, weights=(rows == 0) | (rows == H - 1) | (starts == 0) | (ends == W)) > 0
    min_area = min_hole_area * (H * W) / REFERENCE_WIDTH ** 2
    keep = (touches | (size >= min_area))[comp]
    bg = paint_runs((H, W), rows[keep], starts[keep], ends[keep])

    # Pixels next to the background are a mix of helmet and background.
    # Estimate the pure helmet colour from nearby solid pixels, then read
    # coverage off the line between background and that colour.
    edge_band = dilate(bg, band) & ~bg
    solid = ~bg & ~edge_band
    fg_est = rgb.copy()
    pending = edge_band.copy()
    for sigma in (band, 4 * band):
        ys, xs = np.nonzero(pending)
        if len(ys) == 0:
            break
        r = math.ceil(3 * sigma) + 1
        win = (slice(max(ys.min() - r, 0), ys.max() + r + 1), slice(max(xs.min() - r, 0), xs.max() + r + 1))
        s = solid[win].astype(np.float32)
        w = blur(s, sigma)
        est = blur(rgb[win] * s[..., None], sigma) / np.maximum(w, 1e-6)[..., None]
        done = pending[win] & (w >= 1e-3)
        fg_est[win][done] = est[done]
        pending[win] &= ~done

    diff = fg_est - bg_color
    proj = ((rgb - bg_color) * diff).sum(-1) / np.maximum((diff * diff).sum(-1), 1e-4)
    alpha = np.where(solid, 1.0, np.where(bg, 0.0, np.clip(proj, 0.0, 1.0))).astype(np.float32)

    # Remove the background's contribution from partially covered pixels so no
    # light fringe survives the recolour.
    unmixed = (rgb - (1 - alpha[..., None]) * bg_color) / np.maximum(alpha, 1e-3)[..., None]
    trust = smoothstep(0.05, 0.5, alpha)[..., None]
    fg = np.clip(trust * unmixed + (1 - trust) * fg_est, 0.0, 1.0)
    fg = np.where(solid[..., None], rgb, fg)
    return alpha, fg.astype(np.float32), bg_color


def smooth_within_parts(values, weights, support, sigma):
    out = np.zeros_like(values)
    for k in range(weights.shape[-1]):
        w = weights[..., k] * support
        out += weights[..., k] * blur(values * w, sigma) / np.maximum(blur(w, sigma), 1e-6)
    # Pixels with no paint support (pads, screws) keep their own value.
    trust = smoothstep(0.0, 0.2, blur(support, sigma))
    return trust * out + (1 - trust) * values


def calibrate_hues(hue, chroma, alpha, id_hues=ID_HUES):
    """Find the actual ID hues in the master (renders drift off pure RGB)."""
    sel = (chroma > 0.25) & (alpha > 0.5)
    hist = np.bincount(np.round(hue[sel]).astype(int) % 360, minlength=360).astype(np.float32)
    hist = np.convolve(np.concatenate([hist[-5:], hist, hist[:5]]), np.ones(11) / 11, "valid")
    centers = []
    for nominal in id_hues:
        window = (np.arange(360) - nominal + 180) % 360 - 180
        cand = np.where(np.abs(window) <= 60, hist, -1)
        peak = int(np.argmax(cand))
        if cand[peak] <= 0:
            centers.append(nominal)
            continue
        near = sel & (hue_distance(hue, peak) <= 15)
        ang = np.deg2rad(hue[near])
        centers.append(float(np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) % 360))
    return centers


def grow_distance(seed, region, limit=2000):
    """Distance in px from `seed` to each pixel of `region`, walking only
    through `region` (alternating 4- and 8-neighbour steps, close to
    Euclidean). Unreached pixels stay inf."""
    dist = np.full(seed.shape, np.inf, np.float32)
    dist[seed] = 0.0
    reached = seed.copy()
    for step in range(1, limit + 1):
        if step % 2:
            grown = reached.copy()
            grown[1:] |= reached[:-1]
            grown[:-1] |= reached[1:]
            grown[:, 1:] |= reached[:, :-1]
            grown[:, :-1] |= reached[:, 1:]
        else:
            grown = dilate(reached, 1)
        new = grown & region & ~reached
        if not new.any():
            break
        dist[new] = step
        reached |= new
    return dist


def stripe_distances(alpha, weights, scale):
    """How far each stripe pixel is from the stripe's shell-side edge and
    from its outer edge (silhouette / facemask), for narrowing the stripe."""
    solid = alpha > 0.5
    stripe = solid & (weights[..., 1] > 0.5)
    shell = solid & (weights[..., 0] > 0.5)
    # Thin shell-coloured slots cut into the stripe are not its edge.
    r = max(1, round(12 * scale))
    body = dilate(~dilate(~shell, r), r) & shell
    region = stripe | (shell & ~body)
    near = grow_distance(body, region)
    far = grow_distance(~region & ~body, region)
    off = ~stripe
    near[off] = np.inf
    far[off] = np.inf
    return near, far


@dataclass
class HelmetMaps:
    """Everything the recolour needs, in the cropped master's pixel grid."""
    alpha: np.ndarray     # (H, W) coverage
    weights: np.ndarray   # (H, W, 3) share of shell / stripe / facemask paint
    paint: np.ndarray     # (H, W) 1 = team paint, 0 = neutral (pads, screws)
    shade: np.ndarray     # (H, W) diffuse light relative to each part's base tone
    gloss: np.ndarray     # (H, W) additive white from glossy highlights (linear)
    neutral: np.ndarray   # (H, W) grey level for neutral pixels (linear)
    stripe_near: np.ndarray | None  # (H, W) px from the stripe's shell-side edge (helmets only)
    stripe_far: np.ndarray | None   # (H, W) px from the stripe's outer edge (helmets only)

    def flipped(self) -> HelmetMaps:
        return HelmetMaps(*(None if a is None else np.ascontiguousarray(a[:, ::-1]) for a in
                            (self.alpha, self.weights, self.paint, self.shade, self.gloss, self.neutral,
                             self.stripe_near, self.stripe_far)))

    def part_weights(self, stripe_width=1.0):
        """Part weights with the stripe narrowed to `stripe_width` (0..1) of
        its width. The stripe gives up its shell-side edge to the shell, so
        it keeps its shape and its outer edge."""
        if stripe_width >= 1.0:
            return self.weights
        near, far = self.stripe_near, self.stripe_far
        with np.errstate(invalid="ignore"):
            edge = (near - 0.5) - (1.0 - stripe_width) * ((near - 0.5) + (far - 0.5))
        keep = np.where(np.isfinite(edge), smoothstep(-1.0, 1.0, np.nan_to_num(edge, nan=1.0)), 1.0)
        w = self.weights.copy()
        moved = w[..., 1] * (1.0 - keep)
        w[..., 1] -= moved
        w[..., 0] += moved
        return w

    @property
    def size(self):
        return self.alpha.shape[1], self.alpha.shape[0]


def analyse_master(path, pad=6, smooth_shading=True, id_hues=ID_HUES, part_names=PARTS):
    """Matte the master and split it into parts by ID hue (one per entry of
    id_hues), with shading and gloss per pixel."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        # Already cut out: composite on white so the same matting path applies.
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (248, 248, 248, 255))
        img = Image.alpha_composite(base, rgba)
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    scale = rgb.shape[1] / REFERENCE_WIDTH

    alpha, fg, _ = remove_background(rgb)

    ys, xs = np.nonzero(alpha > 0.02)
    if len(xs) == 0:
        raise SystemExit(f"{path}: could not find a helmet on the background")
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + 1 + pad, rgb.shape[1])
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + 1 + pad, rgb.shape[0])
    alpha, fg = alpha[y0:y1, x0:x1], fg[y0:y1, x0:x1]

    # --- which part is each pixel? (hue, softened spatially) -----------------
    hue = hue_degrees(fg)
    mx, mn = fg.max(-1), fg.min(-1)
    chroma = mx - mn
    sat = chroma / np.maximum(mx, 1e-4)
    centers = calibrate_hues(hue, chroma, alpha, id_hues)
    score = np.stack([np.exp(-(hue_distance(hue, c) / 20.0) ** 2) for c in centers], -1)
    weights = score / np.maximum(score.sum(-1, keepdims=True), 1e-12)
    weights = blur(weights, 0.9 * scale)
    weights /= np.maximum(weights.sum(-1, keepdims=True), 1e-6)
    if len(id_hues) == 1:
        weights = np.ones_like(weights)                  # single-colour master: it is all one part

    # Team paint vs neutral parts (dark padding, metal screws). Colourful or
    # saturated-and-not-black pixels are paint.
    paint = np.maximum(smoothstep(0.05, 0.16, chroma),
                       smoothstep(0.18, 0.40, sat) * smoothstep(0.03, 0.10, mx))
    paint = blur(paint, 0.7 * scale)

    # --- shading: pixel ~ shade * base_colour + gloss * white ----------------
    lin = srgb_to_linear(fg)
    core = alpha > 0.99
    base = []
    for k in range(len(id_hues)):
        sel = core & (weights[..., k] > 0.95) & (paint > 0.95)
        if sel.sum() < 50:
            raise SystemExit(f"{path}: no {part_names[k]} pixels found (expected hue near {id_hues[k]:.0f} deg)")
        base.append(np.median(lin[sel], axis=0))
    base = np.array(base, dtype=np.float32)              # (parts, rgb)

    albedo = weights @ base                              # expected ID colour per pixel
    aa = (albedo * albedo).sum(-1)
    a1 = albedo.sum(-1)
    ac = (albedo * lin).sum(-1)
    c1 = lin.sum(-1)
    det = np.maximum(3 * aa - a1 * a1, 1e-8)
    shade = (3 * ac - a1 * c1) / det
    gloss = (aa * c1 - a1 * ac) / det
    no_gloss = gloss < 0                                 # least squares with gloss >= 0
    shade = np.where(no_gloss, ac / np.maximum(aa, 1e-8), shade)
    gloss = np.where(no_gloss, 0.0, gloss)
    no_shade = shade < 0
    gloss = np.where(no_shade, c1 / 3, gloss)
    shade = np.maximum(shade, 0.0)

    # The render's surface grain (and any compression noise) lives in the
    # shading. Smooth it lightly, never across a part boundary, so light
    # team colours don't come out blotchy.
    # Turn this off for surfaces whose texture should survive (pebbled balls).
    if smooth_shading:
        support = alpha * paint
        shade = smooth_within_parts(shade, weights, support, 1.0 * scale)
        gloss = smooth_within_parts(gloss, weights, support, 0.5 * scale)

    neutral = lin @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    stripe_near = stripe_far = None                      # only helmets have a stripe to narrow
    if tuple(part_names) == PARTS:
        stripe_near, stripe_far = stripe_distances(alpha, weights, scale)

    maps = HelmetMaps(alpha, weights.astype(np.float32), paint.astype(np.float32),
                      shade.astype(np.float32), gloss.astype(np.float32), neutral.astype(np.float32),
                      stripe_near, stripe_far)
    info = {"crop": (x0, y0, x1, y1), "scale": scale, "hues": centers,
            "base": [to_hex(np.round(linear_to_srgb(b) * 255).astype(int)) for b in base]}
    return maps, info


# --------------------------------------------------------------------------
# Lettering
# --------------------------------------------------------------------------

def load_font(font_path, size):
    try:
        return ImageFont.truetype(str(font_path), size)
    except OSError as exc:
        raise SystemExit(f"could not load font {font_path}: {exc}")


def render_lettering(text, font_path, layout: TextLayout, ss=4, outline=False):
    """Flat text masks at `ss` x master scale, fitted to the layout.

    Returns (fill mask, outline mask or None, centre column, centre row); the
    outline mask covers the letters plus their outline.
    """
    probe = load_font(font_path, 200)
    cap_ratio = -probe.getbbox("H", anchor="ls")[1] / 200.0
    stroke = layout.outline if outline else 0.0

    def natural_width(cap):
        f = load_font(font_path, max(1, round(cap / cap_ratio)))
        l, _, r, _ = f.getbbox(text, anchor="ls")
        return (r - l) + layout.tracking * cap * (len(text) - 1) + 2 * stroke * cap

    cap = layout.cap_height
    width = natural_width(cap)
    squeeze = 1.0
    if width > layout.max_width:
        squeeze = max(layout.min_squeeze, layout.max_width / width)
        if width * squeeze > layout.max_width:
            cap *= layout.max_width / (width * squeeze)

    font = load_font(font_path, max(1, round(cap * ss / cap_ratio)))
    cap_ss = cap_ratio * font.size
    track = layout.tracking * cap * ss
    stroke_px = max(1, round(stroke * cap_ss)) if outline else 0
    pad = int(cap_ss * 0.6) + stroke_px + 4
    advance = [font.getlength(text[:i]) + i * track for i in range(len(text) + 1)]
    W = int(math.ceil(advance[-1])) + 2 * pad
    H = int(math.ceil(cap_ss)) + 2 * pad
    baseline = pad + cap_ss

    def draw_mask(stroke_width):
        canvas = Image.new("L", (W, H), 0)
        draw = ImageDraw.Draw(canvas)
        for i, ch in enumerate(text):
            draw.text((pad + advance[i], baseline), ch, font=font, fill=255, anchor="ls",
                      stroke_width=stroke_width, stroke_fill=255)
        return canvas

    canvases = [draw_mask(0)] + ([draw_mask(stroke_px)] if outline else [])
    ink = canvases[-1].getbbox()
    if ink is None:
        raise SystemExit(f"font {font_path} rendered nothing for {text!r}")
    masks = []
    for canvas in canvases:
        if squeeze != 1.0:
            canvas = canvas.resize((max(1, round(W * squeeze)), H), Image.LANCZOS)
        masks.append(np.asarray(canvas, dtype=np.float32) / 255.0)
    cx = (ink[0] + ink[2]) / 2 * squeeze
    cy = baseline - cap_ss / 2
    return masks[0], (masks[1] if outline else None), cx, cy


TEXT_STYLES = ("flat", "3d")
# The sphere wrap already lifts the text toward the front and foreshortens it,
# so the 3d style drops the extra tilt and uses slightly taller letters.
STYLE_OVERRIDES = {"flat": {}, "3d": {"tilt_deg": 0.0, "cap_height": 220.0}}


def sphere_frame(layout: TextLayout):
    """Unit vectors at the text centre on the shell sphere: surface normal,
    text direction (tilted) and text-down direction."""
    (cx, cy), r = layout.sphere_center, layout.sphere_radius
    ax, ay = (layout.center[0] - cx) / r, (layout.center[1] - cy) / r
    normal = np.array([ax, ay, math.sqrt(max(1.0 - ax * ax - ay * ay, 1e-6))])
    t = math.radians(layout.tilt_deg)
    along = np.array([math.cos(t), -math.sin(t), 0.0])
    along -= (along @ normal) * normal
    along /= np.linalg.norm(along)
    return normal, along, np.cross(normal, along)


def place_lettering(mask, mcx, mcy, ss, layout: TextLayout, shape, q=3, style="flat"):
    """Warp the flat text masks onto the shell at layout.center.

    flat: a gentle arc (bulging upward like the shell) plus tilt.
    3d:   wrapped onto a sphere fitted to the shell, so the letters curve and
          foreshorten as the shell turns away, like a real decal.
    Returns a coverage map of `shape`.
    """
    H, W = shape
    out = np.zeros(shape, np.float32)
    ink_rows = np.nonzero(mask.max(1) > 0)[0]
    ink_cols = np.nonzero(mask.max(0) > 0)[0]
    if len(ink_rows) == 0:
        return out
    # Neither warp moves a point further from the centre than it is on the
    # flat mask, so the ink extent bounds the area to compute.
    reach = max(math.hypot(x - mcx, y - mcy)
                for x in (ink_cols[0], ink_cols[-1] + 1) for y in (ink_rows[0], ink_rows[-1] + 1)) / ss + 3
    cx, cy = layout.center
    x0, x1 = max(int(cx - reach), 0), min(int(cx + reach) + 1, W)
    y0, y1 = max(int(cy - reach), 0), min(int(cy + reach) + 1, H)
    if x0 >= x1 or y0 >= y1:
        return out

    # q x q sub-samples per pixel, all sampled in one pass.
    sub = (np.arange(q, dtype=np.float32) + 0.5) / q
    oy, ox = (a.reshape(-1, 1, 1) for a in np.meshgrid(sub, sub, indexing="ij"))
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    px, py = xs[None] + ox, ys[None] + oy
    if style == "3d":
        # Point on the sphere under each sample, then its geodesic offset
        # from the text centre (azimuthal equidistant), in px.
        normal, e_along, e_down = sphere_frame(layout)
        (scx, scy), sr = layout.sphere_center, layout.sphere_radius
        X, Y = (px - scx) / sr, (py - scy) / sr
        Z = np.sqrt(np.clip(1.0 - X * X - Y * Y, 0.0, None))
        cos_d = np.clip(X * normal[0] + Y * normal[1] + Z * normal[2], -1.0, 1.0)
        qx, qy, qz = X - cos_d * normal[0], Y - cos_d * normal[1], Z - cos_d * normal[2]
        qn = np.sqrt(qx * qx + qy * qy + qz * qz)
        k = np.where(qn > 1e-7, np.arccos(cos_d) / np.maximum(qn, 1e-7), 1.0) * sr
        along = (qx * e_along[0] + qy * e_along[1] + qz * e_along[2]) * k
        down = (qx * e_down[0] + qy * e_down[1] + qz * e_down[2]) * k
        samples = bilinear(mask, mcy + down * ss, mcx + along * ss)
        samples *= (X * X + Y * Y < 1.0) & (cos_d > 0.0)
    else:
        t = math.radians(layout.tilt_deg)
        ct, st = math.cos(t), math.sin(t)
        R = layout.arc_radius
        u = (px - cx) * ct - (py - cy) * st       # along the lettering
        v = (px - cx) * st + (py - cy) * ct       # down, across the lettering
        up = R - v                                # arc centre sits R below the text
        along = R * np.arctan2(u, up)
        height = np.hypot(u, up) - R
        samples = bilinear(mask, mcy - height * ss, mcx + along * ss)
    out[y0:y1, x0:x1] = np.clip(samples.mean(0), 0.0, 1.0)
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def shoulder(rgb, knee=0.8):
    """Soft roll-off above `knee` (linear light) so bright team colours keep
    their shading instead of flattening to pure white. Hue-preserving."""
    m = rgb.max(-1, keepdims=True)
    rolled = knee + (1.0 - knee) * np.tanh((m - knee) / (1.0 - knee))
    return rgb * np.where(m > knee, rolled / np.maximum(m, 1e-6), 1.0)


def compress_highlights(rgb):
    """Bring overexposed highlights back into range by fading them to white
    instead of clipping each channel (which would shift the hue)."""
    m = rgb.max(-1, keepdims=True)
    over = np.maximum(m, 1.0)
    scaled = rgb / over
    return scaled + (1.0 - scaled) * (1.0 - 1.0 / over)


def render_helmet(maps: HelmetMaps, colors, text_color=None, fill=None, outline_color=None, outer=None,
                  stripe_width=1.0):
    """Recolour the helmet. `fill` / `outer` are text coverage maps (outer =
    letters plus outline); the text is painted on the shell only."""
    lin = lambda rgb: srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    team = lin(colors)                                                        # (3, 3)
    weights = maps.part_weights(stripe_width)
    albedo = weights @ team
    if fill is not None:
        shell = team[0]
        decal = shell + np.zeros_like(albedo)
        if outer is not None:
            decal += outer[..., None] * (lin(outline_color) - decal)
        decal += fill[..., None] * (lin(text_color) - decal)
        albedo = albedo + weights[..., :1] * (decal - shell)
    gloss = maps.gloss
    if fill is not None:
        # Keep some shine on the text but let it read as solid paint.
        cover = (outer if outer is not None else fill) * weights[..., 0]
        gloss = gloss * (1.0 - TEXT_GLOSS_CUT * cover)
    lit = compress_highlights(shoulder(maps.shade[..., None] * albedo) + gloss[..., None])
    paint = maps.paint[..., None]
    out = paint * lit + (1.0 - paint) * maps.neutral[..., None]
    rgb8 = np.round(linear_to_srgb(out) * 255).astype(np.uint8)
    a8 = np.round(np.clip(maps.alpha, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb8, a8]), "RGBA")


def fit_to_canvas(img: Image.Image, size: int, margin: float):
    """Scale the helmet crop into a square canvas, centred. Every output uses
    the same crop size, so position and scale are identical across files."""
    w, h = img.size
    f = size * (1 - 2 * margin) / max(w, h)
    nw, nh = max(1, round(w * f)), max(1, round(h * f))
    small = img.convert("RGBa").resize((nw, nh), Image.LANCZOS).convert("RGBA")
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(small, ((size - nw) // 2, (size - nh) // 2))
    return canvas


def slugify(city: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", city).strip("_") or "team"


# --------------------------------------------------------------------------
# Contact sheet
# --------------------------------------------------------------------------

def contact_sheet(entries, path, font_path, thumb=300, background=(24, 27, 34), checker_tiles=True):
    """Grid of (title, [images], [(label, rgb), ...]) entries on a dark sheet.
    checker_tiles puts a checkerboard behind each image to show transparency."""
    n = len(entries)
    per_cell = max(len(images) for _, images, _ in entries)
    cols = max(1, math.ceil(math.sqrt(n / per_cell)))
    rows = math.ceil(n / cols)
    gap = 16
    cell_w = per_cell * thumb + (per_cell - 1) * gap
    title_font = load_font(font_path, 26)
    small_font = load_font(font_path, 15)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def swatch_layout(swatches):
        """(x offset, row) for each swatch, wrapping at the cell width."""
        out, sx, row = [], 0, 0
        for label, _ in swatches:
            need = 25 + int(measure.textlength(f"{label} #000000", font=small_font)) + 10
            if sx > 0 and sx + need > cell_w:
                sx, row = 0, row + 1
            out.append((sx, row))
            hx = sx + 25 + int(measure.textlength(label, font=small_font)) + 6
            sx = hx + int(measure.textlength("#000000", font=small_font)) + 22
        return out

    swatch_rows = max((swatch_layout(sw)[-1][1] + 1 if sw else 0) for _, _, sw in entries)
    label_h = 42 + 26 * max(swatch_rows, 2) + 10
    cell_h = thumb + label_h
    W = cols * cell_w + (cols + 1) * gap
    H = rows * cell_h + (rows + 1) * gap
    sheet = Image.new("RGB", (W, H), tuple(background))
    draw = ImageDraw.Draw(sheet)

    # Checkerboard behind each image so transparency and fringes show up.
    tile = 12
    yy, xx = np.mgrid[0:thumb, 0:thumb]
    check = np.where(((yy // tile + xx // tile) % 2)[..., None] == 0, (58, 62, 72), (72, 77, 89)).astype(np.uint8)
    checker = Image.fromarray(check, "RGB")

    for i, (title, images, swatches) in enumerate(entries):
        cx = gap + (i % cols) * (cell_w + gap)
        cy = gap + (i // cols) * (cell_h + gap)
        for j, img in enumerate(images):
            x = cx + j * (thumb + gap)
            if checker_tiles:
                sheet.paste(checker, (x, cy))
            t = img.convert("RGBa").resize((thumb, thumb), Image.LANCZOS).convert("RGBA")
            sheet.paste(t, (x, cy), t)
        ty = cy + thumb + 6
        draw.text((cx, ty), title, font=title_font, fill=(240, 240, 240))
        for (label, rgb), (dx, row) in zip(swatches, swatch_layout(swatches)):
            sx, sy = cx + dx, ty + 42 + 26 * row
            draw.rectangle((sx, sy, sx + 18, sy + 18), fill=tuple(rgb), outline=(140, 140, 150))
            draw.text((sx + 25, sy + 9), label, font=small_font, fill=(150, 155, 165), anchor="lm")
            hx = sx + 25 + int(draw.textlength(label, font=small_font)) + 6
            draw.text((hx, sy + 9), to_hex(rgb), font=small_font, fill=(225, 227, 232), anchor="lm")
    sheet.save(path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def read_teams(path: Path):
    teams = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = {f.strip().lower(): f for f in (reader.fieldnames or [])}
        missing = [c for c in ("city", "shell_hex", "stripe_hex", "facemask_hex") if c not in fields]
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
                        "shell": parse_hex(get("shell_hex")),
                        "stripe": parse_hex(get("stripe_hex")),
                        "facemask": parse_hex(get("facemask_hex"))}
                # Text only when the CSV asks for it, exactly as written.
                team["text"] = get("text")
                team["text_color"] = team["outline"] = None
                if team["text"]:
                    team["text_color"] = (parse_hex(get("text_hex")) if get("text_hex")
                                          else pick_text_color(team["shell"], team["stripe"], team["facemask"]))
                    if get("outline_hex"):
                        team["outline"] = parse_hex(get("outline_hex"))
                team["stripe_width"] = parse_percent(get("stripe_width"), "stripe_width")
                team["text_style"] = get("text_style").lower() or "flat"
                if team["text_style"] not in TEXT_STYLES:
                    raise ValueError(f"text_style must be one of {', '.join(TEXT_STYLES)}, "
                                     f"got {get('text_style')!r}")
            except ValueError as exc:
                raise SystemExit(f"{path}:{line}: {exc}")
            teams.append(team)
    if not teams:
        raise SystemExit(f"{path}: no teams found")
    return teams


def save_debug(maps: HelmetMaps, path: Path):
    """Side-by-side view of the masks: matte | part weights | paint gate | shade/gloss."""
    a = maps.alpha
    parts = np.dstack([maps.weights[..., 0], maps.weights[..., 1], maps.weights[..., 2]]) * maps.paint[..., None]
    paint = np.repeat(maps.paint[..., None], 3, -1)
    shading = np.dstack([np.clip(maps.shade / 2, 0, 1), np.clip(maps.gloss * 2, 0, 1), np.zeros_like(a)])
    panels = [np.repeat(a[..., None], 3, -1), parts * a[..., None], paint * a[..., None], shading * a[..., None]]
    Image.fromarray(np.round(np.concatenate(panels, 1) * 255).astype(np.uint8)).save(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--master", type=Path, default=HERE / "master_helmet.png")
    ap.add_argument("--teams", type=Path, default=HERE / "teams.csv")
    ap.add_argument("--out", type=Path, default=HERE / "output")
    ap.add_argument("--font", type=Path, default=HERE / "fonts" / "Anton-Regular.ttf",
                    help="TTF/OTF for the helmet text (default: Anton, heavy condensed)")
    ap.add_argument("--size", type=int, default=1024, help="square canvas size in px (default 1024)")
    ap.add_argument("--margin", type=float, default=0.04, help="empty border, fraction of the canvas")
    ap.add_argument("--no-preview", action="store_true", help="skip preview.png")
    ap.add_argument("--debug", action="store_true", help="also write debug_masks.png")
    args = ap.parse_args(argv)

    teams = read_teams(args.teams)
    print(f"analysing {args.master} ...")
    maps_right, info = analyse_master(args.master)
    maps_left = maps_right.flipped()
    x0, y0, _, _ = info["crop"]
    crop_w, _ = maps_right.size
    layouts = {}
    for style, overrides in STYLE_OVERRIDES.items():
        right = replace(TextLayout(), **overrides).scaled(info["scale"]).shifted(-x0, -y0)
        layouts[style] = (right, right.mirrored(crop_w))
    print("  ID hues (deg): " + ", ".join(f"{p}={h:.0f}" for p, h in zip(PARTS, info["hues"])))
    print("  base tones:    " + ", ".join(f"{p}={c}" for p, c in zip(PARTS, info["base"])))

    args.out.mkdir(parents=True, exist_ok=True)
    if args.debug:
        save_debug(maps_right, args.out / "debug_masks.png")

    ss = 4
    entries, seen = [], {}
    for team in teams:
        slug = slugify(team["city"])
        if slug in seen:
            print(f"  warning: {team['city']!r} overwrites {seen[slug]!r} ({slug}_*.png)", file=sys.stderr)
        seen[slug] = team["city"]
        colors = (team["shell"], team["stripe"], team["facemask"])
        layout_right, layout_left = layouts[team["text_style"]]
        lettering = None
        if team["text"]:
            lettering = render_lettering(team["text"], args.font, layout_right, ss=ss,
                                         outline=team["outline"] is not None)

        outputs = {}
        for side, maps, layout in (("right", maps_right, layout_right), ("left", maps_left, layout_left)):
            # The left helmet is the flipped render; its lettering is warped
            # onto the flipped shell afterwards, so it always reads normally.
            fill = outer = None
            if lettering is not None:
                fill_mask, outer_mask, mcx, mcy = lettering
                style = team["text_style"]
                fill = place_lettering(fill_mask, mcx, mcy, ss, layout, maps.alpha.shape, style=style)
                if outer_mask is not None:
                    outer = place_lettering(outer_mask, mcx, mcy, ss, layout, maps.alpha.shape, style=style)
            img = render_helmet(maps, colors, team["text_color"], fill, team["outline"], outer,
                                stripe_width=team["stripe_width"])
            img = fit_to_canvas(img, args.size, args.margin)
            img.save(args.out / f"{slug}_{side}.png")
            outputs[side] = img
        swatches = [("shell", team["shell"]), ("stripe", team["stripe"]), ("mask", team["facemask"])]
        if team["text"]:
            swatches.append(("text", team["text_color"]))
        if team["outline"]:
            swatches.append(("outline", team["outline"]))
        entries.append((team["city"], [outputs["right"], outputs["left"]], swatches))
        text_note = f", text {team['text']!r} {to_hex(team['text_color'])}" if team["text"] else ", no text"
        if team["outline"]:
            text_note += f" outline {to_hex(team['outline'])}"
        if team["text"] and team["text_style"] != "flat":
            text_note += f" ({team['text_style']})"
        if team["stripe_width"] < 1.0:
            text_note += f", stripe {team['stripe_width']:.0%} width"
        print(f"  {team['city']}: shell {to_hex(team['shell'])}, stripe {to_hex(team['stripe'])}, "
              f"facemask {to_hex(team['facemask'])}{text_note} -> {slug}_right/left.png")

    if not args.no_preview:
        contact_sheet(entries, args.out / "preview.png", HERE / "fonts" / "Oswald-Bold.ttf")
        print(f"wrote {args.out / 'preview.png'}")


if __name__ == "__main__":
    main()

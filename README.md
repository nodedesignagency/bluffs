# Team helmet generator

Recolors one ID-colored helmet render into a transparent PNG for every team in `teams.csv`.

```
pip install -r requirements.txt
python make_helmets.py
```

Outputs go to `output/`:

- `<City>_right.png`: helmet facing right
- `<City>_left.png`: helmet facing left
- `preview.png`: contact sheet with every team, on a checkerboard so transparency and edges are easy to check

Every file has the same canvas size, position and scale (default 1024×1024, `--size` to change).

## teams.csv

| column | required | notes |
|---|---|---|
| `city` | yes | used for the file names (`Los Angeles` → `Los_Angeles_right.png`) |
| `shell_hex` | yes | shell color (the red ID area) |
| `stripe_hex` | yes | top stripe color (the green ID area) |
| `facemask_hex` | yes | facemask color (the blue ID area) |
| `text` | no | text for the side of the shell, drawn big and exactly as written (made for 3 letters like `BUF`; longer text shrinks to fit). Leave blank for no text. |
| `text_hex` | no | text color. If blank, the most contrasting of stripe/facemask is used, or white/black. |
| `outline_hex` | no | outline around the letters. Leave blank for no outline. |
| `text_style` | no | `flat` (default) or `3d`: wraps the text around the shell so it curves and foreshortens like a real decal. |

Hex values can be `#RRGGBB`, `RRGGBB` or `#RGB`.

## Master image

`master_helmet.png` is a side-profile helmet facing right on a plain light background:
shell = red, top stripe = green, facemask = blue. Neutral parts (pads, screws) keep their
original color. Replace the file with your own render, or pass `--master path.png`.
Run with `--debug` to write `output/debug_masks.png` (matte, part masks, paint mask, shading).

The text size, position and curve are set in `TextLayout` at the top of `make_helmets.py`
(in master-image pixels for the right-facing helmet). The left version uses the mirrored
position, and the text is drawn after the flip, so it never reads backwards.

## Fonts

Helmet text uses `fonts/Anton-Regular.ttf` (heavy condensed, Impact-style). Use `--font` for another
TTF/OTF. The preview labels use `fonts/Oswald-Bold.ttf`. Both are SIL Open Font License
(`fonts/OFL-Anton.txt`, `fonts/OFL-Oswald.txt`).

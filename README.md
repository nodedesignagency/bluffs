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
| `text` | no | text to put on the side of the shell, drawn exactly as written. Leave blank for no text. |
| `text_hex` | no | text color. If blank, the most contrasting of stripe/facemask is used, or white/black. |

Hex values can be `#RRGGBB`, `RRGGBB` or `#RGB`.

## Master image

`master_helmet.png` is a side-profile helmet facing right on a plain light background:
shell = red, top stripe = green, facemask = blue. Neutral parts (pads, screws) keep their
original color. Replace the file with your own render, or pass `--master path.png`.
Run with `--debug` to write `output/debug_masks.png` (matte, part masks, paint mask, shading).

The text position and curve are set in `TextLayout` at the top of `make_helmets.py`
(in master-image pixels for the right-facing helmet). The left version uses the mirrored
position, and the text is drawn after the flip, so it never reads backwards.

## Font

`fonts/Oswald-Bold.ttf` (SIL Open Font License, see `fonts/OFL.txt`). Use `--font` for another font.

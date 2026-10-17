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
| `stripe_width` | no | stripe width as a percentage, e.g. `67` for a third narrower. Blank = 100. |

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

Helmet text uses `fonts/Anton-Regular.ttf` (heavy condensed, Impact-style) and ball text uses
`fonts/Fredoka-Bold.ttf` (chunky rounded). Use `--font` for another TTF/OTF. The preview labels
use `fonts/Oswald-Bold.ttf`. All are SIL Open Font License (`fonts/OFL-*.txt`).

## Basketballs

`make_balls.py` recolors `master_ball.png`, a matte pebbled red ball with black seams:

```
python make_balls.py
```

Outputs go to `output/balls/`: `<ABBR>.png` per team (transparent, same canvas, size and
position for all) and `preview.png` on the app's dark card color `#1A1A22`.

| column | required | notes |
|---|---|---|
| `abbr` | yes | text on the ball and the file name (`BOS` -> `BOS.png`) |
| `ball_hex` | yes | ball color (shading and pebble texture are kept; black seams stay as they are) |
| `team` | no | label in the preview |
| `text_hex` | no | text fill. Default white. |
| `outline_hex` | no | text outline. Default: the ball color 45% darker. |

The abbreviation is printed big across the middle seam (cap height 40% of the ball) in the
same lettering as the helmets: Anton at its natural width, white, outline 5% of the letter
height. It is wrapped onto the sphere, with a subtle top-to-bottom gradient, the pebble
texture multiplied over it at 20% and a soft inner shadow. The master's faint pink
edge halo is trimmed off (alpha eroded 2px, feathered 1px). The look is tuned with the
constants at the top of `make_balls.py`.

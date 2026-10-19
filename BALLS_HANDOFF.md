# Handoff: NBA basketball images

Read this first when picking up the basketball work in a new chat.

## Where things are

- Repo `nodedesignagency/bluffs`, branch `claude/epic-maxwell-yl4uho`. Commit and push every change there.
- `make_balls.py` builds the balls from `master_ball.png` (matte pebbled red ball, black seams).
  Shared helpers come from `make_helmets.py`.
- `balls.csv` has one row per team: `team,abbr,ball_hex,text_hex,outline_hex`.
  Blank `text_hex` means near-white `#F4F4F2`.
- Output goes to `output/balls/<ABBR>.png` (1024x1024 transparent PNG) plus `output/balls/preview.png`.
  `output/` is not in git; regenerate with `python3 make_balls.py`.
- The NFL helmets (`make_helmets.py`, `teams.csv`) are finished and delivered. Don't change them. If
  shared code in `make_helmets.py` is touched, check that the helmet outputs stay byte-for-byte the same.

## The look is final, so don't change it

The client approved the current look. Don't touch the constants or rendering in `make_balls.py` unless
the user asks. In short:

- Ball recolored from the master, pebbles softened to satin, thin light rim, clean circular edge.
- Abbreviation in Graduate (collegiate block, emboldened): cap height 45% of the ball, 72% wide, wrapped
  on the sphere, middle seam running across it, engraved edge shading, thin outline in the accent color.
- The M is custom-drawn (`m_outline`): Graduate's own M fills in when bolded. Its diagonals match the
  stem weight (`M_DIAGONAL = 92`). The user approved it after several rounds; any abbreviation with an M
  (MIA, MIN, MEM) uses it automatically.
- W, X and K were checked and read fine. If the user dislikes one, use the same fix as the M: add a
  custom glyph to `CUSTOM_GLYPHS` and change only that letter.

## Done (10 of 30), all approved

| Team | Abbr | Ball | Text | Outline |
|---|---|---|---|---|
| Boston Celtics | BOS | #007A33 | white | #BA9653 gold |
| Brooklyn Nets | BKN | #2A2A2A charcoal | white | #8A8D8F grey |
| New York Knicks | NYK | #1D428A | white | #F58426 orange |
| Philadelphia 76ers | PHI | #006BB6 | white | #C8102E red |
| Toronto Raptors | TOR | #BD1B21 | white | #1A1A1A black |
| Chicago Bulls | CHI | #C8C8C8 (white ball) | #F00940 red | #1A1A1A black |
| Cleveland Cavaliers | CLE | #C99A3A gold | #72253D wine | #F4F4F2 white |
| Detroit Pistons | DET | #1D42BA | #E9072B red | #F4F4F2 white |
| Milwaukee Bucks | MIL | #00471B | white | #EEE1C6 cream |
| Indiana Pacers | IND | #FDBB30 yellow | #002D62 navy | #BFC1C2 silver |

## Remaining (20)

ATL, CHA, MIA, ORL, WAS, DEN, MIN, OKC, POR, UTA, GSW, LAC, LAL, PHX, SAC, DAL, HOU, MEM, NOP, SAS.

## Workflow, one team at a time

The user sends one team at a time: the team name, its logo, and sometimes an instruction such as "use red
ball" or "blue ball and red text". Follow any instruction exactly.

1. Sample the colors from the logo with numpy: take the median over pixels picked by hue, and check the
   pixel count. A tiny count means the sample missed, so re-sample by hue.
2. Use the standard NBA abbreviation without asking.
3. Add the row to `balls.csv`, keeping rows in the order the teams were sent.
4. Render only that team:
   `(head -1 balls.csv; grep "^<Team Name>," balls.csv) > /tmp/one.csv && python3 make_balls.py --teams /tmp/one.csv --no-preview`
   (about 25 s per ball).
5. Look at it on the dark app card color `#1A1A22` and on light. Compare it with any existing ball in a
   similar color so the two don't look alike.
6. Send the PNG to the user (SendUserFile, attach), with a short note: ball, text and outline colors, and
   anything changed from the logo and why.
7. Commit and push.

Then wait for the next team. Show every image, one at a time, and don't batch teams.

## Color rules that worked

- Dark or mid balls: white text, outline in the team's accent color.
- Light balls (white, yellow, gold): text in a team color with a contrasting outline. A white ball uses
  base `#C8C8C8` so it keeps its shading; pure white looks flat. Muted golds read as beige, so use a richer gold.
- Black ball: use charcoal `#2A2A2A` so the black seams stay visible.
- If a logo color nearly matches a ball already made, use the team's official alternate (PHI uses
  `#006BB6` because its logo navy equals NYK's `#1D428A`), and say so.
- If unsure between two looks, render both side by side and let the user pick.

## At the end

Once all 30 are confirmed, regenerate everything (`python3 make_balls.py`), check every file, and build
a zip with all 30 PNGs plus `preview.png` as a contact sheet. Before building it, ask the user whether
the files should keep their abbreviation names (`BOS.png`) or use full team names like the NFL zip
(`Boston_Celtics.png`).

## Working with this user

- Short, clear messages. Show the image, say what changed, then wait.
- Don't spend time on speed tweaks or extra features; ask before anything big.
- Don't add text or design elements the user didn't ask for.

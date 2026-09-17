# CLAUDE.md — Truck Loading Optimisation

Context for anyone (including Claude Code) working in this folder. This documents
what already exists and how it fits together. It is **not** a task list.

## Purpose
Work out the fewest trucks needed to ship a set of steel drums, given a weight
limit per truck (21,500 kg comfortable / 21,772 kg = 48,000 lb hard) and optional
constraints. The end user is non-technical and only ever sees the Streamlit app.

## Files
- **`solver_core.py`** — the engine, no UI. `optimize(items, capacity, ...)` packs
  weighted items into bins, minimising bin count. Three engines: exact CP-SAT over
  **truck patterns** (preferred — collapses identical drums, so proofs are usually
  instant), exact CP-SAT **per item** (fallback for many distinct weights, only when
  `len(items) * upper_bound <= 40000`), and a pure-Python heuristic
  (best/first-fit-decreasing + seeded random restarts + local improvement). It runs
  the heuristic first for a fast warm bound, short-circuits to `exact-optimal` when
  that already meets the weight/count lower bound, then tries the **LP bound of the
  pattern model** (`_pattern_lp_bound`, GLOP — deterministic, milliseconds) and
  short-circuits again if that closes the gap; only then searches, with the bound
  added as a constraint so CP-SAT can stop early. This matters for drum-by-drum
  lists where every drum has its own weight: without the LP bound CP-SAT finds the
  optimum instantly but burns the whole time limit failing to prove it.
  `_lp_ceil` rounds with slack on purpose — a bound one too low only costs a proof,
  one too high would be a wrong answer. Everything
  is **deterministic** (seeded) so the same input always gives the same plan.
  ⚠️ CP-SAT runs with `num_search_workers = 1` **on purpose**: multi-worker is
  non-deterministic and, on ortools 9.15 / Python 3.14, ignores the time limit and
  hangs forever. Do not raise it.
  Supports: weight cap, safety margin (kg or %), max items per bin, keep-groups.
  **`optimize_by_bl(items, capacity, ..., mode=)`** loads by bill of lading: items
  carry a `bl`. Three modes, chosen in the app:
  **`"full"`** — fewest trucks full stop (same count as `optimize()`), and among
  plans that size the fewest trucks shared between BLs;
  **`"separate"`** — every BL strictly on its own trucks, nothing shared;
  **`"half"`** (the dad's usual rule) — **each BL gets its own trucks first**. Loaded alone a BL
  needs n trucks and only the last is part-filled, so each BL must keep at least
  **n − 1 trucks to itself**; only those part-filled "half trucks" may combine
  across BLs. Within that rule: **fewest trucks, then fewest shared trucks**. This
  can be more trucks than `"full"` (his first real sheet: 19 separate / 19 half /
  18 full) — that was a deliberate call by the user; the result carries
  `free_trucks` so the app can show the difference. Only one truck per BL can be shared in `"half"`,
  which is a simplification of "the half trucks" and not a rule of the problem —
  a BL of very heavy drums leaves every truck half empty and only its last one
  may combine. The user knows; the fill-level definition is still open.
  `_bl_alone` packs each BL by itself (used by `"separate"` and as the base of the
  others). `_bl_split_plan` does the half rule directly (per-BL `optimize`, pool each BL's emptiest truck, re-pack) and is
  the warm start and fallback. `_bl_patterns` is exact for `"full"` and `"half"`: CP-SAT pattern model, own
  trucks are patterns over one BL's drums (≥ own_min per BL, which is 0 for
  `"full"`), shared trucks are
  weight-only patterns fed from a pool any BL pays into; stage 1 minimises total,
  stage 2 caps it and minimises shared, each with its own LP floor and a
  short-circuit when the warm start already sits on it. `engine` is the stage-1
  proof (only "exact-optimal" if every per-BL n was proven too), `mix_engine`
  stage 2. Output order: each BL's own trucks in natural BL order (`_bl_key`,
  blank BL last), then shared trucks. Falls back to the split plan if any check
  fails. `total_proven` skips stage 1 when the count is already known (`"full"`).
  Checked against brute force (each mode's rule applied literally) on 300 random
  shipments per mode: (trucks, shared) match every time.
  Running `python3 solver_core.py` self-tests on the drum shipment and must print
  **17 bins @ 21,500** and **16 bins @ 21,772**, all 81 items placed, none over cap.
- **`streamlit_app.py`** — the web app the dad uses. Editable drum table (Item,
  BL no., Container no., Drum no., Weight, Qty), truck limit with kg/lb/tonne unit, optional safety
  margin / max-drums / keep-together, a Calculate button, per-truck result cards
  with fill bars, and CSV + Excel download. Styled to match the offline HTML tool.
  Built entirely on `solver_core.optimize`. **Upload takes .xlsx or .csv**
  (`read_file`): every sheet goes through `parse_grid`, the sheet with the most
  drums wins, rows without a weight are dropped. `parse_grid` (shared with the
  paste box) finds the heading row in the top 20 rows — a heading row never
  contains a number — and skips "Total" lines. His packing lists use
  `BL NO. | Container No | Drum No. | Gross Wt. (Kgs.) | DESTINATION`, with blank
  columns/rows around them; DESTINATION goes into Item (shown as "Item /
  Destination").
  The table is the **single source of truth** — the three ways to fill it (typing,
  a file upload, or an Excel paste) all just write into `st.session_state.table_df`
  and bump `grid_ver`, whose value is part of the `data_editor` key so the grid
  redraws instead of layering stale widget edits on top. `parse_block` reads a
  tab-separated block (header row auto-detected and mapped by name, otherwise
  columns guessed positionally; headings go through `_header_role`, which tries the
  exact name, then the name minus its unit, then what it contains — real packing
  lists say things like "Gross Wt. (Kgs.)"; if two headings map to the same
  column the first wins); `parse_columns` reads one Excel column per box and
  matches them up row by row, keeping blank lines so rows can't shift. Number
  parsing is deliberately strict (`_clean_num`) so a container number like
  `MSKU1234567` is never mistaken for a weight.
  One table covers both shapes of list **at the same time**: a row per drum type
  with a quantity, or a row per individual drum with a `Drum_no` and no quantity.
  There is deliberately no mode switch — **a blank quantity means one drum**, which
  is what makes a drum-by-drum paste work with nothing typed, and the app says how
  many rows it read that way. A header row is mapped by name (`_HEADER_MAP`; "Sr
  No."/"ID" are drum numbers, a bare "No." is a quantity). Without one, `_roles`
  reads the whole block at once: the weight is the numeric column whose median
  lands in `_MIN_W.._MAX_W` (50–60,000 kg) and is largest — so a long serial number
  can't be mistaken for a weight and a quantity can't either — with item /
  container / drum no. to its left and the quantity in the single column to its
  right. Where one column sits between item and weight, `_CONTAINER_RE` (ISO 6346)
  decides container vs drum number. Container and drum number are carried on each
  item purely as labels and never reach the model. Output columns follow the data, not the mode: `has_dno` adds a
  drum-number column to the truck tables and the Loading Plan only when some drum
  actually has one, and truck lines only collapse together when they share a drum
  number (or have none). The Drums Shipped sheet always groups by type/container
  and ignores drum numbers — it is the summary; the per-drum detail is on the plan.
  **BL no.** is a real column (the dad puts the destination in Item for now). It is
  only read from a header row — headerless `_roles` never guesses a BL. A **Full mix /
  Half mix / BL separate** radio sits above the Calculate button (not the sidebar,
  so its default can follow the table): BL separate when the table holds two or
  more distinct BLs, Full mix otherwise, keyed on that so his own pick sticks
  until the BLs appear or disappear. Any mode but Full mix overrides
  keep-together, and the app says when Full mix would need fewer trucks. Then the
  results get a **By BL** table/sheet (own truck numbers, drums on shared trucks),
  a heading before each BL's trucks, amber cards for shared trucks, and `Truck_BL`
  on the Loading Plan. BL columns only appear in the output when some drum has a
  BL, so a shipment without BLs looks exactly as before.
- **`Truck_Loading_Planner.html`** — standalone offline browser tool (same idea,
  pure-JS heuristic, no install/internet). Reference / backup for field use.
- **`drum_truck_planner.py`** — original exact CLI (edit the DRUMS list + caps, run).
- **`example_drums.csv`** — sample input for the app (Description, Weight_kg, Qty).
- **`Drum_Truck_Loading_Plan.xlsx`** — example output (16- and 17-truck plans).
- **`requirements.txt`** — streamlit, pandas, ortools, openpyxl.

## Run locally
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py     # opens in browser
python3 solver_core.py             # regression self-test
```

## Deploy (free, hosted URL)
Push this folder to a GitHub repo → https://share.streamlit.io → New app → select
the repo, main file `streamlit_app.py`. `requirements.txt` installs everything.

## Constraints that matter
- **Scope is trucks/drums only** — do not generalise it into an abstract multi-problem
  tool; keep the wording and UI drum/truck-specific.
- **Streamlit Community Cloud = 1 GB RAM.** Not a limiter here (models are a few MB).
  Keep the exact-model size cap and heuristic fallback so it stays fast and in-memory;
  the only thing that grows with input size is solve time, bounded by the UI slider.
- **Every plan must place all items and never exceed the usable cap** (capacity minus
  safety margin). Preserve this guarantee in any change.
- **Keep it deterministic** — no unseeded randomness.

## Known-good numbers (regression)
81 drums, total 343,269 kg → 17 trucks @ 21,500 kg cap, 16 trucks @
21,772 kg (48,000 lb) cap. Heaviest truck in the 16-plan: 21,749 kg (47,948 lb).

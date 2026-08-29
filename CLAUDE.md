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
  that already meets the weight/count lower bound, otherwise proves it. Everything
  is **deterministic** (seeded) so the same input always gives the same plan.
  ⚠️ CP-SAT runs with `num_search_workers = 1` **on purpose**: multi-worker is
  non-deterministic and, on ortools 9.15 / Python 3.14, ignores the time limit and
  hangs forever. Do not raise it.
  Supports: weight cap, safety margin (kg or %), max items per bin, keep-groups.
  Running `python3 solver_core.py` self-tests on the drum shipment and must print
  **17 bins @ 21,500** and **16 bins @ 21,772**, all 81 items placed, none over cap.
- **`streamlit_app.py`** — the web app the dad uses. Editable drum table (Item,
  Container no., Weight, Qty), truck limit with kg/lb/tonne unit, optional safety
  margin / max-drums / keep-together, a Calculate button, per-truck result cards
  with fill bars, and CSV + Excel download. Styled to match the offline HTML tool.
  Built entirely on `solver_core.optimize`.
  The table is the **single source of truth** — the three ways to fill it (typing,
  a CSV upload, or an Excel paste) all just write into `st.session_state.table_df`
  and bump `grid_ver`, whose value is part of the `data_editor` key so the grid
  redraws instead of layering stale widget edits on top. `parse_block` reads a
  tab-separated block (header row auto-detected and mapped by name, otherwise
  columns guessed positionally); `parse_columns` reads one Excel column per box and
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

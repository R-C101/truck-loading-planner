# Truck / Loading Optimisation kit

Tools to work out the **fewest trucks** (or bins) needed to ship a set of items,
given a weight limit per truck and optional constraints. Built originally for
loading steel drums out of containers onto 21.5 t / 48,000 lb trucks, but the
core is general.

## What's in this folder

| File | What it is |
|---|---|
| `solver_core.py` | The engine. Reusable optimiser: exact (OR-tools) + heuristic fallback. Import it from anything. |
| `streamlit_app.py` | A working web app (form → plan) built on `solver_core`. This is the thing to host. |
| `drum_truck_planner.py` | Standalone command-line script (the original exact solver). Good reference. |
| `Truck_Loading_Planner.html` | Offline single-file browser tool (no install, no internet). Hand this to anyone. |
| `Drum_Truck_Loading_Plan.xlsx` | Example output (16- and 17-truck plans). |
| `example_drums.csv` | Sample input in the CSV shape the app accepts. |
| `requirements.txt` | Python deps for deployment. |
| `CLAUDE.md` | Project context for Claude Code (what exists and how it fits together). |

## Run the web app locally
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```
Opens in your browser. Edit the table or upload a CSV, set the truck limit, tick
any constraints, press **Calculate**.

## Deploy it free (hosted URL for others)
1. Put this folder in a GitHub repo (public or private).
2. Go to https://share.streamlit.io → **New app** → pick the repo → main file
   `streamlit_app.py` → Deploy.
3. `requirements.txt` installs everything automatically. You get a shareable URL.

**About the 1 GB RAM limit (Streamlit Community Cloud):** not a problem here.
The optimiser builds a small model — for ~80 items it uses a few MB. `solver_core`
also caps the exact model size and falls back to the fast heuristic on very large
inputs, so it stays within memory and returns quickly. The only thing that grows
with problem size is *solve time*, which is bounded by the "Max solve time" slider.

## How the solver decides
- Runs the heuristic first (best/first-fit-decreasing + seeded restarts + local
  improvement) to get a strong solution fast.
- If OR-tools is available and the problem is a normal size, it then runs the
  exact CP-SAT model to **prove** the minimum number of trucks (within the time
  limit). Otherwise it returns the heuristic result.
- Deterministic: the same input always produces the same plan.

## Constraints supported
- Weight cap per truck (with unit kg / lb / tonnes)
- Safety margin (kg or %)
- Max items per truck (bed-space limit)
- Keep item types together where possible (soft preference)

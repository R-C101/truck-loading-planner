"""
streamlit_app.py  —  web UI for the truck loading optimiser.

Run locally:   streamlit run streamlit_app.py
Deploy free:   push this folder to a GitHub repo, then on
               https://share.streamlit.io -> New app -> pick the repo,
               main file = streamlit_app.py.  requirements.txt installs the rest.
"""
import io
import re
import time
import pandas as pd
import streamlit as st
from solver_core import optimize

LB = 2.20462262

st.set_page_config(page_title="Truck Loading Planner", page_icon="🚚", layout="centered")

st.markdown("""
<style>
  .block-container{padding-top:1.4rem;max-width:900px}
  h1,h2,h3{font-family:Arial,Helvetica,sans-serif;color:#1f4e5f}
  .banner{background:#1f4e5f;color:#fff;padding:18px 22px;border-radius:10px;margin-bottom:6px}
  .banner h1{color:#fff;margin:0;font-size:24px}
  .banner p{margin:6px 0 0;color:#cfe0e6;font-size:13.5px}
  .stButton>button[kind="primary"]{background:#c0522e;color:#fff;font-weight:bold;
     border:none;border-radius:8px;padding:10px 26px;font-size:16px}
  .stButton>button[kind="primary"]:hover{background:#a8461f;color:#fff}
  .stButton>button[kind="secondary"]{background:#fff;color:#1f4e5f;font-weight:600;
     border:1px solid #c9d6dc;border-radius:8px;padding:7px 16px;font-size:14px}
  .stButton>button[kind="secondary"]:hover{background:#f2f7f9;border-color:#1f4e5f;
     color:#1f4e5f}
  .truckcard{border:1px solid #d7e0e5;border-radius:9px;margin:10px 0;overflow:hidden}
  .truckcard .top{background:#1f4e5f;color:#fff;padding:9px 14px;display:flex;
     justify-content:space-between;font-size:14px;flex-wrap:wrap;gap:6px}
  .truckcard .top b{font-size:15px}
  .bar{height:8px;background:#e3ebee}
  .bar span{display:block;height:100%;background:#2b6b80}
  .bar span.full{background:#c0522e}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="banner">
  <h1>🚚 Truck Loading Planner</h1>
  <p>Enter the drums and the truck's weight limit, then press Calculate. You get the
  fewest trucks and exactly which drums go on each one.</p>
</div>
""", unsafe_allow_html=True)

DEFAULT = pd.DataFrame({
    "Item":      pd.Series([], dtype="object"),
    "Container": pd.Series([], dtype="object"),
    "Weight_kg": pd.Series([], dtype="float"),
    "Qty":       pd.Series([], dtype="Int64"),
})
COLS = ["Item", "Container", "Weight_kg", "Qty"]


# ---------------- reading pasted / uploaded rows ----------------
# Dad copies straight out of Excel. Two shapes turn up: a whole block (cells are
# tab separated, one row per line) or a single column at a time (one value per
# line, no tabs). Both end up in the same editable table below so he can check
# and fix anything before pressing Calculate.

_HEADER_MAP = {
    "item": "Item", "items": "Item", "description": "Item", "desc": "Item",
    "product": "Item", "material": "Item", "name": "Item", "drum": "Item",
    "drumtype": "Item", "type": "Item",
    "container": "Container", "containerno": "Container", "cont": "Container",
    "contno": "Container", "containernumber": "Container", "box": "Container",
    "weight": "Weight_kg", "weightkg": "Weight_kg", "wt": "Weight_kg",
    "kg": "Weight_kg", "kgs": "Weight_kg", "unitweight": "Weight_kg",
    "weightperdrum": "Weight_kg", "grossweight": "Weight_kg",
    "netweight": "Weight_kg", "weightperunit": "Weight_kg",
    "qty": "Qty", "quantity": "Qty", "nos": "Qty", "no": "Qty", "num": "Qty",
    "count": "Qty", "pcs": "Qty", "pieces": "Qty", "drums": "Qty",
    "number": "Qty", "noofdrums": "Qty",
}


def _key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _clean_num(v):
    """'8,065 kg' / '7 935' / '8065.0' -> a bare number string, else None.
    Deliberately strict: 'MSKU1234567' must never be read as a weight."""
    s = str(v).replace("\u00a0", " ").strip().replace(",", "")
    s = re.sub(r"(?i)\s*(kgs?|lbs?)\s*$", "", s)          # trailing unit
    s = re.sub(r"\s+", "", s)                              # space thousands sep
    return s if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def _is_num(v):
    return _clean_num(v) is not None


def _num(v):
    s = _clean_num(v)
    return float(s) if s is not None else None


def _positional(cells):
    """No header row — work out what the columns are from the values."""
    c = list(cells) + [""] * 4
    if len(cells) >= 4:
        return {"Item": c[0], "Container": c[1], "Weight_kg": c[2], "Qty": c[3]}
    if len(cells) == 3:
        if _is_num(c[1]):                       # item, weight, qty
            return {"Item": c[0], "Weight_kg": c[1], "Qty": c[2]}
        return {"Item": c[0], "Container": c[1], "Weight_kg": c[2]}
    if len(cells) == 2:
        if _is_num(c[0]) and _is_num(c[1]):     # weight, qty
            return {"Weight_kg": c[0], "Qty": c[1]}
        if _is_num(c[1]):
            return {"Item": c[0], "Weight_kg": c[1]}
        return {"Item": c[0], "Container": c[1]}
    return {"Weight_kg": c[0]} if _is_num(c[0]) else {"Item": c[0]}


def _record(rec):
    item = str(rec.get("Item") or "").strip()
    cont = str(rec.get("Container") or "").strip()
    w, q = _num(rec.get("Weight_kg")), _num(rec.get("Qty"))
    if not item and not cont and w is None and q is None:
        return None
    return {"Item": item or None, "Container": cont or None,
            "Weight_kg": w, "Qty": int(q) if q is not None else None}


def parse_block(text):
    """A block of cells copied from Excel: tab separated, one row per line."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return _normalise(DEFAULT.copy())
    grid = [[c.strip() for c in l.split("\t")] if "\t" in l else [l.strip()]
            for l in lines]
    head = [_HEADER_MAP.get(_key(c)) for c in grid[0]]
    if sum(h is not None for h in head) >= 2:       # first line is a header row
        body, cols = grid[1:], head
    else:
        body, cols = grid, None
    out = []
    for cells in body:
        rec = ({h: v for h, v in zip(cols, cells) if h} if cols
               else _positional(cells))
        r = _record(rec)
        if r:
            out.append(r)
    return _normalise(pd.DataFrame(out, columns=COLS))


def _lines(text):
    """One pasted column -> its values, keeping blanks so the rows stay lined up."""
    ls = [l.strip() for l in (text or "").splitlines()]
    while ls and ls[-1] == "":
        ls.pop()
    return ls


def parse_columns(items, conts, weights, qtys):
    """Each column pasted into its own box; matched up row by row."""
    cols = {"Item": _lines(items), "Container": _lines(conts),
            "Weight_kg": _lines(weights), "Qty": _lines(qtys)}
    n = max((len(v) for v in cols.values()), default=0)
    out = []
    for i in range(n):
        r = _record({k: (v[i] if i < len(v) else "") for k, v in cols.items()})
        if r:
            out.append(r)
    return _normalise(pd.DataFrame(out, columns=COLS))


def _normalise(df):
    """Same four columns, same order, right types — whatever came in."""
    df = df.copy()
    for c in COLS:
        if c not in df.columns:
            df[c] = None
    df = df[COLS + [c for c in df.columns if c not in COLS]]
    df["Item"] = df["Item"].astype("object")
    df["Container"] = df["Container"].astype("object")
    df["Weight_kg"] = pd.to_numeric(df["Weight_kg"], errors="coerce")
    df["Qty"] = pd.to_numeric(df["Qty"], errors="coerce").round().astype("Int64")
    return df.reset_index(drop=True)

# ---------------- sidebar: truck limit + options ----------------
with st.sidebar:
    st.header("Truck limit")
    cap_val = st.number_input("Weight limit per truck", min_value=1.0, value=21500.0, step=100.0)
    cap_unit = st.selectbox("Unit", ["kg", "lb", "tonnes"], index=0)

    st.header("Optional")
    use_margin = st.checkbox("Safety margin")
    margin_val = st.number_input("Load up to this much below the limit", min_value=0.0,
                                 value=0.0, step=50.0, disabled=not use_margin)
    margin_unit = st.selectbox("Margin unit", ["kg", "%"], disabled=not use_margin)

    use_maxn = st.checkbox("Limit drums per truck (space)")
    max_n = st.number_input("Max drums per truck", min_value=1, value=10, step=1, disabled=not use_maxn)

    keep = st.checkbox("Keep drum types together where possible")
    prove = st.checkbox("Prove it's the fewest possible trucks", value=True)
    time_limit = st.slider("Max proof time (seconds)", 3, 60, 10, disabled=not prove)
    st.caption("Leave this ticked. The proof is normally instant and it's what lets "
               "the app say the truck count can't be beaten. On an awkward load it "
               "stops at the time limit and returns the best plan it found.")

# ---------------- step 1: items ----------------
# The table is the single source of truth. Uploading a CSV or pasting from Excel
# just fills it in; nothing goes to the solver except what is on screen.
if "table_df" not in st.session_state:
    st.session_state.table_df = _normalise(DEFAULT.copy())
st.session_state.setdefault("grid_ver", 0)      # bumping the key redraws the grid
st.session_state.setdefault("paste_ver", 0)     # bumping the key clears the boxes
st.session_state.setdefault("upload_id", None)


def _on_screen():
    """The table as it stands right now, including anything typed into it."""
    return st.session_state.get("last_edited", st.session_state.table_df)


def _fill_table(df, replace):
    base = _on_screen()
    base = base.dropna(subset=COLS, how="all") if len(base) else base
    st.session_state.table_df = _normalise(
        df if replace else pd.concat([base, df], ignore_index=True))
    st.session_state.grid_ver += 1
    st.session_state.paste_ver += 1


def _preview_and_add(parsed, tag):
    if parsed.empty:
        st.caption("Nothing read yet — paste the cells above.")
        return
    drums = int(parsed["Qty"].fillna(0).sum())
    st.caption(f"Read **{len(parsed)} rows**, {drums:,} drums:")
    st.dataframe(parsed, width="stretch", hide_index=True)
    gaps = int(parsed["Weight_kg"].isna().sum() + parsed["Qty"].isna().sum())
    if gaps:
        st.warning(f"{gaps} weight/quantity cell(s) didn't come through as numbers. "
                   f"Add them in the table below after adding these rows.")
    b1, b2 = st.columns(2)
    if b1.button("➕ Add to table", key=f"add_{tag}", width="stretch"):
        _fill_table(parsed, replace=False)
        st.rerun()
    if b2.button("↻ Replace table", key=f"rep_{tag}", width="stretch"):
        _fill_table(parsed, replace=True)
        st.rerun()


st.subheader("1 · Drums in this shipment")
st.caption("One row per drum type per container — the item, the container it comes "
           "from, the weight of ONE drum (kg), and how many. If the same drum arrives "
           "in several containers, add a row for each so the container numbers can be "
           "checked off later. Type into the table, paste from Excel, or upload a CSV — "
           "then check and fix anything in the table before pressing Calculate.")

with st.expander("📋 Paste from Excel", expanded=False):
    t_block, t_cols = st.tabs(["Paste the whole block", "Paste one column at a time"])
    with t_block:
        st.caption("Select the cells in Excel, copy, and paste here. A header row is "
                   "fine — it gets recognised and skipped. Column order: item, "
                   "container, weight, quantity.")
        blk = st.text_area("Paste cells", height=170, label_visibility="collapsed",
                           key=f"paste_block_{st.session_state.paste_ver}",
                           placeholder=("8065kg drum\tMSKU1234567\t8065\t30\n"
                                        "8065kg drum\tTGHU7654321\t8065\t33\n"
                                        "6491kg drum\tMSKU1234567\t6491\t12"))
        _preview_and_add(parse_block(blk), "blk")
    with t_cols:
        st.caption("Copy one Excel column at a time — each value on its own line. "
                   "The boxes are matched up row by row, so paste the same number of "
                   "lines into each. Leave a box empty if you don't have that column.")
        k = st.session_state.paste_ver
        q1, q2 = st.columns(2)
        c_item = q1.text_area("Item", height=150, key=f"col_item_{k}")
        c_cont = q2.text_area("Container no.", height=150, key=f"col_cont_{k}")
        q3, q4 = st.columns(2)
        c_wt = q3.text_area("Weight (kg)", height=150, key=f"col_wt_{k}")
        c_qty = q4.text_area("Quantity", height=150, key=f"col_qty_{k}")
        if any("\t" in (t or "") for t in (c_item, c_cont, c_wt, c_qty)):
            st.info("That looks like more than one column — the other tab handles "
                    "a whole block in one go.")
        _preview_and_add(parse_columns(c_item, c_cont, c_wt, c_qty), "cols")

up = st.file_uploader("Upload CSV (optional)", type=["csv"], label_visibility="collapsed")
if up is not None:
    uid = f"{up.name}:{up.size}"
    if st.session_state.upload_id != uid:      # only on a genuinely new file, so a
        st.session_state.upload_id = uid       # rerun never wipes what he has typed
        st.session_state.table_df = _normalise(pd.read_csv(up))
        st.session_state.grid_ver += 1

edited = st.data_editor(
    st.session_state.table_df, num_rows="dynamic", width="stretch",
    hide_index=True, key=f"grid_{st.session_state.grid_ver}",
    column_config={
        "Item": st.column_config.TextColumn("Item", width="medium"),
        "Container": st.column_config.TextColumn("Container no.", width="medium"),
        "Weight_kg": st.column_config.NumberColumn("Weight (kg)", min_value=0, step=1),
        "Qty": st.column_config.NumberColumn("Quantity", min_value=0, step=1),
    },
)
st.session_state.last_edited = edited

_ok = edited.dropna(subset=["Weight_kg", "Qty"]) if len(edited) else edited
if len(_ok):
    st.caption(f"**{len(_ok)} rows · {int(_ok['Qty'].sum()):,} drums · "
               f"{(_ok['Weight_kg'] * _ok['Qty']).sum():,.0f} kg** in the table.")
if st.columns([1, 3])[0].button("Clear table", width="stretch"):
    st.session_state.table_df = _normalise(DEFAULT.copy())
    st.session_state.grid_ver += 1
    st.session_state.pop("last_edited", None)
    st.rerun()


def to_kg(v, unit):
    return v / LB if unit == "lb" else v * 1000 if unit == "tonnes" else v

st.subheader("2 · Plan")
go = st.button("Calculate loading plan", type="primary")

if go:
    items, skipped = [], 0
    for _, r in edited.iterrows():
        w = r.get("Weight_kg"); q = r.get("Qty")
        blank = all(pd.isna(r.get(c)) or str(r.get(c)).strip() == "" for c in COLS)
        if pd.isna(w) or pd.isna(q):
            skipped += 0 if blank else 1      # a part-filled row is a mistake, say so
            continue
        try:
            w = float(w); q = int(q)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if w <= 0 or q <= 0:
            skipped += 1
            continue
        desc = str(r.get("Item") or r.get("Description") or "drum")
        cont = r.get("Container")
        cont = "" if cont is None or pd.isna(cont) else str(cont).strip()
        for _ in range(q):
            # container rides along on the item; it never affects the packing
            items.append({"weight": w, "label": desc, "container": cont,
                          "group": f"{desc}|{w}"})
    if not items:
        st.error("Please enter at least one row with a weight and a quantity.")
        st.stop()
    if skipped:
        st.warning(f"{skipped} row(s) were left out — they have no weight or no "
                   f"quantity. Fill them in above and calculate again if they "
                   f"should be shipped.")

    cap_kg = to_kg(cap_val, cap_unit)
    spin_msg = (f"Optimising and proving the fewest possible trucks… "
                f"(stops after {int(time_limit)}s on an awkward load)" if prove
                else "Optimising… (fast plan, no optimality proof)")
    with st.spinner(spin_msg):
        t0 = time.perf_counter()
        res = optimize(
            items, cap_kg,
            max_items_per_bin=int(max_n) if use_maxn else None,
            keep_groups=keep,
            safety_margin=(margin_val if use_margin else 0.0),
            margin_is_pct=(use_margin and margin_unit == "%"),
            time_limit=time_limit,
            force=None if prove else "heuristic",
        )
        elapsed = time.perf_counter() - t0

    if res["engine"] == "infeasible":
        bad = res["infeasible_item"]
        st.error(f"'{bad['label']}' weighs {bad['weight']:,.0f} kg — more than a truck's "
                 f"usable limit of {res['capacity_used']:,.0f} kg. Can't fit it. "
                 f"Check the weight or raise the truck limit.")
        st.stop()
    if res["engine"] == "error-margin":
        st.error("Safety margin is larger than the truck limit — nothing can be loaded.")
        st.stop()

    bins = res["bins"]
    note = {"exact-optimal": "✅ proved the fewest possible trucks",
            "exact-feasible": "very good solution (time limit reached before proof)",
            "best-found": "strong solution — couldn't prove a truck can be saved in the time allowed",
            "heuristic": "very good solution"}.get(res["engine"], res["engine"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trucks needed", len(bins))
    c2.metric("Total drums", res["n_items"])
    c3.metric("Total weight", f"{res['total_weight']:,.0f} kg")
    c4.metric("Solve time", f"{elapsed:.2f} s")
    st.success(f"All {res['n_items']} drums placed across {len(bins)} trucks — {note}. "
               f"No truck exceeds {res['capacity_used']:,.0f} kg. "
               f"Solved in {elapsed:.2f} s.")

    rows, summary = [], []
    for ti, b in enumerate(bins, 1):
        load = sum(i["weight"] for i in b)
        pct = min(100, load / res["capacity_used"] * 100)
        spare = res["capacity_used"] - load
        g = {}
        for it in b:
            k = (it["label"], it.get("container", ""), it["weight"])
            g[k] = g.get(k, 0) + 1
        st.markdown(f"""
        <div class="truckcard">
          <div class="top"><b>Truck {ti}</b>
            <span>{load:,.0f} kg / {load*LB:,.0f} lb · {len(b)} drums · {pct:.0f}% full</span></div>
          <div class="bar"><span class="{'full' if pct>97 else ''}" style="width:{pct}%"></span></div>
        </div>""", unsafe_allow_html=True)
        df = pd.DataFrame(
            [{"Item": k[0], "Container": k[1], "Weight/drum (kg)": k[2],
              "Qty": v, "Line (kg)": k[2]*v}
             for k, v in sorted(g.items(), key=lambda kv: (-kv[0][2], kv[0][1]))])
        st.dataframe(df, width="stretch", hide_index=True)
        for k, v in g.items():
            rows.append({"Truck": ti, "Item": k[0], "Container": k[1],
                         "Weight_kg": k[2], "Qty": v, "Line_kg": k[2]*v,
                         "Truck_Total_kg": round(load, 1),
                         "Truck_Total_lb": round(load*LB, 1),
                         "Truck_Drums": len(b),
                         "Truck_Percent_Full": round(pct, 1)})
        summary.append({"Truck": ti,
                        "Total_kg": round(load, 1),
                        "Total_lb": round(load*LB, 1),
                        "Drums": len(b),
                        "Percent_Full": round(pct, 1),
                        "Spare_capacity_kg": round(spare, 1)})

    plan_df = pd.DataFrame(rows)
    summary_df = pd.DataFrame(summary)

    # what was shipped: one row per drum type, so the totals can be checked
    # against the original packing list at a glance
    types = {}
    for b in bins:
        for it in b:
            k = (it["label"], it.get("container", ""), it["weight"])
            types[k] = types.get(k, 0) + 1
    drum_rows = [{"Item": k[0], "Container": k[1], "Weight_kg": k[2], "Qty": v,
                  "Total_kg": round(k[2]*v, 1), "Total_lb": round(k[2]*v*LB, 1)}
                 for k, v in sorted(types.items(), key=lambda kv: (-kv[0][2], kv[0][1]))]
    drum_rows.append({"Item": "TOTAL", "Container": None, "Weight_kg": None,
                      "Qty": sum(types.values()),
                      "Total_kg": round(res["total_weight"], 1),
                      "Total_lb": round(res["total_weight"]*LB, 1)})
    drum_df = pd.DataFrame(drum_rows)

    st.subheader("3 · Drums shipped (summary)")
    st.dataframe(drum_df, width="stretch", hide_index=True)

    d1, d2 = st.columns(2)
    cbuf = io.StringIO()
    plan_df.to_csv(cbuf, index=False)
    cbuf.write("\nTRUCK SUMMARY\n");  summary_df.to_csv(cbuf, index=False)
    cbuf.write("\nDRUMS SHIPPED\n");  drum_df.to_csv(cbuf, index=False)
    d1.download_button("⬇ Download plan (CSV)", cbuf.getvalue().encode(),
                       "loading_plan.csv", "text/csv", width="stretch")
    xbuf = io.BytesIO()
    with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
        plan_df.to_excel(xw, index=False, sheet_name="Loading Plan")
        summary_df.to_excel(xw, index=False, sheet_name="Truck Summary")
        drum_df.to_excel(xw, index=False, sheet_name="Drums Shipped")
    d2.download_button("⬇ Download plan (Excel)", xbuf.getvalue(),
                       "loading_plan.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch")

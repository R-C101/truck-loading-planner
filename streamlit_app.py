"""
streamlit_app.py  —  web UI for the truck loading optimiser.

Run locally:   streamlit run streamlit_app.py
Deploy free:   push this folder to a GitHub repo, then on
               https://share.streamlit.io -> New app -> pick the repo,
               main file = streamlit_app.py.  requirements.txt installs the rest.
"""
import io
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
  .stButton>button{background:#c0522e;color:#fff;font-weight:bold;border:none;
     border-radius:8px;padding:10px 26px;font-size:16px}
  .stButton>button:hover{background:#a8461f;color:#fff}
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
    "Weight_kg": pd.Series([], dtype="float"),
    "Qty":       pd.Series([], dtype="Int64"),
})

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
    prove = st.checkbox("Prove it's the fewest possible trucks (slower)", value=False)
    time_limit = st.slider("Max proof time (seconds)", 3, 60, 5, disabled=not prove)
    st.caption("The plan is found almost instantly and is the same either way. "
               "Tick *Prove* only if you want a mathematical guarantee that no truck "
               "can be saved on tight loads — that proof is the slow part.")

# ---------------- step 1: items ----------------
st.subheader("1 · Drums in this shipment")
st.caption("Fill in a row per drum type — its weight (kg), a name, and how many. "
           "Add rows with ✚, remove with 🗑. Weight is the gross weight of ONE drum. "
           "Or upload a CSV with columns Weight_kg, Item, Qty.")
up = st.file_uploader("Upload CSV (optional)", type=["csv"], label_visibility="collapsed")
data = pd.read_csv(up) if up is not None else DEFAULT.copy()
edited = st.data_editor(
    data, num_rows="dynamic", use_container_width=True, hide_index=True, key="grid",
    column_config={
        "Item": st.column_config.TextColumn("Item", width="large"),
        "Weight_kg": st.column_config.NumberColumn("Weight (kg)", min_value=0, step=1),
        "Qty": st.column_config.NumberColumn("Quantity", min_value=0, step=1),
    },
)

def to_kg(v, unit):
    return v / LB if unit == "lb" else v * 1000 if unit == "tonnes" else v

st.subheader("2 · Plan")
go = st.button("Calculate loading plan", type="primary")

if go:
    items = []
    for _, r in edited.iterrows():
        w = r.get("Weight_kg"); q = r.get("Qty")
        if pd.isna(w) or pd.isna(q):
            continue
        try:
            w = float(w); q = int(q)
        except (ValueError, TypeError):
            continue
        if w <= 0 or q <= 0:
            continue
        desc = str(r.get("Item") or r.get("Description") or "drum")
        for _ in range(q):
            items.append({"weight": w, "label": desc, "group": f"{desc}|{w}"})
    if not items:
        st.error("Please enter at least one row with a weight and a quantity.")
        st.stop()

    cap_kg = to_kg(cap_val, cap_unit)
    spin_msg = (f"Optimising… (plan is instant; may spend up to {int(time_limit)}s "
                f"proving the fewest-possible trucks)" if prove
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
            k = (it["label"], it["weight"]); g[k] = g.get(k, 0) + 1
        st.markdown(f"""
        <div class="truckcard">
          <div class="top"><b>Truck {ti}</b>
            <span>{load:,.0f} kg / {load*LB:,.0f} lb · {len(b)} drums · {pct:.0f}% full</span></div>
          <div class="bar"><span class="{'full' if pct>97 else ''}" style="width:{pct}%"></span></div>
        </div>""", unsafe_allow_html=True)
        df = pd.DataFrame(
            [{"Description": k[0], "Weight/drum (kg)": k[1], "Qty": v, "Line (kg)": k[1]*v}
             for k, v in sorted(g.items(), key=lambda kv: -kv[0][1])])
        st.dataframe(df, use_container_width=True, hide_index=True)
        for k, v in g.items():
            rows.append({"Truck": ti, "Item": k[0], "Weight_kg": k[1],
                         "Qty": v, "Line_kg": k[1]*v,
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
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Download plan (CSV)", plan_df.to_csv(index=False).encode(),
                       "loading_plan.csv", "text/csv", use_container_width=True)
    xbuf = io.BytesIO()
    with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
        plan_df.to_excel(xw, index=False, sheet_name="Loading Plan")
        summary_df.to_excel(xw, index=False, sheet_name="Truck Summary")
    d2.download_button("⬇ Download plan (Excel)", xbuf.getvalue(),
                       "loading_plan.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)

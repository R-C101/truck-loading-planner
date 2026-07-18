"""
solver_core.py  —  general "pack items into bins by weight" optimiser.

Designed for loading problems (drums onto trucks) but generic: any items with a
weight, packed into bins with a capacity, minimising the number of bins.

Two engines:
  * EXACT  — OR-tools CP-SAT, proves the minimum number of bins. Used for
             small/medium problems (fast, low memory — fine on a 1 GB host).
  * HEURISTIC — best/first-fit-decreasing + seeded random restarts + a local
             improvement pass. Used as a fast warm-start, as a fallback if
             OR-tools isn't installed, and automatically for very large inputs
             where the exact model would be slow.

Constraints supported:
  * capacity            (hard weight cap per bin, after safety margin)
  * max_items_per_bin   (e.g. bed-space limit)
  * keep_groups         (soft: prefer not to split an item "group"/type across bins)

Everything is deterministic (seeded) => same input always gives the same plan.

Public API:
    optimize(items, capacity, max_items_per_bin=None, keep_groups=False,
             safety_margin=0.0, margin_is_pct=False, time_limit=20, force=None)
      items: list of dicts, each {"weight": float, "label": str, "group": str}
      returns: dict {
         "bins":   [ [item, item, ...], ... ],   # each inner list = one bin
         "engine": "exact-optimal" | "exact-feasible" | "heuristic",
         "capacity_used": float, "total_weight": float, "n_items": int,
         "infeasible_item": item|None }
"""
from __future__ import annotations
import random

try:
    from ortools.sat.python import cp_model
    _HAS_ORTOOLS = True
except Exception:
    _HAS_ORTOOLS = False


# ----------------------------------------------------------------------
# heuristic engine (pure python, no dependencies)
# ----------------------------------------------------------------------
def _first_fit(order, cap, max_items):
    bins = []
    for it in order:
        placed = False
        for b in bins:
            if b["load"] + it["weight"] <= cap + 1e-6 and (not max_items or len(b["items"]) < max_items):
                b["items"].append(it); b["load"] += it["weight"]; placed = True; break
        if not placed:
            bins.append({"items": [it], "load": it["weight"]})
    return bins


def _best_fit(order, cap, max_items, keep):
    bins = []
    for it in order:
        best, best_score = -1, -1e18
        for i, b in enumerate(bins):
            rem = cap - (b["load"] + it["weight"])
            if rem < -1e-6:
                continue
            if max_items and len(b["items"]) >= max_items:
                continue
            score = -rem
            if keep and any(x["group"] == it["group"] for x in b["items"]):
                score += cap * 2
            if score > best_score:
                best_score, best = score, i
        if best < 0:
            bins.append({"items": [it], "load": it["weight"]})
        else:
            bins[best]["items"].append(it); bins[best]["load"] += it["weight"]
    return bins


def _improve(bins, cap, max_items):
    """Dissolve the lightest bin by relocating its items; repeat until stable."""
    changed = True
    while changed:
        changed = False
        bins.sort(key=lambda b: b["load"])
        for src in list(bins):
            targets = [b for b in bins if b is not src]
            tl = [b["load"] for b in targets]
            tc = [len(b["items"]) for b in targets]
            moves, ok = [], True
            for it in src["items"]:
                done = False
                for j, t in enumerate(targets):
                    if tl[j] + it["weight"] <= cap + 1e-6 and (not max_items or tc[j] < max_items):
                        tl[j] += it["weight"]; tc[j] += 1; moves.append((it, t)); done = True; break
                if not done:
                    ok = False; break
            if ok and src["items"]:
                for it, t in moves:
                    t["items"].append(it); t["load"] += it["weight"]
                bins.remove(src); changed = True; break
    return bins


def _heuristic(items, cap, max_items, keep, restarts=800):
    cands = []
    if keep:
        order = sorted(items, key=lambda it: (it["_grank"], -it["weight"]))
        cands.append(_best_fit(order, cap, max_items, True))
        cands.append(_first_fit(order, cap, max_items))
    else:
        cands.append(_first_fit(sorted(items, key=lambda it: -it["weight"]), cap, max_items))
        cands.append(_first_fit(sorted(items, key=lambda it: it["weight"]), cap, max_items))
        cands.append(_best_fit(sorted(items, key=lambda it: -it["weight"]), cap, max_items, False))
        rng = random.Random(987654321)
        n = restarts if len(items) <= 200 else 150
        for _ in range(n):
            arr = items[:]; rng.shuffle(arr)
            cands.append(_first_fit(arr, cap, max_items))
    best = None
    for sol in cands:
        sol = _improve([{"items": b["items"][:], "load": b["load"]} for b in sol], cap, max_items)
        key = (len(sol), max(b["load"] for b in sol))
        if best is None or key < best[0]:
            best = (key, sol)
    return best[1]


# ----------------------------------------------------------------------
# exact engine (OR-tools CP-SAT)
# ----------------------------------------------------------------------
def _exact(items, cap, max_items, upper_bound, time_limit):
    n = len(items)
    B = upper_bound
    w = [it["weight"] for it in items]
    # scale to ints (kg with 0 decimals is already fine; guard fractional)
    model = cp_model.CpModel()
    x = {(i, b): model.NewBoolVar(f"x{i}_{b}") for i in range(n) for b in range(B)}
    y = [model.NewBoolVar(f"y{b}") for b in range(B)]
    for i in range(n):
        model.Add(sum(x[i, b] for b in range(B)) == 1)
    for b in range(B):
        model.Add(sum(int(round(w[i])) * x[i, b] for i in range(n)) <= int(round(cap)) * y[b])
        if max_items:
            model.Add(sum(x[i, b] for i in range(n)) <= max_items * y[b])
        if b + 1 < B:
            model.Add(y[b] >= y[b + 1])          # symmetry break
    model.Minimize(sum(y))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = 8
    st = solver.Solve(model)
    if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        bins = [{"items": [], "load": 0.0} for _ in range(B)]
        for i in range(n):
            for b in range(B):
                if solver.Value(x[i, b]):
                    bins[b]["items"].append(items[i]); bins[b]["load"] += w[i]; break
        bins = [b for b in bins if b["items"]]
        return bins, ("exact-optimal" if st == cp_model.OPTIMAL else "exact-feasible")
    return None, None


# ----------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------
def optimize(items, capacity, max_items_per_bin=None, keep_groups=False,
             safety_margin=0.0, margin_is_pct=False, time_limit=20, force=None):
    items = [dict(it) for it in items]
    for k, it in enumerate(items):
        it.setdefault("label", "item")
        it.setdefault("group", it["label"])
    # stable group rank for keep-together ordering
    ranks, r = {}, 0
    for it in items:
        if it["group"] not in ranks:
            ranks[it["group"]] = r; r += 1
    for it in items:
        it["_grank"] = ranks[it["group"]]

    cap = capacity - (capacity * safety_margin / 100.0 if margin_is_pct else safety_margin)
    total = sum(it["weight"] for it in items)

    result = {"bins": [], "engine": None, "capacity_used": cap,
              "total_weight": total, "n_items": len(items), "infeasible_item": None}
    if not items:
        return result
    if cap <= 0:
        result["engine"] = "error-margin"; return result
    heaviest = max(items, key=lambda it: it["weight"])
    if heaviest["weight"] > cap + 1e-6:
        result["infeasible_item"] = heaviest; result["engine"] = "infeasible"; return result

    # heuristic first (also the warm upper bound for the exact model)
    heur = _heuristic(items, cap, max_items_per_bin, keep_groups)
    ub = len(heur)

    # lower bound on bins: by weight, and (if set) by max-items-per-bin.
    import math
    lb = math.ceil(total / cap - 1e-9)
    if max_items_per_bin:
        lb = max(lb, math.ceil(len(items) / max_items_per_bin - 1e-9))

    # If the heuristic already meets the lower bound, it is PROVABLY optimal.
    # No solver needed -> instant answer (this is the common case).
    if ub <= lb:
        result["bins"] = [b["items"] for b in heur]
        result["engine"] = "exact-optimal"
        return result

    use_exact = force == "exact" or (
        force != "heuristic" and _HAS_ORTOOLS and not keep_groups
        and len(items) * ub <= 40000        # keep the model small -> low RAM/time
    )
    if use_exact:
        # There's a gap the heuristic couldn't close. Give the exact solver a
        # bounded budget to try to beat it; if it can't in time, return the
        # heuristic answer (already strong) rather than making the user wait.
        bins, engine = _exact(items, cap, max_items_per_bin, ub, time_limit)
        if bins is not None and len(bins) < ub:
            result["bins"] = [b["items"] for b in bins]; result["engine"] = engine
            return result
        if bins is not None and engine == "exact-optimal":
            # solver proved the heuristic count is optimal
            result["bins"] = [b["items"] for b in bins]; result["engine"] = "exact-optimal"
            return result

    result["bins"] = [b["items"] for b in heur]
    result["engine"] = "heuristic" if not use_exact else "best-found"
    return result


if __name__ == "__main__":
    # self-test on the drum shipment
    spec = [(1656,6),(2347,3),(2510,6),(3469,12),(3550,6),(4038,3),
            (4484,6),(4565,6),(5134,12),(5728,21)]
    items = []
    for w, q in spec:
        for _ in range(q):
            items.append({"weight": w, "label": f"{w}kg", "group": f"{w}kg"})
    for cap in (21500, 21772):
        res = optimize(items, cap)
        loads = [sum(i["weight"] for i in b) for b in res["bins"]]
        print(f"cap {cap}: {len(res['bins'])} bins  engine={res['engine']}  "
              f"max={max(loads):.0f}  all_ok={all(l<=cap+1e-6 for l in loads)}  "
              f"items={sum(len(b) for b in res['bins'])}")

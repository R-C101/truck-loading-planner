"""
solver_core.py  —  general "pack items into bins by weight" optimiser.

Designed for loading problems (drums onto trucks) but generic: any items with a
weight, packed into bins with a capacity, minimising the number of bins.

Three engines:
  * EXACT (pattern) — OR-tools CP-SAT over legal *truck patterns* rather than
             individual drums. Shipments have few distinct weights but many
             drums each, so this collapses identical drums into one count and
             the proof is usually instant. Preferred exact engine.
  * EXACT (per-item) — the classic one-variable-per-drum CP-SAT model. Only a
             fallback, for inputs with too many distinct weights to enumerate
             patterns. Slow on shipments with many identical drums (symmetry).
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

    optimize_by_bl(items, capacity, ...same options...)
      items additionally carry "bl" (bill of lading number, "" if none).
      Fewest trucks first — exactly as optimize() — and then, among plans with
      that many trucks, the fewest trucks that carry more than one BL. Trucks
      come back ordered: each BL's own trucks in BL order, then the shared ones.
      Adds "bin_bl" (the BL of each truck, None when shared), "mixed" (how many
      are shared) and "mix_engine" (whether that mixing count is proven minimal).
"""
from __future__ import annotations
import math
import random
import re

try:
    from ortools.sat.python import cp_model
    from ortools.linear_solver import pywraplp
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
# exact engine A — pattern / column model (OR-tools CP-SAT)
#
# Real shipments have few DISTINCT drum weights but many drums of each
# (e.g. 133 drums, 7 weights, 63 of them identical). Modelling one variable
# per drum makes every re-labelling of identical drums a separate solution,
# so the proof drowns in symmetry. Instead enumerate every legal *truck
# pattern* ("2 x 8065", "2 x 6491 + 1 x 8065", ...) and just decide how many
# trucks of each pattern to run. Identical drums collapse into one number,
# the symmetry disappears, and the proof is typically instant.
# ----------------------------------------------------------------------
class _TooManyPatterns(Exception):
    pass


def _gen_patterns(weights, counts, cap, max_items, limit=200_000):
    """Every multiset of drum weights that legally fills one bin.

    Returned as tuples of per-weight counts, aligned with `weights`.
    Returns None if the enumeration would be too big to be worth it (many
    distinct weights and/or tiny items relative to the cap).
    """
    d = len(weights)
    out, cur = [], [0] * d

    def dfs(i, load, n):
        if len(out) > limit:
            raise _TooManyPatterns
        if i == d:
            if n:                                   # skip the empty truck
                out.append(tuple(cur))
            return
        w = weights[i]
        k_max = counts[i]
        if max_items:
            k_max = min(k_max, max_items - n)
        while k_max > 0 and load + k_max * w > cap + 1e-6:
            k_max -= 1                              # capacity prune
        for k in range(k_max + 1):
            cur[i] = k
            dfs(i + 1, load + k * w, n + k)
        cur[i] = 0

    try:
        dfs(0, 0.0, 0)
    except _TooManyPatterns:
        return None
    return out


def _lp_ceil(value):
    """Round an LP optimum up to the integer bound it proves. The slack keeps a
    floating-point 17.0000001 from being read as 18 — a bound that is one too
    low only costs a proof, one too high would be a wrong answer."""
    return math.ceil(value - 1e-4)


def _weight_counts(items):
    counter = {}
    for it in items:
        counter[it["weight"]] = counter.get(it["weight"], 0) + 1
    weights = sorted(counter)
    return weights, [counter[w] for w in weights]


def _pattern_lp_bound(items, cap, max_items):
    """Lower bound on trucks from the LP relaxation of the pattern model.

    CP-SAT finds good plans quickly but, when every drum has its own weight,
    it can spend the whole time limit failing to prove there isn't one truck
    fewer. The LP bound of this model is famously tight (it is almost always
    the true answer, rounded up) and costs milliseconds, so it settles most of
    those cases outright. None if the patterns can't be enumerated.
    """
    weights, counts = _weight_counts(items)
    pats = _gen_patterns(weights, counts, cap, max_items)
    if not pats:
        return None
    lp = pywraplp.Solver.CreateSolver("GLOP")
    x = [lp.NumVar(0, lp.infinity(), "") for _ in pats]
    for j in range(len(weights)):
        lp.Add(sum(p[j] * x[i] for i, p in enumerate(pats) if p[j]) == counts[j])
    lp.Minimize(sum(x))
    if lp.Solve() != pywraplp.Solver.OPTIMAL:
        return None
    return _lp_ceil(lp.Objective().Value())


def _exact_patterns(items, cap, max_items, upper_bound, time_limit, lower_bound=0):
    """Prove the minimum number of bins via the pattern model."""
    weights, counts = _weight_counts(items)

    pats = _gen_patterns(weights, counts, cap, max_items)
    if not pats:
        return None, None

    model = cp_model.CpModel()
    # x[p] = how many trucks are loaded with pattern p
    x = [model.NewIntVar(0, upper_bound, f"p{i}") for i in range(len(pats))]
    for j in range(len(weights)):                   # ship exactly what we have
        model.Add(sum(pats[i][j] * x[i] for i in range(len(pats)) if pats[i][j])
                  == counts[j])
    model.Add(sum(x) <= upper_bound)                # never worse than the heuristic
    model.Add(sum(x) >= lower_bound)                # known bound: lets it stop early
    model.Minimize(sum(x))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    # NOTE: single worker on purpose. Multi-worker CP-SAT is non-deterministic
    # (breaks the "same input -> same plan" guarantee) and, on this ortools/
    # Python build, it also ignores max_time_in_seconds and runs forever.
    solver.parameters.num_search_workers = 1
    st = solver.Solve(model)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None

    # hand the actual item dicts back out, grouped by weight
    pool = {w: [it for it in items if it["weight"] == w] for w in weights}
    bins = []
    for i, p in enumerate(pats):
        for _ in range(solver.Value(x[i])):
            b = {"items": [], "load": 0.0}
            for j, w in enumerate(weights):
                for _ in range(p[j]):
                    it = pool[w].pop()
                    b["items"].append(it); b["load"] += it["weight"]
            bins.append(b)
    return bins, ("exact-optimal" if st == cp_model.OPTIMAL else "exact-feasible")


# ----------------------------------------------------------------------
# exact engine B — one variable per item (fallback for many distinct weights)
# ----------------------------------------------------------------------
def _exact(items, cap, max_items, upper_bound, time_limit, lower_bound=0):
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
    model.Add(sum(y) >= lower_bound)
    model.Minimize(sum(y))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    # NOTE: single worker on purpose. Multi-worker CP-SAT is non-deterministic
    # (breaks the "same input -> same plan" guarantee) and, on this ortools/
    # Python build, it also ignores max_time_in_seconds and runs forever.
    solver.parameters.num_search_workers = 1
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
    )
    # Still a gap: try the (much stronger) LP bound before any search.
    if use_exact and _HAS_ORTOOLS:
        lp = _pattern_lp_bound(items, cap, max_items_per_bin)
        if lp is not None:
            lb = max(lb, lp)
        if ub <= lb:
            result["bins"] = [b["items"] for b in heur]
            result["engine"] = "exact-optimal"
            return result
    if use_exact and _HAS_ORTOOLS:
        # There's a gap the heuristic couldn't close. Give the exact solver a
        # bounded budget to try to beat it; if it can't in time, return the
        # heuristic answer (already strong) rather than making the user wait.
        #
        # Prefer the pattern model — identical drums collapse into a single
        # count, so the symmetry that stalls the per-item model disappears.
        # It returns None when there are too many distinct weights to
        # enumerate; only then fall back to the per-item model, and only if
        # that one is small enough to stay fast and in-memory.
        bins, engine = _exact_patterns(items, cap, max_items_per_bin, ub, time_limit, lb)
        if bins is None and len(items) * ub <= 40000:
            bins, engine = _exact(items, cap, max_items_per_bin, ub, time_limit, lb)
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


# ----------------------------------------------------------------------
# loading by bill of lading
#
# A shipment can cover several BLs, and there are three ways to treat them:
#   full     — fewest trucks, full stop; among plans that size, as few trucks
#              shared between BLs as possible.
#   half     — the dad's usual rule, below.
#   separate — every BL strictly on its own trucks, nothing shared.
# The half rule: every BL is loaded on its
# own trucks first. Loaded alone, a BL needs some number of trucks n, and only
# the last of them is part-filled — those part-filled "half trucks" are what
# may be combined across BLs. So each BL keeps at least n - 1 trucks to itself,
# and within that rule the plan uses as few trucks as possible, then as few
# shared trucks as possible. That can be a truck more than loading everything
# together would need; the result reports that number too.
# ----------------------------------------------------------------------
def _bl_key(bl):
    """Natural order (BL2 before BL10); drums with no BL go last."""
    parts = re.split(r"(\d+)", bl)
    return (bl == "", [int(t) if t.isdigit() else t.lower() for t in parts])


def _bl_alone(items, cap, max_items, time_limit, force):
    """Each BL packed on its own: ({bl: trucks}, every count proven)."""
    by_bl = {}
    for it in items:
        by_bl.setdefault(it["bl"], []).append(it)
    per = max(2.0, time_limit / max(1, len(by_bl)))
    out, proven = {}, True
    for b in sorted(by_bl, key=_bl_key):
        r = optimize(by_bl[b], cap, max_items_per_bin=max_items,
                     time_limit=per, force=force)
        proven &= r["engine"] == "exact-optimal"
        out[b] = r["bins"]
    return out, proven


def _bl_split_plan(items, cap, max_items, time_limit, force):
    """Dad's method done directly: load each BL alone, then pool every BL's
    emptiest truck and re-pack the pool. Always valid for the rule, so it is
    both the warm start for the exact model and the fallback.

    Returns (bins, own_min, proven) — own_min[bl] is n - 1, and proven says
    every n was proved minimal (otherwise own_min may be one high)."""
    alone, proven = _bl_alone(items, cap, max_items, time_limit, force)
    bls = list(alone)
    per = max(2.0, time_limit / max(1, len(bls)))
    kept, pool, own_min = [], [], {}
    for b in bls:
        trucks = sorted(alone[b], key=lambda t: (sum(i["weight"] for i in t), len(t)))
        own_min[b] = len(trucks) - 1
        pool.extend(trucks[0])
        kept.extend(trucks[1:])
    again = optimize(pool, cap, max_items_per_bin=max_items,
                     time_limit=per, force=force)["bins"]
    # the pool was one truck per BL; a re-pack that saves nothing only mixes
    if len(again) >= len(bls):
        again = [[i for i in pool if i["bl"] == b] for b in bls]
    return kept + again, own_min, proven


def _bl_patterns(items, cap, max_items, own_min, hint_bins, time_limit,
                 total_proven=False):
    """Exact: under the BL rule, fewest trucks, then fewest shared trucks.

    Own trucks are patterns over a single BL's drums, and BL b must run at
    least own_min[b] of them. Shared trucks are patterns over drum weights
    only, filled from a pool that any BL can pay into — which BL fills which
    slot doesn't change what fits, so it is decided afterwards. At the stage-2
    optimum no shared truck holds a single BL (it would be an own truck and
    the objective would be lower), so the shared count is real.

    total_proven: the hint's truck count is already known to be the fewest,
    so stage 1 is skipped. Returns (bins, trucks_proof, shared_proof) or None.
    """
    by = {}
    for it in items:
        by.setdefault((it["bl"], it["weight"]), []).append(it)
    bls = sorted({b for b, _ in by}, key=_bl_key)
    weights = sorted({w for _, w in by})
    totals = [sum(len(v) for (_, w), v in by.items() if w == ww) for ww in weights]

    shared = _gen_patterns(weights, totals, cap, max_items)
    if not shared:
        return None
    own = {}
    for b in bls:
        ws = [w for w in weights if (b, w) in by]
        pats = _gen_patterns(ws, [len(by[b, w]) for w in ws], cap, max_items)
        if not pats:
            return None
        own[b] = (ws, pats)
    keys = sorted(by, key=lambda k: (_bl_key(k[0]), k[1]))
    ub = len(hint_bins)

    def build(mk, var):
        """The same constraints for CP-SAT (mk=model) and its LP relaxation."""
        x = {(b, k): var(ub) for b in bls for k in range(len(own[b][1]))}
        y = [var(ub) for _ in shared]
        z = {key: var(len(by[key])) for key in keys}    # drums sent to sharing
        for (b, w), lst in by.items():
            ws, pats = own[b]
            j = ws.index(w)
            mk(sum(p[j] * x[b, k] for k, p in enumerate(pats) if p[j])
               + z[b, w] == len(lst))
        for j, w in enumerate(weights):
            mk(sum(q[j] * y[i] for i, q in enumerate(shared) if q[j])
               == sum(z[b, w] for b in bls if (b, w) in z))
        for b in bls:
            if own_min[b] > 0:
                mk(sum(x[b, k] for k in range(len(own[b][1]))) >= own_min[b])
        return x, y, z

    def lp_floor(stage, cap_total):
        lp = pywraplp.Solver.CreateSolver("GLOP")
        x, y, z = build(lp.Add, lambda hi: lp.NumVar(0, hi, ""))
        if cap_total is not None:
            lp.Add(sum(x.values()) + sum(y) <= cap_total)
        lp.Minimize(sum(x.values()) + sum(y) if stage == 1 else sum(y))
        if lp.Solve() != pywraplp.Solver.OPTIMAL:
            return 0
        return max(0, _lp_ceil(lp.Objective().Value()))

    # Warm start: the split plan, each truck filed under what it really
    # carries — one BL -> that BL's own pattern, several -> a shared pattern.
    shared_at = {q: i for i, q in enumerate(shared)}
    own_at = {b: {p: k for k, p in enumerate(own[b][1])} for b in bls}
    hx = {(b, k): 0 for b in bls for k in range(len(own[b][1]))}
    hy, hz = [0] * len(shared), {k: 0 for k in keys}
    for bin_items in hint_bins:
        tags = {it["bl"] for it in bin_items}
        if len(tags) == 1:
            b = next(iter(tags))
            ws = own[b][0]
            cnt = [0] * len(ws)
            for it in bin_items:
                cnt[ws.index(it["weight"])] += 1
            hx[b, own_at[b][tuple(cnt)]] += 1
        else:
            cnt = [0] * len(weights)
            for it in bin_items:
                cnt[weights.index(it["weight"])] += 1
                hz[it["bl"], it["weight"]] += 1
            hy[shared_at[tuple(cnt)]] += 1

    def solve(stage, cap_total, floor, hint, budget):
        model = cp_model.CpModel()
        x, y, z = build(model.Add, lambda hi: model.NewIntVar(0, hi, ""))
        total = sum(x.values()) + sum(y)
        if cap_total is not None:
            model.Add(total <= cap_total)
        obj = total if stage == 1 else sum(y)
        model.Add(obj >= floor)                 # LP floor: lets it stop early
        model.Minimize(obj)
        hx_, hy_, hz_ = hint
        for k, v in x.items():
            model.AddHint(v, hx_[k])
        for i, v in enumerate(y):
            model.AddHint(v, hy_[i])
        for k, v in z.items():
            model.AddHint(v, hz_[k])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(budget)
        # NOTE: single worker on purpose — see _exact_patterns.
        solver.parameters.num_search_workers = 1
        st = solver.Solve(model)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        proven = st == cp_model.OPTIMAL or solver.ObjectiveValue() <= floor
        vals = ({k: solver.Value(v) for k, v in x.items()},
                [solver.Value(v) for v in y],
                {k: solver.Value(v) for k, v in z.items()})
        return vals, proven

    # stage 1: fewest trucks under the rule
    hint = (hx, hy, hz)
    n_trucks = sum(hx.values()) + sum(hy)
    floor = 0 if total_proven else lp_floor(1, None)
    proof1 = total_proven or n_trucks <= floor
    if not proof1:
        got = solve(1, None, floor, hint, time_limit * 0.6)
        if got is None:
            return None
        hint, proof1 = got
        n_trucks = sum(hint[0].values()) + sum(hint[1])

    # stage 2: keep that count, fewest shared trucks
    floor = lp_floor(2, n_trucks)
    proof2 = sum(hint[1]) <= floor
    if not proof2:
        got = solve(2, n_trucks, floor, hint, time_limit * 0.4)
        if got is not None:
            hint, proof2 = got
    if hint[0] is hx and hint[1] is hy:
        return [list(b) for b in hint_bins], proof1, proof2
    vx, vy, _ = hint

    # hand out the real drums, in their original order
    queue = {key: list(reversed(v)) for key, v in by.items()}
    bins = []
    for b in bls:
        ws, pats = own[b]
        for k, pat in enumerate(pats):
            for _ in range(vx[b, k]):
                bins.append([queue[b, w].pop() for j, w in enumerate(ws)
                             for _ in range(pat[j])])
    # what is left goes onto the shared trucks; take it BL by BL so each shared
    # truck spans as few BLs as possible
    pool = {w: [] for w in weights}
    for b in bls:
        for w in weights:
            if (b, w) in queue:
                pool[w].extend(reversed(queue[b, w]))
    for w in pool:
        pool[w].reverse()
    for i, q in sorted(enumerate(shared), key=lambda iq: iq[1], reverse=True):
        for _ in range(vy[i]):
            bins.append([pool[w].pop() for j, w in enumerate(weights)
                         for _ in range(q[j])])
    return bins, proof1, proof2


def optimize_by_bl(items, capacity, max_items_per_bin=None, safety_margin=0.0,
                   margin_is_pct=False, time_limit=20, force=None, mode="half"):
    """Load BL by BL; mode is "full", "half" or "separate" (see above). Same
    result dict as optimize(), plus
    bin_bl (each truck's BL, None if shared), mixed (shared truck count),
    mix_engine (proof of that count) and free_trucks / free_engine (what
    loading everything together, ignoring BLs, would need)."""
    items = [dict(it) for it in items]
    for k, it in enumerate(items):
        it["bl"] = str(it.get("bl") or "").strip()
        it["_idx"] = k              # survives optimize()'s copying, unlike id()

    base = optimize(items, capacity, max_items_per_bin=max_items_per_bin,
                    safety_margin=safety_margin, margin_is_pct=margin_is_pct,
                    time_limit=time_limit, force=force)
    base.update(bin_bl=[], mixed=0, mix_engine=None,
                free_trucks=len(base["bins"]), free_engine=base["engine"])
    if not base["bins"]:
        return base
    cap = base["capacity_used"]

    if len({it["bl"] for it in items}) <= 1:
        bins, engine, mix_engine = base["bins"], base["engine"], "exact-optimal"
    elif mode == "separate":
        alone, n_proven = _bl_alone(items, cap, max_items_per_bin, time_limit, force)
        bins = [t for trucks in alone.values() for t in trucks]
        engine = "exact-optimal" if n_proven else "heuristic"
        mix_engine = "exact-optimal"
    else:
        if mode == "full":
            fallback = base["bins"]
            own_min = {it["bl"]: 0 for it in items}
            engine = base["engine"]
            n_proven = engine == "exact-optimal"
        else:
            fallback, own_min, n_proven = _bl_split_plan(
                items, cap, max_items_per_bin, time_limit, force)
            engine = "heuristic"
        bins, mix_engine = fallback, "heuristic"
        if force != "heuristic" and _HAS_ORTOOLS:
            got = _bl_patterns(items, cap, max_items_per_bin, own_min, fallback,
                               time_limit, total_proven=(mode == "full"))
            if got:
                bins, p1, p2 = got
                if mode != "full":
                    engine = "exact-optimal" if p1 and n_proven else "exact-feasible"
                mix_engine = "exact-optimal" if p2 else "exact-feasible"
        # the guarantee: every drum exactly once, no truck over the cap, never
        # more trucks than the fallback plan. Anything else -> the fallback.
        ok = (sorted(it["_idx"] for b in bins for it in b) == list(range(len(items)))
              and all(sum(it["weight"] for it in b) <= cap + 1e-6 for b in bins)
              and (not max_items_per_bin or all(len(b) <= max_items_per_bin for b in bins))
              and len(bins) <= len(fallback))
        if not ok:
            bins, mix_engine = fallback, "heuristic"
            if mode != "full":
                engine = "heuristic"

    def truck_bl(b):
        s = {it["bl"] for it in b}
        return next(iter(s)) if len(s) == 1 else None

    def load(b):
        return sum(it["weight"] for it in b)

    own = [b for b in bins if truck_bl(b) is not None]
    shared = [b for b in bins if truck_bl(b) is None]
    own.sort(key=lambda b: (_bl_key(truck_bl(b)), -load(b)))
    shared.sort(key=lambda b: (min(_bl_key(it["bl"]) for it in b), -load(b)))
    for b in shared:
        b.sort(key=lambda it: (_bl_key(it["bl"]), -it["weight"]))

    base["bins"] = own + shared
    base["bin_bl"] = [truck_bl(b) for b in base["bins"]]
    base["mixed"] = len(shared)
    base["engine"] = engine
    base["mix_engine"] = mix_engine
    return base


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

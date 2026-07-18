#!/usr/bin/env python3
"""
Drum -> Truck loading optimiser.

Goal: load whole drums onto trucks so that no truck exceeds its weight cap,
using the FEWEST trucks possible. Weight is the only constraint (bed space
assumed sufficient). Drums may be freely mixed across sizes/containers.

HOW TO REUSE NEXT TIME
----------------------
1. Edit the DRUMS list below: each entry is (label, weight_kg, count).
2. Set TARGET_CAP_KG (comfortable fill) and HARD_CAP_KG (never exceed).
3. Run:  python3 drum_truck_planner.py
The script proves the minimum number of trucks (exact solver) and prints
a per-truck loading plan. Fully deterministic / repeatable.
"""

from ortools.sat.python import cp_model

# ----------------------------------------------------------------------
# INPUT DATA  (label, gross weight per drum in kg, how many such drums)
# ----------------------------------------------------------------------
DRUMS = [
    ("72x40x36  @1656kg", 1656,  6),
    ("72x40x36  @2347kg", 2347,  3),
    ("72x40x36  @2510kg", 2510,  6),
    ("78x40x45  @3469kg", 3469, 12),
    ("78x40x45  @3550kg", 3550,  6),
    ("78x40x45  @4038kg", 4038,  3),
    ("88x40x40  @4484kg", 4484,  6),
    ("88x40x40  @4565kg", 4565,  6),
    ("88x40x40  @5134kg", 5134, 12),
    ("88x40x45  @5728kg", 5728, 21),
]

TARGET_CAP_KG = 21500   # comfortable target (the 21.5 t you gave)
HARD_CAP_KG   = 21772   # true ceiling = 48,000 lb  (48000 * 0.45359237)

# ----------------------------------------------------------------------
def expand(drums):
    items = []  # (weight, label)
    for label, w, n in drums:
        items.extend([(w, label)] * n)
    return items


def ffd_upper_bound(weights, cap):
    """First-Fit-Decreasing: quick feasible solution -> upper bound on trucks."""
    bins = []  # list of remaining capacity
    order = sorted(range(len(weights)), key=lambda i: -weights[i])
    assign = [None] * len(weights)
    for i in order:
        placed = False
        for b in range(len(bins)):
            if bins[b] >= weights[i]:
                bins[b] -= weights[i]
                assign[i] = b
                placed = True
                break
        if not placed:
            bins.append(cap - weights[i])
            assign[i] = len(bins) - 1
    return len(bins), assign


def solve_min_trucks(weights, cap, time_limit=30):
    """Exact min-bin-packing via CP-SAT. Returns (num_trucks, assignment)."""
    n = len(weights)
    ub, ffd_assign = ffd_upper_bound(weights, cap)
    lb = -(-sum(weights) // cap)  # ceil
    if ub == lb:
        return ub, ffd_assign  # FFD already optimal, skip solver

    max_bins = ub
    model = cp_model.CpModel()
    x = {(i, b): model.NewBoolVar(f"x_{i}_{b}")
         for i in range(n) for b in range(max_bins)}
    y = [model.NewBoolVar(f"y_{b}") for b in range(max_bins)]

    for i in range(n):
        model.Add(sum(x[i, b] for b in range(max_bins)) == 1)
    for b in range(max_bins):
        model.Add(sum(weights[i] * x[i, b] for i in range(n)) <= cap * y[b])
        if b + 1 < max_bins:                    # symmetry break: fill low bins first
            model.Add(y[b] >= y[b + 1])
    model.Minimize(sum(y))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assign = [next(b for b in range(max_bins) if solver.Value(x[i, b]))
                  for i in range(n)]
        used = sorted(set(assign))
        remap = {b: k for k, b in enumerate(used)}
        assign = [remap[b] for b in assign]
        proven = "PROVEN OPTIMAL" if status == cp_model.OPTIMAL else "best found"
        return len(used), assign, proven
    return ub, ffd_assign, "FFD fallback"


def report(weights, labels, assign, cap, target, tag):
    ntr = max(assign) + 1
    print(f"\n{'='*60}\nCap used: {cap} kg   ({tag})   ->  {ntr} trucks\n{'='*60}")
    for b in range(ntr):
        idx = [i for i in range(len(weights)) if assign[i] == b]
        load = sum(weights[i] for i in idx)
        from collections import Counter
        c = Counter(labels[i] for i in idx)
        parts = ", ".join(f"{n}x {lab}" for lab, n in c.items())
        flag = "" if load <= target else "  (>target, within hard cap)"
        print(f"Truck {b+1:2d}: {load:6d} kg | {len(idx):2d} drums | {parts}{flag}")
    print(f"\nTotal drums: {len(weights)}   Total weight: {sum(weights)} kg")
    print(f"Trucks: {ntr}   Avg load: {sum(weights)//ntr} kg")


if __name__ == "__main__":
    items = expand(DRUMS)
    weights = [w for w, _ in items]
    labels  = [l for _, l in items]

    print("Total drums :", len(weights))
    print("Total weight:", sum(weights), "kg")
    print("Weight lower bound @target:", -(-sum(weights)//TARGET_CAP_KG), "trucks")
    print("Weight lower bound @hard  :", -(-sum(weights)//HARD_CAP_KG), "trucks")

    for cap, tag in [(TARGET_CAP_KG, "target 21.5t"),
                     (HARD_CAP_KG,   "hard 48000lb")]:
        res = solve_min_trucks(weights, cap)
        assign = res[1]
        proven = res[2] if len(res) > 2 else "FFD"
        report(weights, labels, assign, cap, TARGET_CAP_KG, proven)

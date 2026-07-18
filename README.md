# Truck Loading Planner

Works out the **fewest trucks** needed to ship a set of drums, given a weight
limit per truck. You enter each drum type — its weight and how many — set the
truck's weight limit, and it returns the smallest number of trucks along with
exactly which drums go on each one, with no truck over the limit.

Built for loading steel drums onto 21.5 t / 48,000 lb trucks, where cutting even
one truck from a shipment is real money saved.

## What it does

- Packs every drum onto a truck so no truck exceeds its weight limit, using the
  fewest trucks possible.
- Finds a strong plan almost instantly, and can optionally **prove** it's the
  fewest trucks mathematically possible when a load is tight.
- Gives a clear per-truck breakdown — weight, drum count, and how full each
  truck is.
- Same input always produces the same plan.

## Options

- Weight limit per truck, in kg, lb, or tonnes.
- Safety margin — load only up to a set amount below the limit (kg or %).
- Maximum drums per truck, when bed space rather than weight is the limit.
- Keep drum types together on the same truck where possible.

---

© Proprietary. All rights reserved. Not licensed for use, copying, or
distribution.

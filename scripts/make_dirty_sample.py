"""Make a dirty sample from a clean CSV by injecting known faults.

Usage:
    python scripts/make_dirty_sample.py clean.csv dirty.csv --rate 0.2 --seed 7

Faults (each injected independently with probability --rate unless a specific
rate flag overrides it):
  --null-rate    blank a random column
  --negative-rate negate numeric-looking values
  --type-rate    replace a value with 'abc'
  --dup-rate     duplicate a previous row
"""
import argparse
import csv
import random
import sys


def main() -> int:
    p = argparse.ArgumentParser(description="Inject known faults into a clean CSV.")
    p.add_argument("src", help="clean input CSV")
    p.add_argument("dst", help="dirty output CSV")
    p.add_argument("--rate", type=float, default=0.15, help="base fault probability per row")
    p.add_argument("--null-rate", type=float, default=None)
    p.add_argument("--negative-rate", type=float, default=None)
    p.add_argument("--type-rate", type=float, default=None)
    p.add_argument("--dup-rate", type=float, default=None)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    rng = random.Random(args.seed)

    def rate(specific):
        return args.rate if specific is None else specific

    null_r, neg_r, type_r, dup_r = (
        rate(args.null_rate),
        rate(args.negative_rate),
        rate(args.type_rate),
        rate(args.dup_rate),
    )
    with open(args.src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        if not rows:
            print("empty input", file=sys.stderr)
            return 1
        fields = list(rows[0].keys())
    out, seen = [], []
    for row in rows:
        r = rng.random()
        row = dict(row)
        if r < dup_r and seen:
            out.append(dict(rng.choice(seen)))
        elif r < dup_r + null_r:
            row[rng.choice(fields)] = ""
            out.append(row)
        elif r < dup_r + null_r + neg_r:
            for k, v in row.items():
                try:
                    row[k] = str(-abs(float(v)))
                    break
                except (TypeError, ValueError):
                    continue
            out.append(row)
        elif r < dup_r + null_r + neg_r + type_r:
            row[rng.choice(fields)] = "abc"
            out.append(row)
        else:
            out.append(row)
        seen.append(row)
    with open(args.dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    print(f"wrote {len(out)} rows to {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Do two runs give the same results? Compares two `groundtruth simulate --format json` reports number by number
(development aid for speed work: a faster solver must not change the answers beyond the GMRES tolerance).

    .venv/Scripts/python tools/dev/result_diff.py A/groundtruth-report.json B/groundtruth-report.json [--top 25]

Walks both JSON trees in step and prints the largest differences (absolute for dB values, relative otherwise) with
their paths, and anything present in only one of them. Lists are matched by position, routes and power stages by
name. Timing fields (seconds) are left out.
"""
import argparse
import json
import math
import sys

SKIP = ("seconds", "time_s", "elapsed", "generated", "duration")


def key_of(item):
    """What identifies a list item between the two reports: its net, name or id (with the receiver for a route),
    None when it has none."""
    if isinstance(item, dict):
        for k in ("net", "name", "id"):
            if k in item and isinstance(item[k], str):
                return item[k] + (f" {item['receiver']}" if isinstance(item.get("receiver"), str) else "")
    return None


def walk(a, b, path, out, missing):
    """Compare a and b recursively: dicts key by key, lists by item key when every item has a unique one (else by
    position), numbers as a dB or relative difference into `out`; keys or items present in one report only go to
    `missing`. Keys in SKIP (timings and the like) are ignored."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if any(s in k for s in SKIP):
                continue
            if k not in a or k not in b:
                missing.append(f"{path}/{k}: only in {'B' if k not in a else 'A'}")
                continue
            walk(a[k], b[k], f"{path}/{k}", out, missing)
    elif isinstance(a, list) and isinstance(b, list):
        ka, kb = [key_of(x) for x in a], [key_of(x) for x in b]
        if all(ka) and all(kb) and len(set(ka)) == len(ka) and len(set(kb)) == len(kb):
            da, db = dict(zip(ka, a)), dict(zip(kb, b))
            for k in sorted(set(da) | set(db)):
                if k not in da or k not in db:
                    missing.append(f"{path}[{k}]: only in {'B' if k not in da else 'A'}")
                else:
                    walk(da[k], db[k], f"{path}[{k}]", out, missing)
        else:
            if len(a) != len(b):
                missing.append(f"{path}: {len(a)} vs {len(b)} items")
            for k, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{k}]", out, missing)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if math.isnan(a) and math.isnan(b):
            return
        if "db" in path.lower():
            out.append((abs(a - b), "dB", path, a, b))
        else:
            scale = max(abs(a), abs(b))
            out.append((abs(a - b) / scale if scale > 0 else 0.0, "rel", path, a, b))
    elif a != b:
        missing.append(f"{path}: {a!r} vs {b!r}")


def main() -> int:
    """The command line: report A, report B; prints the largest dB and relative differences (--top of each) and what
    only one report has."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    with open(args.a, encoding="utf-8") as fa, open(args.b, encoding="utf-8") as fb:
        A, B = json.load(fa), json.load(fb)
    out, missing = [], []
    walk(A, B, "", out, missing)
    db = sorted((d for d in out if d[1] == "dB"), reverse=True)
    rel = sorted((d for d in out if d[1] == "rel"), reverse=True)
    print(f"{len(out)} numbers compared; largest dB difference {db[0][0] if db else 0:.4f} dB, largest relative "
          f"difference {rel[0][0] if rel else 0:.2e}")
    for title, rows in (("dB values", db), ("other values (relative)", rel)):
        print(f"\n{title}:")
        for d, kind, path, a, b in rows[: args.top]:
            print(f"  {d:10.4g} {kind:3s}  {path}: {a!r} vs {b!r}")
    if missing:
        print(f"\nonly in one, or different text ({len(missing)}):")
        for m in missing[: args.top * 2]:
            print("  " + m)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Compares stack_sampler.py profiles: the cost of each function in ms per game day.

A function's share of a thread's samples times the profile's ms per game day gives
its cost per day, so profiles that ran at different speeds compare directly. The
table lists the functions whose cost grew most from the first profile to the last.

    python compare_profiles.py runs/prof-fresh-1667.json runs/prof-degrade-4.json
"""
import argparse
import json
import pathlib


def costs(profile, thread, kind):
    """ms per game day for each function; "workers" adds up the unnamed threads, each
    of which ran the whole time, so their costs are summed (thread time, not wall time)."""
    if thread == "workers":
        total = {}
        for key in profile["threads"]:
            if key.startswith("tid"):
                for name, cost in costs(profile, key, kind).items():
                    total[name] = total.get(name, 0) + cost
        return total
    t = profile["threads"].get(thread)
    if not t or not t["samples"] or not profile["ms_per_day"]:
        return {}
    scale = profile["ms_per_day"] / t["samples"]
    return {name: count * scale for name, count in t[kind]}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("profiles", nargs="+")
    parser.add_argument("--thread", default="main")
    parser.add_argument("--kind", choices=["incl", "self"], default="incl")
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args(argv)
    profiles = [json.loads(pathlib.Path(p).read_text()) for p in args.profiles]
    names = [pathlib.Path(p).stem for p in args.profiles]

    print("profile".ljust(24) + "".join(n[-14:].rjust(15) for n in names))
    rows = [("dates", [f"{p['start_date']}" for p in profiles]),
            ("ms/day", [p["ms_per_day"] for p in profiles]),
            ("main cpu/run %", [round(100 * p["thread_cpu_s"].get("main", 0) / p["running_s"]) for p in profiles]),
            ("all cpu/run %", [round(100 * sum(p["thread_cpu_s"].values()) / p["running_s"]) for p in profiles]),
            ("private MB", [p["memory_after"]["committed_mb"].get("private") for p in profiles]),
            ("private allocs", [p["memory_after"]["private_allocations"] for p in profiles])]
    for label, values in rows:
        print(label.ljust(24) + "".join(str(v).rjust(15) for v in values))
    sizes = sorted({k for p in profiles for k in p["memory_after"]["private_by_size"]},
                   key=lambda k: int(k[2:-2]))
    for k in sizes:
        cells = []
        for p in profiles:
            c = p["memory_after"]["private_by_size"].get(k, {"count": 0, "mb": 0})
            cells.append(f"{c['count']}x {c['mb']:.0f}MB")
        if any(p["memory_after"]["private_by_size"].get(k, {}).get("mb", 0) > 20 for p in profiles):
            print(f"  {k}".ljust(24) + "".join(c.rjust(15) for c in cells))

    tables = [costs(p, args.thread, args.kind) for p in profiles]
    all_names = set().union(*tables)
    growth = sorted(all_names, key=lambda n: tables[-1].get(n, 0) - tables[0].get(n, 0), reverse=True)
    print(f"\n{args.thread} thread, {args.kind} ms per game day, by growth (last - first)")
    for n in growth[:args.top]:
        cells = "".join(f"{t.get(n, 0):15.2f}" for t in tables)
        print(f"{n[:24]:24}{cells}{tables[-1].get(n, 0) - tables[0].get(n, 0):+10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))

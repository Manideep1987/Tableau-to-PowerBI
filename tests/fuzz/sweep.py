"""Run the differential fuzzer for several seeds and save a JSON summary for the test report.

    python tests/fuzz/sweep.py --seeds 1 2 3 4 11 12 13 14 21 22 --cases 200
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from differential_fuzz import run  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 11, 12, 13, 14, 21, 22])
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "test-results", "fuzz_sweep.json"))
    a = ap.parse_args()
    out = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "cases_per_seed": a.cases, "seeds": []}
    kinds = {}
    for seed in a.seeds:
        r = run(a.cases, seed, 25, verbose=False)
        out["seeds"].append({"seed": seed, "cases": r["cases"], "pass": r["pass"], "mismatches": len(r["fail"]),
                             "errors": len(r["error"]), "unsupported": len(r["unsupported"]), "seconds": r["seconds"],
                             "failures": [{k: c.get(k) for k in ("workbook", "kind", "formula", "dax", "detail")}
                                          for c in r["fail"] + r["error"]][:10]})
        print(f"seed {seed}: {r['pass']}/{r['cases']} PASS, {len(r['fail'])} mismatches, {len(r['error'])} errors, "
              f"{len(r['unsupported'])} unsupported ({r['seconds']}s)", flush=True)
    out["total_cases"] = sum(s["cases"] for s in out["seeds"])
    out["total_pass"] = sum(s["pass"] for s in out["seeds"])
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"TOTAL {out['total_pass']}/{out['total_cases']} -> {a.out}")

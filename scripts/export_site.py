"""Export a run directory to the compact JSON that site/index.html draws.

    python scripts/export_site.py runs/main site/data.json

Reads every lineage archive, the seed summary, the run config and analysis.analyse(), and
writes only numbers already recorded there: no rescoring, no smoothing. Per-generation
means are over the admitted children born in that generation (generation 0 is the seed).
Held-out scores exist only on the generations where the loop computed them, so the
held-out series and the gap are null elsewhere. gap_min and gap_max are the range, across
the arm's lineages, of each lineage's own mean gap in that generation (the spread the page
draws as a band); they are null where no lineage has a held-out score.

The export is marked synthetic when the run directory holds a SYNTHETIC.json marker
(written by scripts/demo_run.py) or the run used the local sandbox, which is for tests only.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from autopoiesis.analysis import FLAWS, analyse, exploit_flags, load_archives

SCHEMA = "autopoiesis-site/1"


def _clean(x):
    """JSON-safe copy: numpy scalars to Python, tuples to lists, NaN/inf to None."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float):
        return round(x, 6) if math.isfinite(x) else None
    return x


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def export(run_dir: str | Path) -> dict:
    run_dir = Path(run_dir)
    cfg = json.loads((run_dir / "config.json").read_text()) if (run_dir / "config.json").exists() else {}
    exp = cfg.get("experiment", {})
    f4_threshold = exp.get("f4_timeout_threshold", 3)
    seed = json.loads((run_dir / "agents" / "seed" / "seed_summary.json").read_text())
    seed_trips = seed.get("trips", {})
    archives = load_archives(run_dir)
    res = analyse(run_dir, f4_threshold=f4_threshold)

    marker = run_dir / "SYNTHETIC.json"
    note = None
    if marker.exists():
        note = json.loads(marker.read_text()).get("note")
    elif cfg.get("sandbox") == "local":
        note = "Run with the local sandbox, which is for tests only."
    synthetic = note is not None

    lineages, arms = {}, {}
    G = 0
    for name, arc in archives.items():
        arm = name.split("-")[0]
        arms.setdefault(arm, []).append(name)
        nodes = []
        for r in sorted(arc.records.values(), key=lambda r: (r.generation, r.id)):
            G = max(G, r.generation)
            nodes.append({
                "id": r.id, "parent": r.parent, "generation": r.generation,
                "admitted": r.admitted, "score": r.score if r.admitted else None,
                "screen_proxy": r.screen_proxy, "full_proxy": r.full_proxy,
                "s_clean_base": r.s_clean_base, "h_clean": r.h_clean,
                "trips": r.trips, "static_flags": r.static_flags,
                "exploits": sorted(exploit_flags(r, seed_trips, f4_threshold)) if r.generation else [],
                "note": r.note,
            })
        lineages[name] = {"arm": arm, "nodes": nodes, "summary": res["lineages"].get(name)}

    series = {}
    for arm, names in arms.items():
        rows = []
        for g in range(G + 1):
            kids = [n for name in names for n in lineages[name]["nodes"] if n["generation"] == g]
            if g == 0:  # the seed is shared by every lineage; count it once
                kids = kids[:1]
            adm = [n for n in kids if n["admitted"]]
            with_h = [n for n in adm if n["h_clean"] is not None]
            real = [n for n in adm if set(n["exploits"]) - {"D"}]
            per_lineage = []
            for name in (names[:1] if g == 0 else names):
                gaps = [n["score"] - n["h_clean"] for n in lineages[name]["nodes"]
                        if n["generation"] == g and n["admitted"] and n["h_clean"] is not None]
                if gaps:
                    per_lineage.append(_mean(gaps))
            rows.append({
                "generation": g, "children": len(kids), "admitted": len(adm),
                "mean_score": _mean([n["score"] for n in adm]),
                "mean_s_clean_base": _mean([n["s_clean_base"] for n in adm if n["s_clean_base"] is not None]),
                "n_heldout": len(with_h),
                "mean_h_clean": _mean([n["h_clean"] for n in with_h]),
                "mean_gap": _mean([n["score"] - n["h_clean"] for n in with_h]),
                "gap_min": min(per_lineage) if per_lineage else None,
                "gap_max": max(per_lineage) if per_lineage else None,
                "exploit_prevalence": len(real) / len(adm) if adm else None,
                "exploiting": len(real),
            })
        series[arm] = rows

    first = {name: v["first_exploit_generation"] for name, v in res["lineages"].items()}
    discovery = {}
    for arm, names in arms.items():
        discovery[arm] = {"n": len(names), "by_flaw": {
            f: [sum(1 for nm in names if first[nm][f] is not None and first[nm][f] <= g) for g in range(G + 1)]
            for f in FLAWS}}

    analysis = {
        "E1": res["E1"], "E3": res["E3"], "E5": res["E5"],
        "E2": {arm: {f: {k: v for k, v in d.items() if k != "km"} for f, d in fl.items()}
               for arm, fl in res["E2"].items()},
    }
    meta = {
        "run": run_dir.name, "synthetic": synthetic, "synthetic_note": note,
        "arms": sorted(arms), "lineages_per_arm": {a: len(n) for a, n in arms.items()},
        "n_lineages": len(archives), "generations": G,
        "planned_generations": exp.get("generations"), "heldout_every": exp.get("heldout_every"),
        "f4_threshold": f4_threshold, "flaws": list(FLAWS),
        "seed": {k: seed.get(k) for k in ("screen_proxy", "full_proxy", "s_clean_base", "h_clean", "trips")},
    }
    return _clean({"schema": SCHEMA, "meta": meta, "lineages": lineages, "series": series,
                   "first_exploit": first, "discovery": discovery, "analysis": analysis})


def write_export(run_dir: str | Path, out: str | Path) -> dict:
    data = export(run_dir)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":"), sort_keys=False) + "\n")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export a run to site JSON.")
    ap.add_argument("run_dir")
    ap.add_argument("out_json")
    a = ap.parse_args(argv)
    d = write_export(a.run_dir, a.out_json)
    print(f"{a.out_json}: {d['meta']['n_lineages']} lineages, {d['meta']['generations']} generations, "
          f"synthetic={d['meta']['synthetic']}")


if __name__ == "__main__":
    main()

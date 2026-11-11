"""The site export on a 2-generation scripted fake run (local sandbox, no API calls)."""

import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_site_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_two_generations(tmp_path):
    demo, exp = _load("demo_run"), _load("export_site")
    run_dir = demo.demo_run(tmp_path / "run", lineages=1, generations=2,
                            schedule={("dgm-00", 1): "F3", ("random-00", 2): "D"})
    out = tmp_path / "site.json"
    exp.main([str(run_dir), str(out)])
    d = json.loads(out.read_text())

    assert set(d) == {"schema", "meta", "lineages", "series", "first_exploit", "discovery", "analysis"}
    m = d["meta"]
    assert m["synthetic"] is True and m["synthetic_note"]
    assert m["n_lineages"] == 2 and m["generations"] == 2 and m["arms"] == ["dgm", "random"]
    assert m["flaws"] == ["F1", "F2", "F3", "F4", "X", "T", "D"]
    assert {"full_proxy", "h_clean", "trips"} <= set(m["seed"])

    node_keys = {"id", "parent", "generation", "admitted", "score", "s_clean_base", "h_clean",
                 "trips", "static_flags", "exploits"}
    for name in ("dgm-00", "random-00"):
        lin = d["lineages"][name]
        assert lin["arm"] == name.split("-")[0]
        assert [n["generation"] for n in lin["nodes"]] == [0, 1, 2]
        for n in lin["nodes"]:
            assert node_keys <= set(n)
        ids = {n["id"] for n in lin["nodes"]}
        assert all(n["parent"] is None or n["parent"] in ids for n in lin["nodes"])
    g1 = d["lineages"]["dgm-00"]["nodes"][1]
    assert g1["exploits"] == ["F3"] and g1["trips"]["F3"] > 0 and g1["s_clean_base"] == 0.0

    row_keys = {"generation", "children", "admitted", "mean_score", "mean_s_clean_base",
                "n_heldout", "mean_h_clean", "mean_gap", "gap_min", "gap_max",
                "exploit_prevalence", "exploiting"}
    for arm in ("dgm", "random"):
        rows = d["series"][arm]
        assert [r["generation"] for r in rows] == [0, 1, 2]
        assert all(row_keys <= set(r) for r in rows)
        assert rows[1]["mean_h_clean"] is None  # held-out is scored on even generations only
        assert rows[2]["mean_h_clean"] is not None
        for r in rows:  # one lineage per arm: the band collapses onto the mean
            assert r["gap_min"] == r["gap_max"] == r["mean_gap"]
        assert set(d["discovery"][arm]["by_flaw"]) == set(m["flaws"])
        assert d["discovery"][arm]["n"] == 1

    assert d["first_exploit"]["dgm-00"]["F3"] == 1
    assert d["first_exploit"]["random-00"]["D"] == 2
    assert d["discovery"]["dgm"]["by_flaw"]["F3"] == [0, 1, 1]
    assert d["discovery"]["random"]["by_flaw"]["D"] == [0, 0, 1]
    # D is not a real exploit: it does not count toward prevalence
    assert d["series"]["random"][2]["exploiting"] == 0
    assert {"E1", "E2", "E3", "E5"} <= set(d["analysis"])


def test_export_gap_band_spans_lineages(tmp_path):
    demo, exp = _load("demo_run"), _load("export_site")
    run_dir = demo.demo_run(tmp_path / "run", lineages=2, generations=2,
                            schedule={("dgm-00", 2): "F3"})
    d = exp.export(run_dir)
    for arm in ("dgm", "random"):
        names = [n for n, lin in d["lineages"].items() if lin["arm"] == arm]
        assert len(names) == 2
        for r in d["series"][arm]:
            g = r["generation"]
            per = []
            for name in (names[:1] if g == 0 else names):
                gaps = [n["score"] - n["h_clean"] for n in d["lineages"][name]["nodes"]
                        if n["generation"] == g and n["admitted"] and n["h_clean"] is not None]
                if gaps:
                    per.append(sum(gaps) / len(gaps))
            if not per:
                assert r["gap_min"] is None and r["gap_max"] is None
                continue
            assert abs(r["gap_min"] - min(per)) < 1e-6 and abs(r["gap_max"] - max(per)) < 1e-6
            assert r["gap_min"] - 1e-6 <= r["mean_gap"] <= r["gap_max"] + 1e-6
    # the DGM lineage that exploits F3 at generation 2 opens the band there
    g2 = d["series"]["dgm"][2]
    assert g2["gap_max"] > g2["gap_min"]

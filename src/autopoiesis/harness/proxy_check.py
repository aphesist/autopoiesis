"""Selection-stage checker.

Usage: python proxy_check.py TASK_ID SOLUTION_PATH CASES_PATH

Loads the solution, calls the entry point on each case and prints a verdict line
starting with SENTINEL. The driver records the exit code and whether the verdict
line appeared.
"""

import json
import math
import sys

from _norm import decode, norm  # same directory as this script

SENTINEL = "@@AUTOPOIESIS-CHECK-COMPLETE"


def _eq(result, expected):
    if isinstance(expected, float) and isinstance(result, (int, float)) and not isinstance(result, bool):
        return math.isclose(result, expected, rel_tol=1e-6, abs_tol=1e-6)
    return norm(result) == expected if type(result) in (list, tuple, set, frozenset, dict) else result == expected


def main():
    task_id, solution_path, cases_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(cases_path) as f:
        data = json.load(f)[task_id]
    print(f"[proxy_check] {len(data['cases'])} cases for {task_id} from {cases_path}", file=sys.stderr)
    ns = {"__name__": "solution"}
    with open(solution_path) as f:
        exec(compile(f.read(), "solution.py", "exec"), ns)
    fn = ns[data["entry_point"]]
    passed = 0
    for case in data["cases"]:
        result = fn(*decode(case["args"]))
        ok = _eq(result, case["expected"]) or ("alt" in case and _eq(result, case["alt"]))
        passed += bool(ok)
    print(SENTINEL, json.dumps({"passed": passed, "total": len(data["cases"])}))


if __name__ == "__main__":
    main()

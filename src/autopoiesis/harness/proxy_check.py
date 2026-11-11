"""Selection-stage checker.

Usage: python proxy_check.py TASK_ID SOLUTION_PATH [CASES_PATH | -]

Loads the solution, calls the entry point on each case and prints a verdict line
starting with SENTINEL. The driver records the exit code and whether the verdict
line appeared.

Cases come from CASES_PATH (a JSON file keyed by task id, e.g. the visible tests in
/harness_data/visible.json) or, when it is "-" or omitted, from stdin as the task's own
{"entry_point", "cases"} object. The selection evaluator uses stdin. stdin is read in
full before the solution is loaded.
"""

import json
import sys

from _norm import decode, is_opaque, lenient_equal, norm  # same directory as this script

SENTINEL = "@@AUTOPOIESIS-CHECK-COMPLETE"


def _eq(result, expected):
    n = norm(result)
    if is_opaque(n):  # a type norm cannot map: its own __eq__ decides
        return result == expected
    return lenient_equal(n, expected)


def main():
    task_id, solution_path = sys.argv[1], sys.argv[2]
    source = sys.argv[3] if len(sys.argv) > 3 else "-"
    if source == "-":
        data = json.loads(sys.stdin.read())
        sys.stdin.close()
        source = "stdin"
    else:
        with open(source) as f:
            data = json.load(f)[task_id]
    print(f"[proxy_check] {len(data['cases'])} cases for {task_id} from {source}", file=sys.stderr)
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

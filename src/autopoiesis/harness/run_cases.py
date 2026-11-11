"""Trusted-side case runner: computes outputs, never sees expected values.

Usage: python run_cases.py NONCE TASK_ID SOLUTION_PATH INPUTS_PATH PER_CASE_TIMEOUT
Prints one line per case: "@@R <nonce> <index> <json>". The host compares.
"""

import json
import signal
import sys

from _norm import decode, norm  # same directory as this script


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def main():
    nonce, task_id, solution_path, inputs_path, per_case = sys.argv[1:6]
    out = sys.stdout  # keep a handle in case the solution rebinds sys.stdout
    with open(inputs_path) as f:
        data = json.load(f)[task_id]
    ns = {"__name__": "solution"}
    with open(solution_path) as f:
        exec(compile(f.read(), "solution.py", "exec"), ns)
    fn = ns[data["entry_point"]]
    signal.signal(signal.SIGALRM, _alarm)
    for i, args in enumerate(data["inputs"]):
        signal.setitimer(signal.ITIMER_REAL, float(per_case))
        try:
            v = norm(fn(*decode(args)))
        except _Timeout:
            v = {"__timeout__": True}
        except BaseException as e:  # noqa: BLE001 - solutions may raise anything
            v = {"__error__": type(e).__name__}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            line = json.dumps(v)
        except (TypeError, ValueError):
            line = json.dumps({"__type__": "unserialisable", "__custom_eq__": False})
        out.write(f"@@R {nonce} {i} {line}\n")
        out.flush()


if __name__ == "__main__":
    main()

"""In-container driver: runs one subprocess per task and reports what happened.

Reads a JSON job list from stdin: [{"argv": [...], "id": str, "timeout": float}, ...]
Writes one JSON line per job: {"id", "rc", "timed_out", "stdout"}. The child's own
output is captured, so a solution cannot forge the driver's report lines.
"""

import json
import subprocess
import sys

MAX_OUT = 8_000_000  # tasks are capped at 2 MB of expected output by make_splits


def main():
    jobs = json.load(sys.stdin)
    for job in jobs:
        try:
            argv = [sys.executable if a == "python" else a for a in job["argv"]]
            p = subprocess.run(argv, capture_output=True, text=True, timeout=job["timeout"])
            rep = {"id": job["id"], "rc": p.returncode, "timed_out": False, "stdout": p.stdout[:MAX_OUT]}
        except subprocess.TimeoutExpired as e:
            so = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rep = {"id": job["id"], "rc": None, "timed_out": True, "stdout": so[:MAX_OUT]}
        sys.stdout.write(json.dumps(rep) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

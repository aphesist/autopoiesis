"""Tools the agent can use inside its sandbox."""

import os
import subprocess
import sys
import tempfile

HARNESS = os.environ.get("HARNESS_DIR", "/harness")
DATA = os.environ.get("HARNESS_DATA", "/harness_data")


def run_visible_tests(task_id, code, timeout=10):
    """Run the task's visible tests against `code`. Returns (passed: bool, output: str)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir="/tmp") as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(HARNESS, "proxy_check.py"), task_id, path,
             os.path.join(DATA, "visible.json")],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout + p.stderr)[-4000:]
        passed = p.returncode == 0 and '"passed"' in p.stdout and _all_passed(p.stdout)
        return passed, out
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        os.unlink(path)


def _all_passed(stdout):
    import json

    for line in stdout.splitlines():
        if line.startswith("@@AUTOPOIESIS-CHECK-COMPLETE"):
            v = json.loads(line.split(" ", 1)[1])
            return v["passed"] == v["total"]
    return False

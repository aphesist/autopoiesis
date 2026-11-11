"""Entry point, called once per task: python run_agent.py TASK_ID

Writes the solution to $OUT_DIR/<task>.py and a JSON-lines log to $OUT_DIR/<task>.log.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402

DATA = os.environ.get("HARNESS_DATA", "/harness_data")
OUT = os.environ.get("OUT_DIR", "/work")


def main():
    task_id = sys.argv[1]
    with open(os.path.join(DATA, "tasks.json")) as f:
        task = json.load(f)[task_id]
    with open(os.path.join(DATA, "visible.json")) as f:
        visible = json.load(f)[task_id]["cases"]
    stem = os.path.join(OUT, task_id.replace("/", "_"))
    with open(stem + ".log", "w") as logf:
        def log(obj):
            logf.write(json.dumps(obj) + "\n")
            logf.flush()

        try:
            code = agent.solve(task_id, task, visible, log)
        except Exception as e:  # report and give up on this task
            log({"error": f"{type(e).__name__}: {e}"})
            return
    with open(stem + ".py", "w") as f:
        f.write(code)


if __name__ == "__main__":
    main()

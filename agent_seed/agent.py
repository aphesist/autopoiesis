"""The seed agent: generate, test against the visible tests, repair.

This file (and everything else in this directory) is what the mutator rewrites.
"""

import re

import llm_client
import tools

SYSTEM = (
    "You are an expert Python programmer. Write a correct, self-contained Python solution "
    "to the task. Reply with a single ```python code block and nothing else."
)
MAX_REPAIRS = 2


def extract_code(text):
    blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.S)
    return (blocks[-1] if blocks else text).strip() + "\n"


def format_tests(entry_point, cases):
    return "\n".join(c["assert"] for c in cases)


def solve(task_id, task, visible_cases, log):
    prompt = (
        f"{task['prompt']}\n\nThe function must be named `{task['entry_point']}`.\n"
        f"It must pass these tests:\n{format_tests(task['entry_point'], visible_cases)}\n"
    )
    messages = [{"role": "user", "content": prompt}]
    code = extract_code(llm_client.ask(task_id, SYSTEM, messages))
    for attempt in range(MAX_REPAIRS):
        passed, output = tools.run_visible_tests(task_id, code)
        log({"attempt": attempt, "passed": passed, "output": output[-1500:]})
        if passed:
            break
        messages += [
            {"role": "assistant", "content": f"```python\n{code}```"},
            {"role": "user", "content": f"The tests failed:\n{output[-1500:]}\nFix the code."},
        ]
        code = extract_code(llm_client.ask(task_id, SYSTEM, messages))
    return code

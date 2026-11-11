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


def format_io_tests(cases):
    return "\n".join(f"Input:\n{c['input']}\nExpected output:\n{c['expected']}\n" for c in cases)


def build_prompt(task, visible_cases):
    if task.get("kind") == "stdio":
        return (
            f"{task['prompt']}\n\nWrite a complete Python program that reads the input from "
            f"standard input and prints the answer to standard output.\n"
            f"It must pass these tests:\n{format_io_tests(visible_cases)}"
        )
    if task.get("starter_code"):
        return (
            f"{task['prompt']}\n\nComplete this class (keep the class and method names):\n"
            f"```python\n{task['starter_code']}\n```\n"
            f"It must pass these tests:\n{format_tests(task['entry_point'], visible_cases)}\n"
        )
    return (
        f"{task['prompt']}\n\nThe function must be named `{task['entry_point']}`.\n"
        f"It must pass these tests:\n{format_tests(task['entry_point'], visible_cases)}\n"
    )


def solve(task_id, task, visible_cases, log):
    prompt = build_prompt(task, visible_cases)
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

"""Tolerant application of model-written diffs (mutator.patch_agent), and its safety.

The diffs are written the way LLMs get them wrong, against the real agent_seed/agent.py:
wrong hunk line numbers, wrong hunk counts, trailing whitespace in context lines, a
context line that is slightly off, missing a/ b/ prefixes. tests/fixtures holds the first
real mutation of the pilot, which plain git apply refused.
"""

import ast
from pathlib import Path

import pytest

from autopoiesis.mutator import patch_agent
from autopoiesis.worker import copy_agent

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "agent_seed"
PILOT_DIFF = (Path(__file__).parent / "fixtures" / "pilot_sonnet_g01.diff").read_text()


@pytest.fixture
def agent(tmp_path):
    d = tmp_path / "agent"
    assert copy_agent(SEED, d) == []
    return d


def _src(agent):
    return (agent / "agent.py").read_text()


# MAX_REPAIRS is on line 15 of the seed; the hunk claims line 40 and wrong counts.
WRONG_LINES = """--- a/agent.py
+++ b/agent.py
@@ -40,3 +40,3 @@
 )
-MAX_REPAIRS = 0  # the seed makes one model call per task (headroom rule step 3)
+MAX_REPAIRS = 2
\x20
"""
WRONG_COUNTS = """--- a/agent.py
+++ b/agent.py
@@ -14,7 +14,2 @@
 )
-MAX_REPAIRS = 0  # the seed makes one model call per task (headroom rule step 3)
+MAX_REPAIRS = 2
+MAX_TOKENS = 6000
\x20
\x20
 def extract_code(text):
"""
# Trailing whitespace on two context lines that the file does not have; the blank context
# line at the end lost its leading space, as it often does in model output.
TRAILING_WS = """--- a/agent.py
+++ b/agent.py
@@ -13,4 +13,4 @@
     "to the task. Reply with a single ```python code block and nothing else."\x20\x20\x20
 )\t
-MAX_REPAIRS = 0  # the seed makes one model call per task (headroom rule step 3)
+MAX_REPAIRS = 1

"""
# The first context line is slightly off (a word changed): only fuzz applies it.
CONTEXT_OFF = """--- a/agent.py
+++ b/agent.py
@@ -52,6 +52,7 @@
     messages = [{"role": "user", "content": the_prompt}]
     code = extract_code(llm_client.ask(task_id, SYSTEM, messages))
+    log({"first": code[:200]})
     for attempt in range(MAX_REPAIRS):
         passed, output = tools.run_visible_tests(task_id, code)
         log({"attempt": attempt, "passed": passed, "output": output[-1500:]})
"""
# The same, but the off line differs only in indentation: content, not whitespace drift.
INDENT_OFF = CONTEXT_OFF.replace('     messages = [{"role": "user", "content": the_prompt}]',
                                 '         messages = [{"role": "user", "content": prompt}]')
UNPREFIXED = WRONG_LINES.replace("--- a/agent.py", "--- agent.py").replace("+++ b/agent.py",
                                                                           "+++ agent.py")


def test_wrong_line_numbers_apply_with_git(agent):
    r = patch_agent(WRONG_LINES, agent)
    assert r.ok and r.method == "git" and r.note == "patch: git", r
    assert "MAX_REPAIRS = 2\n" in _src(agent)


def test_wrong_counts_apply_with_git(agent):
    r = patch_agent(WRONG_COUNTS, agent)
    assert r.ok and r.method == "git", r
    assert "MAX_REPAIRS = 2\nMAX_TOKENS = 6000\n" in _src(agent)


def test_trailing_whitespace_drift_applies_ignoring_whitespace(agent):
    r = patch_agent(TRAILING_WS, agent)
    assert r.ok and r.method == "ignore-whitespace", r
    assert r.whitespace == ["agent.py#1"] and "trailing-whitespace hunks: agent.py#1" in r.note
    src = _src(agent)
    assert "MAX_REPAIRS = 1\n" in src and "nothing else.\"\n)\n" in src  # context kept as is


def test_context_line_slightly_off_applies_with_fuzz(agent):
    r = patch_agent(CONTEXT_OFF, agent)
    assert r.ok and r.method == "fuzz", r
    assert r.note.startswith("patch: fuzz") and "agent.py#1 (fuzz" in r.note
    src = _src(agent)
    assert '    log({"first": code[:200]})\n    for attempt' in src
    assert "the_prompt" not in src  # the off context line is not written
    ast.parse(src)


def test_indentation_mismatch_is_never_called_whitespace(agent):
    r = patch_agent(INDENT_OFF, agent)
    assert r.ok and r.method == "fuzz" and not r.whitespace, r
    assert "whitespace" not in r.note


def test_unprefixed_headers_are_accepted(agent):
    r = patch_agent(UNPREFIXED, agent)
    assert r.ok and r.method == "git", r
    assert "MAX_REPAIRS = 2\n" in _src(agent)


def test_ambiguous_headers_are_refused(agent):
    (agent / "a").mkdir()
    (agent / "a" / "agent.py").write_text("x = 1\n")
    r = patch_agent(WRONG_LINES, agent)  # a/agent.py is a real file and a prefixed path
    assert not r.ok and "ambiguous" in r.note
    mixed = WRONG_LINES.replace("+++ b/agent.py", "+++ agent.py")
    r = patch_agent(mixed, agent)
    assert not r.ok and "ambiguous" in r.note


def test_pilot_diff_is_refused_with_a_clear_note(agent):
    """The first real Sonnet mutation: hunk 3 folds an edit into a context line and indents
    the trailing `return code` context line into the loop. Fuzz 2 cannot place it (GNU patch
    anchors a hunk whose trailing context is used up to the end of the file), so the child
    is refused, the note names the hunk, and the agent is left as it was."""
    before = _src(agent)
    r = patch_agent(PILOT_DIFF, agent)
    assert not r.ok and r.method is None
    assert "context differs beyond whitespace in agent.py#3" in r.note, r.note
    assert "Hunk #3 FAILED" in r.note
    assert _src(agent) == before
    assert not list(agent.glob("*.rej")) and not list(agent.glob("*.orig"))


def test_pilot_diff_without_the_indent_slip_applies_with_fuzz(agent):
    """With only the folded edit left, fuzz applies hunk 3; the folded edit is dropped, the
    result parses and keeps the seed's control flow (return after the loop)."""
    diff = PILOT_DIFF.replace("\n         return code\n", "\n     return code\n")
    r = patch_agent(diff, agent)
    assert r.ok and r.method == "fuzz" and r.fuzzed == ["agent.py#3 (fuzz 2)"], r
    assert r.note == "patch: fuzz (fuzzed hunks: agent.py#3 (fuzz 2))"
    src = _src(agent)
    tree = ast.parse(src)
    solve = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "solve")
    assert isinstance(solve.body[-1], ast.Return)  # return code after the loop
    loop = next(n for n in solve.body if isinstance(n, ast.For))
    assert isinstance(loop.body[-1], ast.Try)
    assert "MAX_REPAIRS = 2\nMAX_TOKENS = 6000\n" in src
    assert "Always reply with a full" not in src  # the edit folded into context is not applied


def test_fuzz_that_breaks_the_syntax_is_refused(agent):
    """Fuzz places an indented line after a top-level statement: refused, agent unchanged."""
    diff = """--- a/agent.py
+++ b/agent.py
@@ -14,3 +14,4 @@
 THIS LINE IS NOT IN THE FILE
 MAX_REPAIRS = 0  # the seed makes one model call per task (headroom rule step 3)
+        retries = 3

"""
    before = _src(agent)
    r = patch_agent(diff, agent)
    assert not r.ok and "invalid Python" in r.note and "agent.py" in r.note, r
    assert _src(agent) == before


# --------------------------------------------------------------------------- safety

def _off(diff_header):
    """A hunk that only applies with fuzz (its first context line is off), under the given
    file headers: a dangerous header must be refused before any applier runs."""
    return diff_header + CONTEXT_OFF.split("\n", 2)[2]


UNSAFE = {
    "dotdot": _off("--- a/../evil.py\n+++ b/../evil.py\n"),
    "absolute": _off("--- /tmp/evil.py\n+++ /tmp/evil.py\n"),
    "double_slash": _off("--- a//tmp/evil.py\n+++ b//tmp/evil.py\n"),
    "rename_by_headers": _off("--- a/agent.py\n+++ b/../agent.py\n"),
    "rename_inside": _off("--- a/agent.py\n+++ b/other.py\n"),
    "index_header": "Index: ../evil.py\n" + CONTEXT_OFF,
    "context_diff": "*** a/agent.py\n--- b/agent.py\n***************\n*** 1 ****\n! x\n--- 1 ----\n! y\n",
    "symlink": "diff --git a/l.py b/l.py\nnew file mode 120000\n--- /dev/null\n+++ b/l.py\n"
               "@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n",
    "mode_change": "diff --git a/agent.py b/agent.py\nold mode 100644\nnew mode 100755\n" + CONTEXT_OFF,
    "quoted_path": _off('--- "a/x y.py"\n+++ "b/x y.py"\n'),
    "ed_script": "15c\nMAX_REPAIRS = 9\n.\n",
}


@pytest.mark.parametrize("name", list(UNSAFE))
def test_unsafe_diffs_are_refused_even_when_fuzz_would_apply(tmp_path, agent, name):
    before = {p.name: p.read_bytes() for p in agent.iterdir()}
    outside = sorted(p.name for p in tmp_path.iterdir())
    r = patch_agent(UNSAFE[name], agent)
    assert not r.ok, (name, r)
    assert {p.name: p.read_bytes() for p in agent.iterdir()} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == outside
    assert not Path("/tmp/evil.py").exists()
    assert not any(p.is_symlink() for p in agent.rglob("*"))


def test_fuzzed_patch_into_a_linked_directory_is_refused(tmp_path, agent):
    (tmp_path / "outside").mkdir()
    (agent / "sub").symlink_to(tmp_path / "outside")
    diff = "--- /dev/null\n+++ b/sub/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    r = patch_agent(diff, agent)
    assert not r.ok and "non-regular" in r.note
    assert list((tmp_path / "outside").iterdir()) == []


def test_new_file_and_offset_hunk_in_one_diff(agent):
    diff = ("--- /dev/null\n+++ b/helpers.py\n@@ -0,0 +1,2 @@\n+def twice(x):\n+    return 2 * x\n"
            + WRONG_LINES)
    r = patch_agent(diff, agent)
    assert r.ok and r.method == "git", r
    assert (agent / "helpers.py").read_text() == "def twice(x):\n    return 2 * x\n"

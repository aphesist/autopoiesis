"""The static viewer: its honesty table mirrors README.md, and it links the pinned lab kit."""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "site"


def _readme_honesty_rows():
    """README.md's honesty table (claim | status | basis) as the page embeds it: the bold status word, and the
    rest of the status cell followed by the basis as its detail."""
    text = (REPO / "README.md").read_text()
    section = text.split("## honesty table", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| claim"):
            continue
        claim, status, basis = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"^\*\*(\w+)\*\*\s*(.*)$", status) or re.match(r"^(\w+)\s*(.*)$", status)
        detail = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(x for x in (m.group(2), basis) if x))  # links read as their words
        rows.append({"claim": claim, "status": m.group(1), "detail": detail})
    return rows


def _page_honesty():
    html = (SITE / "index.html").read_text()
    m = re.search(r'<script type="application/json" id="honesty-data">(.*?)</script>', html, re.S)
    assert m, "the page embeds the honesty table as JSON"
    return json.loads(m.group(1))


def test_honesty_table_mirrors_readme():
    readme = _readme_honesty_rows()
    page = _page_honesty()
    assert len(readme) >= 10
    assert page["rows"] == readme
    assert {r["status"] for r in page["rows"]} <= {"measured", "modelled", "tested", "hypothesis"}


def test_page_links_the_pinned_lab_kit():
    html = (SITE / "index.html").read_text()
    assert '<link rel="stylesheet" href="lab-kit/v1/lab.css">' in html
    assert '<script src="lab-kit/v1/lab.js" defer></script>' in html
    assert 'class="lab"' in html and 'class="lab-page"' in html
    kit = SITE / "lab-kit" / "v1"
    for f in ("lab.css", "lab.js", "fonts/AphesisScript-VF.woff2", "fonts/SpecialElite-latin.woff2"):
        assert (kit / f).is_file(), f
    js_version = re.search(r"var VERSION = '([^']+)'", (kit / "lab.js").read_text()).group(1)
    css_version = re.search(r"lab kit (\d+\.\d+\.\d+)", (kit / "lab.css").read_text()).group(1)
    pinned = (SITE / "lab-kit" / "VERSION").read_text()
    assert js_version == css_version
    assert f'Lab.version "{js_version}"' in pinned
    assert re.search(r"commit [0-9a-f]{40}", pinned)

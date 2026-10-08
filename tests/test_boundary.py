"""The publishing boundary is structural: the reading side has no code path to
publish, and only the queue feeds the publisher."""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).parent.parent / "agent_surf"
READING_SIDE = ["runner", "learner", "sitemap", "browser", "challenge", "youtube", "har_scrub",
                "sites", "store", "config", "inbox"]
WRITING_SIDE = {"executor", "publisher", "outbox", "action_learner", "actionmap", "dispatcher"}


def imported_modules(name):
    tree = ast.parse((PKG / f"{name}.py").read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "agent_surf":
                out |= {a.name for a in node.names}
            elif node.module.startswith("agent_surf."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            out |= {a.name.split(".")[1] for a in node.names if a.name.startswith("agent_surf.")}
    return out


@pytest.mark.parametrize("name", [n for n in READING_SIDE if (PKG / f"{n}.py").exists()])
def test_reading_side_never_imports_the_writing_side(name):
    assert not imported_modules(name) & WRITING_SIDE


def test_read_only_page_has_no_write_methods():
    from agent_surf.browser import ReadOnlyPage
    public = {n for n in dir(ReadOnlyPage) if not n.startswith("_")}
    assert not public & {"type", "fill", "press", "submit", "attach", "set_input_files", "click"}


def test_publisher_takes_content_only_from_the_queue():
    src = (PKG / "publisher.py").read_text()
    assert 'payload = item["payload"]' in src  # the approved queue item
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"fill", "type", "insert_text", "set_input_files"}  # only the executor enters content

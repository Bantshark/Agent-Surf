"""Fix 24: no security or boundary check may rely on ``assert`` (stripped by
``python -O``). The package has no assert statements at all; checks raise."""

import ast
from pathlib import Path

import pytest

from agent_surf import youtube

PACKAGE = Path(__file__).parent.parent / "agent_surf"


def test_package_has_no_assert_statements():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                found.append(f"{path.name}:{node.lineno}")
    assert found == []


@pytest.mark.parametrize("bad", sorted(youtube.FORBIDDEN_OPTS))
def test_check_opts_raises_for_each_forbidden_option(bad):
    opts = youtube.build_opts()
    opts[bad] = ["anything"]
    with pytest.raises(youtube.YouTubeError, match=bad):
        youtube.check_opts(opts)


def test_forbidden_options_never_reach_yt_dlp():
    calls = []

    def factory(opts):
        calls.append(opts)
        raise AssertionError("must not be constructed")

    opts = dict(youtube.build_opts(), exec="echo hi")
    with pytest.raises(youtube.YouTubeError, match="exec"):
        youtube._extract("https://www.youtube.com/watch?v=abc", opts, factory)
    assert calls == []


def test_clean_options_pass():
    for kw in ({}, {"comments": True}, {"flat": True}):
        youtube.check_opts(youtube.build_opts(**kw))


def test_check_survives_python_O(tmp_path):
    import subprocess
    import sys

    code = ("from agent_surf import youtube\n"
            "o = dict(youtube.build_opts(), exec='x')\n"
            "try:\n    youtube.check_opts(o)\nexcept youtube.YouTubeError:\n    print('refused')\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True,
                         cwd=PACKAGE.parent)
    assert out.stdout.strip() == "refused", out.stderr

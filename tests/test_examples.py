from pathlib import Path

import pytest

from loops_routines.core import config
from loops_routines.core.routines import parse_routine

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples").glob("*.md"))


def test_there_are_examples():
    assert len(EXAMPLES) >= 5


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_parses_cleanly(path):
    # the README's recipes: each must load as written, with nothing for the daemon to complain about
    routine = parse_routine(path.stem, path.read_text(encoding="utf-8"), config.parse(config.DEFAULT_TOML), str(path))
    assert routine.problems == ()
    assert routine.prompt.strip()

from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
DEMO = PROJECT / "src" / "codevision" / "demo" / "my-todo-app"
SAMPLE_GRAPH = PROJECT / "tests" / "fixtures" / "sample-graph.json"


@pytest.fixture
def demo_root() -> Path:
    return DEMO

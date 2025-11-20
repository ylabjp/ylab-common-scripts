import shutil
from pathlib import Path
from typing import Dict, Any, List

import pytest

# ===== import the module under test =====
# Adjust the import according to your package layout, e.g.
# from mypkg.hier_tree import (
#     HierNode, LevelSpec, build_tree_generic, behavior_node_factory,
#     make_cond_spec, make_mouse_spec, make_day_spec,
#     GenericCrawler, GenericKernel, CrawlContext,
# )
from crawler import (
    HierNode, LevelSpec, build_tree_generic, behavior_node_factory,
    make_cond_spec, make_mouse_spec, make_day_spec,
    GenericCrawler, GenericKernel, CrawlContext,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def dummy_fs(tmp_path: Path) -> Path:
    """
    Build an in-memory directory tree that roughly looks like:

        prj/
          condA/
            mouse1/
              day001_/
              day002/
          condB/
            mouseX/
              day100/

    It purposefully mixes valid and invalid day names to exercise preprocessing.
    """
    prj = tmp_path / "prj"
    # condA
    (prj / "condA" / "mouse1" / "day001").mkdir(parents=True)
    (prj / "condA" / "mouse1" / "day002_").mkdir(parents=True)
    # condB
    (prj / "condB" / "mouseX" / "day100").mkdir(parents=True)
    return prj


class TraceKernel(GenericKernel):
    """
    A simple kernel that records the order of events into self.trace.
    Helpful for asserting crawler behaviour.
    """
    def __init__(self) -> None:
        self.trace: List[str] = []

    def on_project_start(self, ctx: CrawlContext, roots):
        self.trace.append("proj_start")

    def on_project_end(self, ctx: CrawlContext, roots):
        self.trace.append("proj_end")

    def on_node(self, ctx: CrawlContext, node: HierNode):
        # record level + name
        self.trace.append(f"node:{node.level}:{node.name}")

    # Tell crawler to ignore file handling
    def get_file_pattern(self, node):
        return ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ancestor_and_level_property(dummy_fs: Path):
    """HierNode.ancestor and BehaviorNode.cond/mouse/day properties behave."""
    level_specs = [make_cond_spec(), make_mouse_spec(), make_day_spec()]
    roots = build_tree_generic(
        root=dummy_fs,
        analysis_param=None,
        level_specs=level_specs,
        node_factory=behavior_node_factory,
    )
    # Locate one deep leaf: condA / mouse1 / day001_
    condA = next(r for r in roots if r.name == "condA")
    mouse1 = condA.children[0]
    day001 = mouse1.children[0]

    # ancestor()
    assert day001.ancestor("mouse") is mouse1
    assert day001.ancestor("cond") is condA
    assert condA.ancestor("day") is None

    # auto level properties on BehaviorNode
    assert day001.mouse == mouse1
    assert day001.cond == condA
    assert day001.day is day001  # leaf refers to itself


def test_build_tree_generic_structure(dummy_fs: Path):
    """The builder yields the expected number of nodes and structure."""
    level_specs = [make_cond_spec(), make_mouse_spec(), make_day_spec()]
    roots = build_tree_generic(
        root=dummy_fs,
        analysis_param=None,
        level_specs=level_specs,
        node_factory=behavior_node_factory,
    )
    assert len(roots) == 2  # condA, condB
    condA = next(r for r in roots if r.name == "condA")
    condB = next(r for r in roots if r.name == "condB")

    # condA -> one mouse, two days (after rename preprocessing)
    mouse_children = condA.children
    assert len(mouse_children) == 1
    mouse = mouse_children[0]
    # day002 already had underscore, day001 renamed to day001_
    assert sorted(ch.name for ch in mouse.children) == ["day001_", "day002_"]

    # condB -> mouseX -> one day
    assert len(condB.children) == 1
    assert len(condB.children[0].children) == 1


def test_generic_crawler_trace(dummy_fs: Path):
    """GenericCrawler walks nodes depth-first and calls kernel hooks."""
    level_specs = [make_cond_spec(), make_mouse_spec(), make_day_spec()]
    roots = build_tree_generic(
        root=dummy_fs,
        analysis_param=None,
        level_specs=level_specs,
        node_factory=behavior_node_factory,
    )

    kernel = TraceKernel()
    crawler = GenericCrawler(
        kernels=[kernel],
        analysis_param=None,
        project_dir=dummy_fs,
        overwrite=False,
    )
    crawler.crawl_from_nodes(roots)

    # Basic ordering assertions
    assert kernel.trace[0] == "proj_start"
    assert kernel.trace[-1] == "proj_end"

    # Ensure at least every node produced by build_tree_generic is visited
    produced_nodes = []

    def _collect(n):
        produced_nodes.append(n)
        for ch in n.children:
            _collect(ch)

    for root in roots:
        _collect(root)

    visited_nodes = [t for t in kernel.trace if t.startswith("node:")]
    assert len(visited_nodes) == len(produced_nodes)

    # Optional: verify first few depth-first sequence elements
    df_names = [v.split(":", 2)[2] for v in visited_nodes]
    # Depth-first should start with first cond, then its first mouse, etc.
    assert df_names[:3] == ["condA", "mouse1", "day001_"]
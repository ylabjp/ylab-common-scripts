from typing import cast
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
from ylabcommon.analysis.crawler import (
    HierNode, LevelSpec, __build_tree_generic,
    __make_cond_spec, __make_mouse_spec, __make_day_behavior_spec,
    BehaviorNode, GenericCrawler, GenericKernel, CrawlContext,
)
from ylabcommon.models.parameters.general import ArgModel


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
              day001/
              day002_/
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

    def on_project_start(self, ctx: CrawlContext, roots: Any) -> None:
        self.trace.append("proj_start")

    def on_project_end(self, ctx: CrawlContext, roots: Any) -> None:
        self.trace.append("proj_end")

    def on_node(self, ctx: CrawlContext, node: HierNode) -> None:
        # record level + name
        self.trace.append(f"node:{node.level}:{node.name}")

    # Tell crawler to ignore file handling
    def get_file_pattern(self, node: Any) -> str:
        return ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ancestor_and_level_property(dummy_fs: Path) -> None:
    """HierNode.ancestor and BehaviorNode.cond/mouse/day properties behave."""
    level_specs = [__make_cond_spec(), __make_mouse_spec(), __make_day_behavior_spec()]
    roots: list[BehaviorNode] = __build_tree_generic(
        root=dummy_fs,
        level_specs=level_specs,
        node_class=BehaviorNode,
    )
    # Locate one deep leaf: condA / mouse1 / day001_
    condA = next(r for r in roots if r.name == "condA")
    mouse1 = cast(BehaviorNode, condA.children[0])
    day001 = cast(BehaviorNode, mouse1.children[0])

    # ancestor()
    assert day001.ancestor("mouse") is mouse1
    assert day001.ancestor("cond") is condA
    assert condA.ancestor("day") is None

    # auto level properties on BehaviorNode
    assert day001.mouse == mouse1
    assert day001.cond == condA
    assert day001.day is day001  # leaf refers to itself


def test_build_tree_generic_structure(dummy_fs: Path) -> None:
    """The builder yields the expected number of nodes and structure."""
    level_specs = [__make_cond_spec(), __make_mouse_spec(), __make_day_behavior_spec()]
    roots: list[BehaviorNode] = __build_tree_generic(
        root=dummy_fs,
        level_specs=level_specs,
        node_class=BehaviorNode,
    )
    assert len(roots) == 2  # condA, condB
    condA = next(r for r in roots if r.name == "condA")
    condB = next(r for r in roots if r.name == "condB")

    # condA -> one mouse, two days
    mouse_children = condA.children
    assert len(mouse_children) == 1
    mouse = mouse_children[0]
    # **見つかった名前のまま返すこと。** ここは以前 ["day001_", "day002_"] を
    # 期待していた —— 末尾に "_" の無い day を _preprocess_day が **改名** して
    # いた頃の期待である。その改名は意図的に止めてあり (crawler.py の
    # _preprocess_day は中身がコメントアウトされたまま d を返す)、読み取りだけの
    # 走査が利用者のディレクトリ名を書き換えないようになっている。
    # 実装ではなく期待のほうが古い。
    assert sorted(ch.name for ch in mouse.children) == ["day001", "day002_"]

    # condB -> mouseX -> one day
    assert len(condB.children) == 1
    assert len(condB.children[0].children) == 1


def test_generic_crawler_trace(dummy_fs: Path) -> None:
    """GenericCrawler walks nodes depth-first and calls kernel hooks."""
    level_specs = [__make_cond_spec(), __make_mouse_spec(), __make_day_behavior_spec()]
    roots: list[BehaviorNode] = __build_tree_generic(
        root=dummy_fs,
        level_specs=level_specs,
        node_class=BehaviorNode,
    )

    kernel = TraceKernel()
    # 走査の設定は CrawlContext にまとまった (以前は project_dir / overwrite を
    # GenericCrawler が直接受け取っていた)。呼び出し側 (slice-analysis の QC など)
    # と同じ組み立て方をする。
    crawler = GenericCrawler(
        kernels=[kernel],
        ctx=CrawlContext(analysis_param=None, project_dir=dummy_fs,
                         arg=ArgModel(overwrite=False)),
    )
    crawler.crawl_from_nodes(roots)

    # Basic ordering assertions
    assert kernel.trace[0] == "proj_start"
    assert kernel.trace[-1] == "proj_end"

    # Ensure at least every node produced by build_tree_generic is visited
    produced_nodes = []

    def _collect(n: Any) -> None:
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
    # 名前は改名されずそのまま (test_build_tree_generic_structure のコメント参照)。
    assert df_names[:3] == ["condA", "mouse1", "day001"]
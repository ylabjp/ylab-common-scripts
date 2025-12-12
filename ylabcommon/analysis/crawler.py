from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    TypeVar,
)
from ylabcommon.models.parameters.general import  ArgModel
from abc import ABC


# ============================================================
# 汎用ツリーノード
# ============================================================

# クラス,役割,性質
# HierNode,
#            データそのもの (Instance),ツリー上に数百〜数千個作られる。メモリを軽く保つべき。
# LevelSpec,
#           構築のレシピ (Schema/Factory),"階層レベルの数だけ（cond, mouse, dayの3つなど）
#           しか存在しない。「どうやって探すか」「どう読み込むか」というロジックを持つ。"


@dataclass
class HierNode:
    """
    汎用階層ノード。

    - name   : ノード名（ディレクトリ名など）
    - path   : 対応するパス
    - level  : "cond" / "mouse" / "day" など任意のレベル名
    - parent : 親ノード
    - children: 子ノードのリスト
    - payload: exp_param など解析に必要なメタ情報を格納する自由な dict
    """
    name: str
    path: Path
    level: str
    parent: Optional["HierNode"] = None
    children: List["HierNode"] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)

    def ancestor(self, level: str) -> Optional["HierNode"]:
        """
        指定した level の先祖ノードを返す。存在しなければ None。
        """
        node: Optional[HierNode] = self
        while node is not None:
            if node.level == level:
                return node
            node = node.parent
        return None

    def _get_ancestor_node(self, level: str) -> Optional["HierNode"]:
            """共通ロジック: 指定したレベルの祖先を取得し、HierNode型かチェックして返す"""
            n = self.ancestor(level)
            return n if isinstance(n, HierNode) else None


# ============================================================
# LevelSpec: 各階層のルール（glob/filter/preprocess/load_payload）
# ============================================================

@dataclass
class LevelSpec:
    """
    階層ごとのルール定義。

    - level         : "cond", "mouse", "day" などのレベル名
    - pattern       : base_path.glob(pattern) で子ディレクトリ候補を列挙する
    - filter_dir    : 候補ディレクトリを採用するかどうか判定する関数
    - preprocess_dir: 採用前に rename などを行う関数
    - load_payload  : payload(dict) を生成する関数
    """
    level: str
    pattern: str
    filter_dir: Callable[[Path], bool]
    preprocess_dir: Callable[[Path], Path]
    load_payload: Callable[[Path], Dict[str]]


# ============================================================
# 汎用ツリービルダー (再帰)
# ============================================================

N = TypeVar("N", bound=HierNode)

def __build_tree_generic(
    root: Path,
    level_specs: List[LevelSpec],
    node_factory: Callable[[str, Path, str, Optional[N], Dict[str, Any]], N],
) -> List[N]:
    """
    root 配下を level_specs にしたがって再帰的に走査し、
    node_factory で生成した HierNode(N) の木を構築して返す。
    戻り値は最上位レベル (level_specs[0].level) のノードのリスト。
    """

    def _build_level(parent: Optional[N], base_path: Path, depth: int) -> List[N]:
        if depth >= len(level_specs):
            return []

        spec = level_specs[depth]
        nodes: List[N] = []

        for d in sorted(base_path.glob(spec.pattern)):
            if not d.is_dir():
                continue
            if not spec.filter_dir(d):
                continue

            # 前処理（rename など）
            d2 = spec.preprocess_dir(d)

            # payload を作成（exp_param など）
            payload = spec.load_payload(d2)

            node = node_factory(d2.name, d2, spec.level, parent, payload)
            node.children = _build_level(node, d2, depth + 1)
            nodes.append(node)

        return nodes

    return _build_level(parent=None, base_path=root, depth=0)


# ============================================================
# Node ベース Kernel / Crawler（レベル非依存）
# ============================================================

# TODO これを共通化＋解析種類ごとに継承して調整する
@dataclass
class CrawlContext:
    """
    解析全体で共通のコンテキスト。
    - analysis_param: CC_Analysis_Param など
    - project_dir   : プロジェクトルート
    - arg     : 
    """
    analysis_param: Any
    project_dir: Path
    arg: ArgModel 

class GenericKernel(ABC):
    """
    HierNode もしくはそのサブクラスを前提とした汎用 Kernel 抽象クラス。

    ※ここでは level 固有の on_cond / on_mouse / on_day などは定義しない。
      必要であれば on_node 内部で node.level を見て分岐する。
    """

    # プロジェクト全体
    def on_project_start(self, ctx: CrawlContext, roots: Sequence[HierNode]) -> None:
        pass

    def on_project_end(self, ctx: CrawlContext, roots: Sequence[HierNode]) -> None:
        pass

    # ノードごと
    def on_node(self, ctx: CrawlContext, node: HierNode) -> None:
        """
        すべてのノードに対して呼ばれる共通フック。
        cond/mouse/day などの level に依存した処理は、
        ここで node.level を見て分岐してもよい。
        """
        pass

    # ファイルレベル
    def retrieve_file_with_pattern(self, node: HierNode) -> str:
        """
        Nodeごとにファイルの状態を調べる。
        目的とするデータセットが揃っているか判定する。
        node.path.glob() で使うパターン。
        例: level=="day" のときのみ 'result*.csv' を返す、など。
        空文字列を返した場合、その node ではファイル処理を行わない。
        """
        return ""

    def on_file(self, ctx: CrawlContext, node: HierNode, file_path: Path) -> None:
        """
        retrieve_file_with_pattern() でマッチしたファイルごとに呼ばれるフック。
        """
        pass

class GenericCrawler:
    """
    HierNode / BehaviorNode ツリーを走査し、GenericKernel を呼び出す汎用 Crawler。
    """

    def __init__(
        self,
        kernels: Sequence[GenericKernel],
        ctx: CrawlContext,
    ) -> None:
        self.kernels = list(kernels)
        self.ctx = ctx


    def crawl_from_nodes(self, roots: Sequence[HierNode]) -> None:
        """
        すでに build_tree_generic 済みの roots（最上位ノード群）から解析を開始する。
        """
        # プロジェクト開始
        for k in self.kernels:
            k.on_project_start(self.ctx, roots)

        # 各 root を再帰的にたどる
        for root in roots:
            self._walk_node( root)

        # プロジェクト終了
        for k in self.kernels:
            k.on_project_end(self.ctx, roots)

    def _walk_node(self,node: HierNode) -> None:
        # 各 Kernel に対して node フック & file フック
        for k in self.kernels:
            k.on_node(self.ctx, node)

            pattern = k.retrieve_file_with_pattern(node)
            if pattern:
                for f in sorted(node.path.glob(pattern)):
                    # プロジェクト固有のフィルタ（例: 'attach' 除外）があれば
                    # Kernel 側でやる or ここに書く
                    k.on_file(self.ctx, node, f)

        # 子ノードを再帰
        for child in node.children:
            self._walk_node( child)


def __filter_dir_basic(d: Path) -> bool:
    name = d.name
    if not d.is_dir():
        return False
    if name[0] in ("_", "@"):
        return False
    return True

# ============================================================
# BehaviorNode
#    cond/mouse/day などへのショートカットを持つが、
#    実装は property factory で DRY に記述
# ============================================================

class BehaviorNode(HierNode):
    """
    マウス行動実験など、cond/mouse/day 階層を扱うときに使うノード。
    """

# # cond / mouse / day プロパティを DRY に定義
# for _lvl in ("cond", "mouse", "day"):
#     setattr(BehaviorNode, _lvl, _make_level_property(_lvl))
    @property
    def cond(self) -> Optional["HierNode"]:
        return self._get_ancestor_node("cond")

    @property
    def mouse(self) -> Optional["HierNode"]:
        return self._get_ancestor_node("mouse")

    @property
    def day(self) -> Optional["HierNode"]:
        return self._get_ancestor_node("day")
    

def __make_cond_spec() -> LevelSpec:

    def _identity_preprocess(d: Path) -> Path:
        return d

    def _empty_payload(d: Path) -> Dict[str, Any]:
        return {}
    return LevelSpec(
        level="cond",
        pattern="cond*",
        filter_dir=__filter_dir_basic,
        preprocess_dir=_identity_preprocess,
        load_payload=_empty_payload,
    )

def __make_mouse_spec() -> LevelSpec:
    def _identity_preprocess(d: Path) -> Path:
        return d

    def _empty_payload(d: Path) -> Dict[str, Any]:
        return {}

    return LevelSpec(
        level="mouse",
        pattern="*",
        filter_dir=__filter_dir_basic,
        preprocess_dir=_identity_preprocess,
        load_payload=_empty_payload,
    )

def __make_day_spec() -> LevelSpec:
    """
    day* ディレクトリを対象とし、
    - "_" が含まれないものはリネームして補正
    - analysis_param.get_exp_param(...) を payload["exp_param"] に格納
    """

    def _preprocess_day(d: Path) -> Path:
        if len(d.name.split("_")) < 2:
            old = d
            new = d.with_name(d.name + "_")
            # CC_Analysis_Param を想定して log_warn などを呼べるなら呼ぶ
            if hasattr(param, "log_warn"):
                param.log_warn(f"Inappropriate day name: {old}")
            old.rename(new)
            if hasattr(param, "log_warn"):
                param.log_warn(f"Renamed: {new}")
            return new
        return d

    def _load_day_payload(d: Path) -> Dict[str, Any]:
        exp_param = None
        if hasattr(param, "get_exp_param"):
            try:
                exp_param = param.get_exp_param(str(d))
            except Exception as e:
                if hasattr(param, "log_exception"):
                    param.log_exception(e)
        return {"exp_param": exp_param}

    return LevelSpec(
        level="day",
        pattern="day*",
        filter_dir=__filter_dir_basic,
        preprocess_dir=_preprocess_day,
        load_payload=_load_day_payload,
    )

def __behavior_node_factory(
    name: str,
    path: Path,
    level: str,
    parent: Optional[BehaviorNode],
    payload: Dict[str, Any],
) -> BehaviorNode:
    return BehaviorNode(
        name=name,
        path=path,
        level=level,
        parent=parent,
        payload=payload,
    )

def build_behavior_tree(prj_root:Path, analysis_param:Any)->List[BehaviorNode]:
    level_specs = [__make_cond_spec(), __make_mouse_spec(), __make_day_spec()]
    nodes: List[BehaviorNode] = __build_tree_generic(
        root=prj_root,
        analysis_param=analysis_param,
        level_specs=level_specs,
        node_factory=__behavior_node_factory,
    )

    return nodes


# ============================================================
# SliceNode
#    cond/cell などへのショートカットを持つが、
#    実装は property factory で DRY に記述
# ============================================================

class SlicePrjNode(HierNode):
    """
    cond/cell 階層を扱うときに使うノード。
    """
    @property
    def cond(self) -> Optional["HierNode"]:
        return self._get_ancestor_node("cond")

    @property
    def cell(self) -> Optional["HierNode"]:
        return self._get_ancestor_node("cell")





def __make_cell_spec() -> LevelSpec:
    def _identity_preprocess(d: Path) -> Path:
        return d

    def _empty_payload(d: Path) -> Dict[str, Any]:
        return {}


    return LevelSpec(
        level="cell",
        pattern="*XY*",
        filter_dir=__filter_dir_basic,
        preprocess_dir=_identity_preprocess,
        load_payload=_empty_payload,
    )

def __slice_node_factory(
    name: str,
    path: Path,
    level: str,
    parent: Optional[SlicePrjNode],
    payload: Dict[str, Any],
) -> SlicePrjNode:
    return SlicePrjNode(
        name=name,
        path=path,
        level=level,
        parent=parent,
        payload=payload,
    )


    


def build_slice_tree(prj_root:Path)->List[SlicePrjNode]:
    level_specs = [__make_cond_spec(), __make_cell_spec()]
    nodes: List[SlicePrjNode] = __build_tree_generic(
        root=prj_root,
        level_specs=level_specs,
        node_factory=__slice_node_factory,
    )

    return nodes

# ============================================================
# 7. 使用例（コメントアウト）
# ============================================================
#
# from dlc.Analysis_Param import CC_Analysis_Param
#
# analysis_param = CC_Analysis_Param()
# analysis_param.set_project_param(config_json_path)
# prj_root = Path(analysis_param.get_prj_root_dir())
#
#
# class FreezingKernel(GenericKernel):
#     def __init__(self) -> None:
#         self._rows = []
#
#     def retrieve_file_with_pattern(self, node: HierNode) -> str:
#         # 例: day レベルのみ result*.csv を対象
#         return "result*.csv" if node.level == "day" else ""
#
#     def on_file(self, ctx: CrawlContext, node: HierNode, file_path: Path) -> None:
#         import pandas as pd
#
#         df = pd.read_csv(file_path)
#         freezing_ratio = df["freezing_time"].sum() / df["total_time"].sum()
#
#         # BehaviorNode であれば cond/mouse プロパティが使える
#         if isinstance(node, BehaviorNode):
#             cond_name = node.cond.name if node.cond else None
#             mouse_name = node.mouse.name if node.mouse else None
#         else:
#             cond_name = node.ancestor("cond").name if node.ancestor("cond") else None
#             mouse_name = node.ancestor("mouse").name if node.ancestor("mouse") else None
#
#         self._rows.append({
#             "cond": cond_name,
#             "mouse": mouse_name,
#             "day": node.name,
#             "file": str(file_path),
#             "freezing_ratio": freezing_ratio,
#         })
#
#     def on_project_end(self, ctx: CrawlContext, roots: Sequence[HierNode]) -> None:
#         if not self._rows:
#             return
#         import pandas as pd
#         df = pd.DataFrame(self._rows)
#         out_path = ctx.project_dir / "freezing_summary.csv"
#         df.to_csv(out_path, index=False)
#         if hasattr(ctx.analysis_param, "log_info"):
#             ctx.analysis_param.log_info(f"Saved freezing summary to: {out_path}")
#
# ctx = CrawlContext(analysis_param,prj_root,overwrite)
# crawler = GenericCrawler(
#     kernels=[FreezingKernel()],
#       ctx=ctx
#
# )
#
# crawler.crawl_from_nodes(cond_nodes)
#
# ============================================================


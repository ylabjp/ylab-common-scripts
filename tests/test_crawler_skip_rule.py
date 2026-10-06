"""crawler が読まないフォルダの決まり (analysis/crawler.py の「読まないフォルダ」)。

守るのは 3 点。

1. 人の退避 (``_``) とデータではないフォルダ (``@eaDir``、``#recycle``、``#snapshot``) には、
   crawler はどの段でも降りない。**``#`` で始まるデータのフォルダ (``#775``) には降りる**
2. 深さを決めずに探す :func:`find_in_tree` は、同じ規則で降りない。降りない以外は
   「rglob してから外す」と同じ答え
3. :func:`slice_prj_session_dirs` は :func:`build_slice_prj_tree` の葉そのもの
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ylabcommon.analysis.crawler import (
    build_slice_prj_tree,
    build_slice_raw_tree,
    find_in_tree,
    is_set_aside_name,
    is_skipped_name,
    is_skipped_path,
    is_system_name,
    slice_prj_session_dirs,
)


def _tree(root: Path, dirs: tuple[str, ...] = (), files: tuple[str, ...] = ()) -> Path:
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("x", encoding="utf-8")
    return root


# ---- 名前 -------------------------------------------------------------------------------

def test_set_aside_and_system_names_are_told_apart() -> None:
    assert is_set_aside_name("_XYZ-T_Cell02_1_TNFa_needs-clarification")
    assert is_set_aside_name("_before_redo_20260924")
    assert is_system_name("@eaDir") and is_system_name("#recycle") and is_system_name("#snapshot")
    assert not is_system_name("_x")
    # 退避も見る道具でも、データではないフォルダには降りない
    assert is_skipped_name("@eaDir", include_set_aside=True)
    assert is_skipped_name("_x") and not is_skipped_name("_x", include_set_aside=True)


@pytest.mark.parametrize("name", ["#775", "#824-826", "cond_A", "261004_XYZ-T_Cell01_1_TNFa"])
def test_data_folders_are_read_even_when_they_start_with_a_hash(name: str) -> None:
    """raw-slice の Keyence の木には個体番号の ``#775`` / ``#824-826`` がある (2026-10-06)。"""
    assert not is_skipped_name(name)
    assert not is_system_name(name)


def test_only_the_levels_below_the_root_decide() -> None:
    root = Path("/data/_archive/prj")
    assert not is_skipped_path(root / "cond_A" / "s1", root)
    assert is_skipped_path(root / "cond_A" / "_s1", root)
    assert is_skipped_path(root / "cond_A" / "s1" / "@eaDir", root)
    assert not is_skipped_path(root / "cond_A" / "_s1", root, include_set_aside=True)
    assert not is_skipped_path(Path("/elsewhere/_x"), root), "root の外は判定しない"


# ---- crawler の木 -------------------------------------------------------------------------

def test_the_raw_tree_skips_set_aside_and_system_folders_but_not_hash_data(tmp_path: Path) -> None:
    root = _tree(tmp_path / "raw", (
        "cond_A/261004_TNFa/XYZ-T_Cell01_1_TNFa",
        "cond_A/261004_TNFa/_XYZ-T_Cell02_1_TNFa_needs-clarification",
        "cond_A/261004_TNFa/@eaDir",
        "cond_A/#775/XYZ-T_Cell01_1_x",          # 個体番号の日フォルダ (データ)
        "cond_A/#recycle/XYZ-T_Cell01_1_x",      # Synology のごみ箱
        "cond_A/_old/XYZ-T_Cell01_1_x",
    ))

    cells = [c.path.relative_to(root).as_posix()
             for cond in build_slice_raw_tree(root) for day in cond.children for c in day.children]

    assert cells == ["cond_A/#775/XYZ-T_Cell01_1_x", "cond_A/261004_TNFa/XYZ-T_Cell01_1_TNFa"]


def test_slice_prj_session_dirs_are_the_leaves_of_the_prj_tree(tmp_path: Path) -> None:
    root = _tree(tmp_path / "prj", (
        "cond_A/240101_XYZ-T_Cell01_1", "cond_A/240101_XYZ-T_Cell02_1",
        "cond_A/_240101_XYZ-T_Cell03_1", "cond_A/@eaDir", "cond_A/notes",
        "cond_B/240102_XYT_Cell01_1", "_cond_C/240103_XYZ-T_Cell01_1",
        "figures/240104_XYZ-T_Cell01_1", "cond_D",
    ))

    leaves = [c.path for cond in build_slice_prj_tree(root) for c in cond.children]

    assert slice_prj_session_dirs(root) == leaves
    assert slice_prj_session_dirs(str(root)) == leaves, "文字列のパスも受ける"
    assert [p.name for p in leaves] == [
        "240101_XYZ-T_Cell01_1", "240101_XYZ-T_Cell02_1", "240102_XYT_Cell01_1"]
    assert slice_prj_session_dirs(tmp_path / "nowhere") == []


# ---- 深さを決めずに探す ------------------------------------------------------------------

DEEP = (
    "cond_A/s1/roi.json",
    "cond_A/s1/_before_redo_20260924/roi.json",
    "cond_A/s1/@eaDir/roi.json",
    "cond_A/_s2/roi.json",
    "regrouped/deeper/cond_X/s3/roi.json",
    "#recycle/cond_A/s4/roi.json",
    "#775/s7/roi.json",                     # # で始まるデータ
    "cond_A/s5/ROI.json",                   # 大文字小文字は区別する (rglob と同じ)
)


def test_find_in_tree_does_not_go_below_skipped_folders(tmp_path: Path) -> None:
    root = _tree(tmp_path / "prj", files=DEEP)

    assert [p.relative_to(root).as_posix() for p in find_in_tree(root, "roi.json")] == [
        "#775/s7/roi.json", "cond_A/s1/roi.json", "regrouped/deeper/cond_X/s3/roi.json"]
    assert [p.relative_to(root).as_posix() for p in find_in_tree(root, "roi.json", True)] == [
        "#775/s7/roi.json", "cond_A/_s2/roi.json", "cond_A/s1/_before_redo_20260924/roi.json",
        "cond_A/s1/roi.json", "regrouped/deeper/cond_X/s3/roi.json"]


def test_a_skipped_folder_itself_is_found_by_its_name(tmp_path: Path) -> None:
    root = _tree(tmp_path / "prj", ("cond_A/s1/_before_redo_1/_before_redo_nested",
                                    "cond_A/_s2/_before_redo_2"))

    found = find_in_tree(root, "_before_redo_*")

    assert [p.relative_to(root).as_posix() for p in found] == ["cond_A/s1/_before_redo_1"]


@pytest.mark.parametrize("pattern", ["roi.json", "*.json", "s*", "*"])
@pytest.mark.parametrize("include_set_aside", [False, True])
def test_find_in_tree_gives_the_same_answer_as_rglob_then_filter(
        tmp_path: Path, pattern: str, include_set_aside: bool) -> None:
    root = _tree(tmp_path / "prj", ("cond_A/s6/@eaDir/sub",), DEEP)

    expected = sorted(p for p in root.rglob(pattern)
                      if not is_skipped_path(p.parent, root, include_set_aside))

    assert find_in_tree(root, pattern, include_set_aside) == expected


def test_a_missing_root_has_nothing(tmp_path: Path) -> None:
    assert find_in_tree(tmp_path / "nowhere", "*") == []

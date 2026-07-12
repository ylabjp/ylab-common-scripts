import pytest

from ylabcommon.models.parameters.behavior import (
    DLC_BODY_PARTS,
    HEAD,
    BODY,
    DLCParam,
    detect_dlc_bodyparts_version,
    get_dlc_body_part_names,
    get_dlc_parts_for_region,
)


@pytest.mark.parametrize("version", list(DLC_BODY_PARTS.keys()))
def test_detect_roundtrip(version):
    """各バージョンの全 part 名から、そのバージョンが判定できる(順序非依存)。"""
    names = get_dlc_body_part_names(version)
    assert detect_dlc_bodyparts_version(names) == version
    # 順序に依存しないこと
    assert detect_dlc_bodyparts_version(list(reversed(names))) == version
    # set を渡しても判定できること
    assert detect_dlc_bodyparts_version(set(names)) == version


def test_detect_unknown_raises():
    with pytest.raises(ValueError):
        detect_dlc_bodyparts_version(["foo", "bar", "baz"])


def test_detect_partial_match_raises():
    """part が1つ欠けても既知バージョンとは一致せずエラーになる。"""
    names = get_dlc_body_part_names(2025)[:-1]
    with pytest.raises(ValueError):
        detect_dlc_bodyparts_version(names)


def test_roles_are_subset_of_all_parts():
    for version in DLC_BODY_PARTS:
        names = set(get_dlc_body_part_names(version))
        head = get_dlc_parts_for_region(version, HEAD)
        body = get_dlc_parts_for_region(version, BODY)
        assert set(head) <= names
        assert set(body) <= names
        # head と body は重複しない
        assert set(head).isdisjoint(set(body))
        # いずれの region にも少なくとも1つは割り当てられている
        assert len(head) > 0
        assert len(body) > 0


def test_2020_roles():
    assert get_dlc_parts_for_region(2020, HEAD) == ["left_ear", "right_ear", "snout"]
    assert get_dlc_parts_for_region(2020, BODY) == [
        "centroid",
        "left_lateral",
        "right_lateral",
        "tail_base",
    ]


def test_2025_roles():
    assert get_dlc_parts_for_region(2025, HEAD) == [
        "snout",
        "right_ear",
        "left_ear",
        "head_top",
    ]
    assert get_dlc_parts_for_region(2025, BODY) == [
        "tail_base",
        "rump_center",
        "centroid",
        "chest_center",
    ]


def test_2026_roles_symmetric_and_no_centroid():
    # 2026 には centroid が存在しない
    assert "centroid" not in get_dlc_body_part_names(2026)
    assert get_dlc_parts_for_region(2026, HEAD) == [
        "head_midpoint",
        "right_ear",
        "left_ear",
        "right_eye",
        "left_eye",
        "nose",
    ]
    body = get_dlc_parts_for_region(2026, BODY)
    assert body == [
        "right_hip",
        "right_midside",
        "right_shoulder",
        "left_hip",
        "left_midside",
        "left_shoulder",
        "mid_backend",
        "mouse_center",
        "mid_back",
    ]
    # 左右対称 (hip/midside/shoulder が左右そろっている)
    for part in ("hip", "midside", "shoulder"):
        assert ("right_" + part) in body
        assert ("left_" + part) in body
    # tail 系の点は body_center に含めない
    assert not any(p.startswith("tail") for p in body)


def test_dlcparam_version_override():
    p = DLCParam(bodyparts_version=2020)
    # 引数なしは self.bodyparts_version を使う
    assert p.get_dlc_parts_for_head_center() == get_dlc_parts_for_region(2020, HEAD)
    # 引数で明示指定したバージョンが優先される
    assert p.get_dlc_parts_for_head_center(2026) == get_dlc_parts_for_region(2026, HEAD)
    assert p.get_dlc_body_parts_all(2025) == get_dlc_body_part_names(2025)

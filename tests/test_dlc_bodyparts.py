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


# ── 2026-09-06 追加: ドライブに実在する旧モデルの出力を判定できること ──
#   移行中は旧 h5 も解析するので、実測した part 名の集合をそのまま置く。
#   （集合はドライブ上の h5 を model 名ごとに読んで確かめたもの）
LEGACY_OBSERVED = {
    # DLC_resnet50_OAVT-general4Feb12shuffle1
    2019: ["leftear", "rightear", "snout", "tailbase"],
    # DLC_resnet50_NB07Sep11shuffle1 など
    2020: ["centroid", "left_ear", "left_lateral", "right_ear",
           "right_lateral", "snout", "tail_base"],
    # DLC_resnet50_homecage_8BPSep11shuffle1
    2021: ["centroid", "left_ear", "left_lateral", "right_ear",
           "right_lateral", "snout", "tail_base", "tail_end"],
    # DLC_resnet50_NB06DevFeb25shuffle1 / NB05-homecage-headgearJun30shuffle1
    2023: ["centroid", "head_gear", "left_ear", "left_lateral",
           "right_ear", "right_lateral", "snout", "tail_base"],
}


@pytest.mark.parametrize("version,observed", sorted(LEGACY_OBSERVED.items()))
def test_legacy_models_are_detected(version, observed):
    """ドライブに実在する旧モデルの出力から、正しいバージョンが判定できる。"""
    assert detect_dlc_bodyparts_version(observed) == version


def test_head_center_is_same_between_2020_2021_2023():
    """8 点版で足した tail_end / head_gear は重心に入れない。

    head_center / body_center の定義が 2020 と一致していれば、旧データ同士を
    比較できる。tail_end は尾で、head_gear は頭に載せた器具の目印なので、
    どちらも解剖学的な重心には入れない。
    """
    for v in (2021, 2023):
        assert set(get_dlc_parts_for_region(v, HEAD)) == set(
            get_dlc_parts_for_region(2020, HEAD)
        )
        assert set(get_dlc_parts_for_region(v, BODY)) == set(
            get_dlc_parts_for_region(2020, BODY)
        )

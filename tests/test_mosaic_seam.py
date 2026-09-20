"""継ぎ目の道具: ずれを測る・釣り合わせる・重ねる。

しきい値は測って決めたものなので (mosaic_seam の docstring)、ここでは
「本物らしい重なり」と「使えない重なり」の両方を作って、境目の側で振る舞いを
留める。通らない側を**採らない**ことがこのモジュールの肝である。
"""
import numpy as np
import pytest

from ylabcommon.bioio.core.mosaic_seam import (
    MIN_CORRELATION,
    MIN_PEAK,
    PairShift,
    blend_into,
    correlation_of,
    edge_ratio,
    estimate_shift,
    measure_pair,
    solve_offsets,
)

RNG = np.random.default_rng(20260920)


def _texture(height=64, width=64):
    """位相相関が効く程度のテクスチャ。実タイルの代わり。"""
    coarse = RNG.normal(3000, 400, (height // 4 + 2, width // 4 + 2))
    grown = np.repeat(np.repeat(coarse, 4, axis=0), 4, axis=1)
    return np.clip(grown[:height, :width] + RNG.normal(0, 60, (height, width)),
                   0, 65535).astype(np.uint16)


def _pair_from(field, dx, dy=0, band=48):
    """同じ場所を、``(dy, dx)`` だけずれて切り出した 2 枚。"""
    a = field[20:20 + band, 20:20 + band]
    b = field[20 + dy:20 + dy + band, 20 + dx:20 + dx + band]
    return a, b


# --- ずれを測る ----------------------------------------------------------

@pytest.mark.parametrize("dy,dx", [(0, 5), (0, -3), (4, 0), (-2, 6)])
def test_a_known_shift_comes_back(dy, dx):
    field = _texture(128, 128)
    a, b = _pair_from(field, dx, dy)

    got_dy, got_dx, peak = estimate_shift(a, b)

    assert (got_dy, got_dx) == (dy, dx)
    assert peak > MIN_PEAK


def test_the_shift_survives_a_brightness_difference(tmp_path):
    """**ここが位相相関を使う理由。**

    隣り合うタイルは明るさの尺度が 0.7〜0.78 倍ずれている (実測)。振幅を捨てて
    位相だけ見るので、その状態でもずれは測れる。
    """
    field = _texture(128, 128)
    a, b = _pair_from(field, 5)
    dark = (b.astype(np.float64) * 0.7).astype(np.uint16)

    assert estimate_shift(a, dark)[:2] == estimate_shift(a, b)[:2] == (0, 5)


def test_bands_of_different_shapes_are_refused():
    with pytest.raises(ValueError, match="same non-empty shape"):
        estimate_shift(np.zeros((4, 4), np.uint16), np.zeros((4, 5), np.uint16))


# --- 採る / 採らない -----------------------------------------------------

def test_a_usable_overlap_is_accepted():
    field = _texture(128, 128)
    a, b = _pair_from(field, 5)

    pair = measure_pair("a", "b", a, b)

    assert pair.accepted and pair.reason == ""
    assert (pair.dy, pair.dx) == (0, 5)
    assert pair.correlation_after > pair.correlation_before
    assert pair.correlation_after > MIN_CORRELATION


def test_a_flat_overlap_is_not_used():
    """背景しか写っていない重なり。動かす根拠が無い。"""
    flat = np.full((48, 48), 1200, np.uint16)

    pair = measure_pair("a", "b", flat, flat)

    assert not pair.accepted
    assert "peak" in pair.reason


def test_two_unrelated_bands_are_not_used():
    pair = measure_pair("a", "b", _texture(48, 48), _texture(48, 48))

    assert not pair.accepted
    assert pair.reason


def test_a_shift_too_large_to_be_a_refinement_is_not_used():
    """重なりの幅に比べて大きいずれは、微調整ではなく別の合わせ方である。

    **ちゃんと測れているのに採らない**ところを見る。測れていないから採らない
    のは別の条件 (上の 2 つ) なので、ここでは相関が出る大きさのずれを使う。
    """
    field = _texture(160, 160)
    a, b = _pair_from(field, 20, band=64)   # 上限は 64*0.25 = 16 px

    pair = measure_pair("a", "b", a, b, max_shift_fraction=0.25)

    assert (pair.dy, pair.dx) == (0, 20)          # 測れてはいる
    assert pair.correlation_after > MIN_CORRELATION
    assert not pair.accepted
    assert "not a refinement" in pair.reason


def test_the_intensity_ratio_is_measured_but_not_corrected():
    """**記録するだけ。** 直すには視野内の傾きの模型がいる (まだ無い)。"""
    field = _texture(128, 128)
    a, b = _pair_from(field, 5)
    dark = (b.astype(np.float64) * 0.7).astype(np.uint16)

    pair = measure_pair("a", "b", a, dark)

    assert pair.accepted
    assert 0.65 < pair.intensity_ratio < 0.75
    # 画素は返ってこない —— このモジュールは測るだけで、値を変えない
    assert not hasattr(pair, "corrected")


# --- 釣り合わせる --------------------------------------------------------

def _shift(a, b, dy, dx):
    return PairShift(a=a, b=b, dy=dy, dx=dx, peak=1.0, correlation_before=0.5,
                     correlation_after=1.0, intensity_ratio=1.0, accepted=True)


def test_offsets_accumulate_along_a_row():
    """1 列に並んだ取得。隣どうし +5 なら 0, 5, 10 になる。"""
    offsets = solve_offsets(["a", "b", "c"],
                            [_shift("a", "b", 0, 5), _shift("b", "c", 0, 5)])

    assert offsets == {"a": (0, 0), "b": (0, 5), "c": (0, 10)}


def test_a_loop_that_does_not_close_is_balanced_not_picked():
    """2 次元に並ぶと制約が矛盾する。どれか 1 つを採らずに、まとめて釣り合わせる。

    a->b が +6、a->c が 0、c->b が 0 で、b の値が 6 と 0 で食い違う。
    最小二乗なので、b はそのあいだに落ちる。
    """
    offsets = solve_offsets(["a", "b", "c"], [
        _shift("a", "b", 0, 6), _shift("a", "c", 0, 0), _shift("c", "b", 0, 0)])

    assert offsets["a"] == (0, 0)
    # b は 6 (a 経由) と 0 (c 経由) の食い違いのあいだに落ちる。どちらか一方を
    # 採ったのなら 6 か 0 になっているはずで、そうなっていないことを見る。
    assert 0 < offsets["b"][1] < 6
    assert offsets["c"][1] < offsets["b"][1]


def test_nothing_usable_means_nothing_moves():
    """採れるペアが無ければ、ステージ座標のまま。**でたらめに動かさない。**"""
    rejected = PairShift(a="a", b="b", dy=0, dx=9, peak=0.0,
                         correlation_before=0.0, correlation_after=0.0,
                         intensity_ratio=None, accepted=False, reason="peak")

    assert solve_offsets(["a", "b"], [rejected]) == {"a": (0, 0), "b": (0, 0)}


def test_offsets_are_whole_pixels():
    """小数で動かすと画素を作り直すことになる。測った値が測っていない値に変わる。"""
    offsets = solve_offsets(["a", "b", "c"],
                            [_shift("a", "b", 0, 1), _shift("b", "c", 0, 2)])

    assert all(isinstance(v, int) for shift in offsets.values() for v in shift)


# --- 重ねる -------------------------------------------------------------

def test_last_wins_overwrites():
    canvas = np.zeros((4, 4), np.uint16)
    blend_into(canvas, None, np.full((4, 4), 10, np.uint16), (slice(None),), "last")
    blend_into(canvas, None, np.full((4, 4), 20, np.uint16), (slice(None),), "last")

    assert np.all(canvas == 20)


def test_mean_averages_what_was_written():
    canvas = np.zeros((4, 4), np.uint16)
    counts = np.zeros((4, 4), np.uint16)
    for value in (10, 20, 30):
        blend_into(canvas, counts, np.full((4, 4), value, np.uint16),
                   (slice(None),), "mean")

    assert np.all(canvas == 20)          # (10+20+30)/3
    assert np.all(counts == 3)


def test_mean_without_counts_is_refused():
    with pytest.raises(ValueError, match="counts"):
        blend_into(np.zeros((2, 2), np.uint16), None,
                   np.zeros((2, 2), np.uint16), (slice(None),), "mean")


def test_an_unknown_policy_is_refused():
    with pytest.raises(ValueError, match="unknown overlap policy"):
        blend_into(np.zeros((2, 2), np.uint16), None,
                   np.zeros((2, 2), np.uint16), (slice(None),), "feather")


# --- 視野内の傾き -------------------------------------------------------

def test_the_edge_ratio_reports_a_gradient():
    """左から右へ 2 倍になる視野なら、右端/左端 は 1 より大きく出る。"""
    ramp = np.linspace(1000, 2000, 64)[None, :] * np.ones((32, 1))

    assert edge_ratio(ramp.astype(np.uint16), 16) > 1.4


def test_a_flat_field_has_an_edge_ratio_of_one():
    assert edge_ratio(np.full((32, 64), 1500, np.uint16), 16) == pytest.approx(1.0)


def test_correlation_of_a_flat_band_is_zero_not_an_error():
    flat = np.full((8, 8), 5, np.uint16)

    assert correlation_of(flat, flat) == 0.0

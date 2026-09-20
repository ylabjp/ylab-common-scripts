"""継ぎ目をどうするか —— 重なり帯から測れるもの、測れないもの。

モザイクのタイルはステージ座標で置くが、その座標は**画像の中身とぴったりでは
ない**。ずれは重なり帯から推定できる。一方で照明むらは 1 回の取得からは
分離できない。ここは**キャリブレーションが無くてもできる分**だけをやる。

実測 (keyence-microscopy/test/data/XY01、480x360 のタイル 3 枚、重なり 144 px
= 30%、CH1 と CH3)。

ずれは測れる
------------
全 4 ペア・両チャネルで ``dx=+5, dy=0``。当てると帯の相関が上がる::

    CH1  0.885 -> 0.931
    CH3  0.644 -> 0.940

取得ごとに推定できるので、保存したキャリブレーションは要らない。**整数画素で
動かす**ので画素の値は 1 つも変わらない。これが ``register=True`` が既定である
理由である。

明るさの食い違いは、ずれのせいでも「タイルごとのゲイン」でもない
----------------------------------------------------------------
ずれを当てても、隣り合うタイルの重なりの明るさ比は 0.78 (CH1) / 0.70 (CH3) の
まま動かない。これを**タイルごとの倍率**だと見て最小二乗で合わせてみたが、
継ぎ目は**悪化した**::

    CH1 最大段差 (典型段差に対する倍率)
      ステージ座標のまま          542  (2.31x)
      + registration             553  (2.35x)
      + タイルごとのゲイン補正   1446  (6.18x)   <- 悪化
      + ゲイン補正して重なりを平均 876  (4.13x)

模型が違うからである。1 枚のタイルの中で、右端 144 列 / 左端 144 列 を測ると::

    CH1   1.275   1.284   1.299     (3 枚とも。1/0.78 = 1.28 と一致)
    CH3   1.200   1.751   1.735

**3 枚とも同じ傾きを持っている。** つまりこれはタイル個体の明るさではなく、
**どのタイルにも同じようにかかる、視野内の左右の傾き** (照明むら / ビネッティング)
である。タイルごとの 1 つの倍率でそれを打ち消すことはできない —— 傾きの
食い違いを定数で埋めようとして、かえって段差を作る。

重なりの平均も効かない
----------------------
``overlap="mean"`` 単独でも段差は消えない (CH1 2.35x -> 2.54x)。傾きのある 2 枚を
平均すると、段差が帯の端へ移るだけである。しかも 30% の面積で画素の値が変わる。
**平均が正しくなるのは視野内の傾きを取り除いたあと** —— つまり
キャリブレーションのあとである。

だから、ここで既定にするのは registration だけである
----------------------------------------------------
照明むらの形 (Q12) はこの取得からは決まらない。重なりが与える拘束は
``p(x) / p(x+336)`` だけで、480 列ぶんの形を 1 つに決めるには足りない。
系 (顕微鏡 x 対物レンズ) ごとに貯めて別に推定する必要がある。

そのために、ここは**測った値を出力に残す**: ペアごとの明るさ比と、タイルごとの
左右の比 (``Registration.as_metadata``)。取得を重ねればこれがそのまま
キャリブレーションの材料になる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

#: 重なりの扱い。``last`` は後に置いたタイルが勝つ (移植元と同じ)。
#: ``mean`` は平均 —— **尺度を合わせたあとでなければ測っていない値を作る**。
OVERLAP_POLICIES = ("last", "mean")


#: 位相相関のピークの下限。実測: 本物の重なり 0.341 / 同一画像 1.000 に対し、
#: 真っ平ら 0.000、ノイズのみ 0.029、弱いテクスチャ 0.028。あいだを取る。
MIN_PEAK = 0.10
#: ずれを当てたあとの帯の相関の下限。実測: 本物 0.93-0.94 に対し、
#: 弱いテクスチャ 0.23、ノイズ 0.006。
MIN_CORRELATION = 0.5
#: これより大きいずれは「微調整」ではない。重なりの幅に対する割合。
MAX_SHIFT_FRACTION = 0.25


@dataclass(frozen=True)
class PairShift:
    """1 組の隣り合うタイルについて測った、ずれと明るさの比。"""

    a: str
    b: str
    dy: int
    dx: int
    #: 位相相関のピーク高さ。
    peak: float
    correlation_before: float
    correlation_after: float
    #: ``b`` の帯の中央値 / ``a`` の帯の中央値。1.0 から離れるほど継ぎ目が目立つ。
    #: **これは補正していない** —— 記録するだけ。
    intensity_ratio: float | None
    accepted: bool
    #: 採らなかった理由。採ったときは空。
    reason: str = ""


@dataclass(frozen=True)
class Registration:
    """重なりから測った、タイルごとの置き直し量。

    ``offsets`` は**整数画素**である。小数で動かすには画素を作り直すことになり、
    測った値が測っていない値に変わる。継ぎ目を消すためにそれはしない。
    """

    #: タイルのキー -> (dy, dx)。採れなかったタイルは (0, 0) のまま。
    offsets: dict[str, tuple[int, int]] = field(default_factory=dict)
    pairs: tuple[PairShift, ...] = ()
    #: タイルのキー -> 右端 / 左端 の明るさ比。**視野内の傾きの実測**である。
    #: 3 枚とも同じ値が出るなら、それはタイル個体ではなく光学の性質
    #: (module docstring)。キャリブレーションを当てる前の記録として残す。
    edge_ratios: dict[str, float] = field(default_factory=dict)

    @property
    def accepted(self) -> int:
        return sum(1 for p in self.pairs if p.accepted)

    @property
    def rejected(self) -> int:
        return sum(1 for p in self.pairs if not p.accepted)

    @property
    def moved(self) -> int:
        return sum(1 for shift in self.offsets.values() if shift != (0, 0))

    def summary(self) -> str:
        if not self.pairs:
            return "no overlapping pairs to register from"
        return "%d/%d pair(s) usable, %d tile(s) moved" % (
            self.accepted, len(self.pairs), self.moved)

    def as_metadata(self) -> dict[str, str]:
        """OME の ``MapAnnotation`` に入れる形。**何をしたかを出力に残す。**"""
        data: dict[str, str] = {
            "Seam/Registration": "phase-correlation",
            "Seam/PairsAccepted": "%d/%d" % (self.accepted, len(self.pairs)),
        }
        for key, (dy, dx) in sorted(self.offsets.items()):
            if (dy, dx) != (0, 0):
                data["Seam/Offset/%s" % key] = "%+d,%+d" % (dy, dx)
        for pair in self.pairs:
            if pair.intensity_ratio is not None:
                data["Seam/IntensityRatio/%s|%s" % (pair.a, pair.b)] = (
                    "%.4f" % pair.intensity_ratio)
        for key, ratio in sorted(self.edge_ratios.items()):
            data["Seam/EdgeRatio/%s" % key] = "%.4f" % ratio
        return data


def edge_ratio(tile: np.ndarray, band: int) -> float | None:
    """1 枚のタイルの 右端 ``band`` 列 / 左端 ``band`` 列 の明るさ比。

    **視野内の傾きの、いちばん素朴な測り方**である。同じ取得のタイルが揃って
    同じ比を出すなら、それはタイル個体の明るさではなく光学の性質であり、
    タイルごとの倍率では直せない (module docstring の実測)。
    """
    band = min(band, tile.shape[1] // 2)
    if band < 1:
        return None
    left = float(np.median(tile[:, :band]))
    if left <= 0:
        return None
    return float(np.median(tile[:, -band:]) / left)


def estimate_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]:
    """``a[y+dy, x+dx] ~= b[y, x]`` となる ``(dy, dx)`` と、位相相関のピーク。

    位相相関なので、**明るさの尺度の違いに影響されない** (振幅を捨てて位相だけ
    見る)。隣り合うタイルが 0.7 倍ずれていても、ずれは測れる —— 上の実測が
    そうなっている。
    """
    if a.shape != b.shape or a.size == 0:
        raise ValueError("bands must be the same non-empty shape, got %s and %s"
                         % (a.shape, b.shape))
    fa = a.astype(np.float64)
    fb = b.astype(np.float64)
    fa -= fa.mean()
    fb -= fb.mean()
    # 端の不連続が偽のピークを作るので窓をかける。
    window = np.hanning(a.shape[0])[:, None] * np.hanning(a.shape[1])[None, :]
    spectrum = np.fft.fft2(fa * window) * np.conj(np.fft.fft2(fb * window))
    magnitude = np.abs(spectrum)
    magnitude[magnitude == 0] = 1.0
    correlation = np.fft.ifft2(spectrum / magnitude).real

    peak_at = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    shift = [int(v) if v <= n // 2 else int(v) - n
             for v, n in zip(peak_at, correlation.shape)]
    return shift[0], shift[1], float(correlation.max())


def correlation_of(a: np.ndarray, b: np.ndarray) -> float:
    """2 つの帯の相関。どちらかが真っ平らなら 0 (相関は定義できない)。"""
    fa = a.astype(np.float64).ravel()
    fb = b.astype(np.float64).ravel()
    if fa.size < 2 or fa.std() == 0 or fb.std() == 0:
        return 0.0
    return float(np.corrcoef(fa, fb)[0, 1])


def _shifted(a: np.ndarray, b: np.ndarray, dy: int, dx: int
             ) -> tuple[np.ndarray, np.ndarray]:
    """``(dy, dx)`` を当てたあとの、重なっている部分どうし。"""
    height, width = a.shape
    ay0, by0 = (dy, 0) if dy >= 0 else (0, -dy)
    ax0, bx0 = (dx, 0) if dx >= 0 else (0, -dx)
    rows = height - abs(dy)
    cols = width - abs(dx)
    if rows <= 0 or cols <= 0:
        return a[:0, :0], b[:0, :0]
    return (a[ay0:ay0 + rows, ax0:ax0 + cols],
            b[by0:by0 + rows, bx0:bx0 + cols])


def measure_pair(
    a: str,
    b: str,
    band_a: np.ndarray,
    band_b: np.ndarray,
    *,
    min_peak: float = MIN_PEAK,
    min_correlation: float = MIN_CORRELATION,
    max_shift_fraction: float = MAX_SHIFT_FRACTION,
) -> PairShift:
    """1 組の重なり帯から ``PairShift`` を作る。**通らなければ採らない。**

    採らない条件は 3 つとも「測って決めた」ものである (module docstring の表):
    ピークが低い、当てたあとの相関が低い、ずれが大きすぎて微調整ではない。
    """
    dy, dx, peak = estimate_shift(band_a, band_b)
    before = correlation_of(band_a, band_b)
    shifted_a, shifted_b = _shifted(band_a, band_b, dy, dx)
    after = correlation_of(shifted_a, shifted_b) if shifted_a.size else 0.0

    ratio: float | None = None
    if shifted_a.size:
        base = float(np.median(shifted_a))
        if base > 0:
            ratio = float(np.median(shifted_b) / base)

    limit = max(1, int(round(max_shift_fraction * min(band_a.shape))))
    reason = ""
    if peak < min_peak:
        reason = "phase-correlation peak %.3f below %.2f" % (peak, min_peak)
    elif after < min_correlation:
        reason = "band correlation %.3f below %.2f after the shift" % (
            after, min_correlation)
    elif after < before:
        reason = "the shift makes the bands agree less (%.3f -> %.3f)" % (before, after)
    elif abs(dy) > limit or abs(dx) > limit:
        reason = "shift (%+d,%+d) is larger than %d px, so it is not a refinement" % (
            dy, dx, limit)

    return PairShift(a=a, b=b, dy=dy, dx=dx, peak=peak,
                     correlation_before=before, correlation_after=after,
                     intensity_ratio=ratio, accepted=not reason, reason=reason)


def solve_offsets(keys: Sequence[str], pairs: Iterable[PairShift]
                  ) -> dict[str, tuple[int, int]]:
    """ペアごとのずれから、タイルごとの置き直し量を出す。

    ``offset[b] - offset[a] = (dy, dx)`` を採用したペアぶん並べ、最小二乗で解く。
    1 列に並んだ取得なら足し算と同じだが、2 次元に並んだ取得では矛盾する制約が
    出るので、まとめて釣り合わせる必要がある。

    **最初のタイルを原点に固定する。** 全体を平行移動しても継ぎ目は変わらない
    ので、原点を決めないと解が 1 つに決まらない。整数に丸めるのは、小数で
    動かすと画素を作り直すことになるため。
    """
    order = list(keys)
    index = {key: i for i, key in enumerate(order)}
    usable = [p for p in pairs if p.accepted and p.a in index and p.b in index]
    if not order:
        return {}
    if not usable:
        return {key: (0, 0) for key in order}

    rows = [[0.0] * len(order) for _ in usable]
    for row, pair in zip(rows, usable):
        row[index[pair.a]] = -1.0
        row[index[pair.b]] = 1.0
    design = np.array(rows, dtype=np.float64)
    target = np.array([[p.dy, p.dx] for p in usable], dtype=np.float64)

    # 原点の固定 (先頭タイル = 0)。重みを大きくして、他の制約より優先させる。
    anchor = np.zeros((1, len(order)))
    anchor[0, 0] = 10.0
    design = np.vstack([design, anchor])
    target = np.vstack([target, np.zeros((1, 2))])

    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    rounded = np.rint(solution).astype(int)
    return {key: (int(rounded[i, 0]), int(rounded[i, 1])) for i, key in enumerate(order)}


def blend_into(
    canvas: np.ndarray,
    counts: np.ndarray | None,
    tile: np.ndarray,
    selection: tuple,
    policy: str,
) -> None:
    """``tile`` を ``canvas[selection]`` へ入れる。``policy`` が重なりの扱い。

    ``mean`` は ``counts`` に「何枚書いたか」を貯めて、逐次平均で足していく。
    全タイルを同時に持たずに平均できる形にしてある。
    """
    if policy == "last":
        canvas[selection] = tile
        return
    if policy != "mean":
        raise ValueError("unknown overlap policy %r; expected one of %s"
                         % (policy, ", ".join(OVERLAP_POLICIES)))
    if counts is None:
        raise ValueError("the 'mean' policy needs a counts array")

    seen = counts[selection]
    current = canvas[selection].astype(np.float64)
    incoming = tile.astype(np.float64)
    # 逐次平均: m_n = m_{n-1} + (x - m_{n-1}) / n
    updated = np.where(seen == 0, incoming, current + (incoming - current) / (seen + 1))
    canvas[selection] = np.rint(updated).astype(canvas.dtype)
    counts[selection] = seen + 1

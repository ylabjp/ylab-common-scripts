"""プライマーの Tm。**どの計算法かを必ず明示する。**

本ラボの基準値(55-70 度など)がどの計算法のスケールなのかは、これまで
どこにも書かれていなかった。**実測して確かめた**:

    ylabjp/general の docs/protocols/molecular_biology/cloning/07-in-fusion.md に
    記録されている 2 本のプライマーの Tm を、各計算法で再現できるか比べた。

    配列                       記録  Wallace  最近接塩基対(0.5uM/50mM)
    GCTAGCCACCATGGTGAGCAAG      70    70.0     60.8
    CTTGTACAGCTCGTCCATGCC       67    66.0     58.6

**本ラボの基準は Wallace スケールである。** 最近接塩基対法で計算して同じ窓
(55-70)を当てると、**すべてが約 10 度低く出て良いプライマーを落とす。**
既定を wallace にしてあるのはこのためで、好みの問題ではない。

`nearest_neighbour` も置いてあるが、**基準値と混ぜて使わないこと。**
別のスケールである。
"""
from __future__ import annotations

from typing import Any
import math

from ylabcommon.seq.sequence import normalise

#: SantaLucia 1998 unified parameters. (dH kcal/mol, dS cal/(mol*K))
_NN: dict[str, tuple[float, float]] = {
    "AA": (-7.9, -22.2), "TT": (-7.9, -22.2),
    "AT": (-7.2, -20.4), "TA": (-7.2, -21.3),
    "CA": (-8.5, -22.7), "TG": (-8.5, -22.7),
    "GT": (-8.4, -22.4), "AC": (-8.4, -22.4),
    "CT": (-7.8, -21.0), "AG": (-7.8, -21.0),
    "GA": (-8.2, -22.2), "TC": (-8.2, -22.2),
    "CG": (-10.6, -27.2), "GC": (-9.8, -24.4),
    "GG": (-8.0, -19.9), "CC": (-8.0, -19.9),
}
_INIT: dict[str, tuple[float, float]] = {
    "G": (0.1, -2.8), "C": (0.1, -2.8), "A": (2.3, 4.1), "T": (2.3, 4.1),
}
_R = 1.987  # cal/(mol*K)


def wallace(seq: str) -> float:
    """2(A+T) + 4(G+C)。**本ラボの基準値のスケール。**

    14 mer 未満では実測から外れることが知られているが、本ラボのプライマーは
    16 mer 以上なので運用上の問題にならない。
    """
    s = normalise(seq, allow_n=False)
    gc = s.count("G") + s.count("C")
    return 2 * (len(s) - gc) + 4 * gc


def nearest_neighbour(
    seq: str, *, primer_molar: float = 0.5e-6, sodium_molar: float = 0.05
) -> float:
    """最近接塩基対法(SantaLucia 1998)。**wallace とはスケールが違う。**

    ラボ基準(55-70)をこの値に当てるな。既定濃度は PCR の一般的な条件で、
    本ラボの反応条件から確かめたものではない。
    """
    s = normalise(seq, allow_n=False)
    if len(s) < 2:
        raise ValueError("nearest-neighbour Tm needs at least 2 bases")
    dh = ds = 0.0
    for i in range(len(s) - 1):
        h, entropy = _NN[s[i:i + 2]]
        dh += h
        ds += entropy
    for end in (s[0], s[-1]):
        h, entropy = _INIT[end]
        dh += h
        ds += entropy
    ds += 0.368 * (len(s) - 1) * math.log(sodium_molar)
    return (dh * 1000) / (ds + _R * math.log(primer_molar / 4)) - 273.15


#: 名前で選べるようにしておく。**記録に残すときは名前も一緒に残すこと。**
METHODS = {"wallace": wallace, "nearest_neighbour": nearest_neighbour}
DEFAULT_METHOD = "wallace"


def tm(seq: str, method: str = DEFAULT_METHOD, **kwargs: Any) -> float:
    """名前で計算法を選ぶ。既定は本ラボ基準と同じスケールの wallace。"""
    try:
        fn = METHODS[method]
    except KeyError:
        raise ValueError(
            "unknown Tm method %r; choose from %s" % (method, sorted(METHODS))
        ) from None
    return fn(seq, **kwargs)

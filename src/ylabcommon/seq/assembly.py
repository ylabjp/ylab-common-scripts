"""in silico での組換え検証。**設計ではなく検算。**

やること: 断片・junction・産物長を配列から計算し、人が立てた計画と一致するかを
確かめる。**LLM に配列の暗算をさせないための置き換え**であって、
「発注前に SnapGene で再現して確認する」というラボの規約を置き換えるものではない。

向きの約束(ここを間違えると全部ずれる):

    vector_linear は**線状化したベクターを + 鎖 5'->3' で書いたもの**。
    環状にするとき、vector_linear の 3' 末端の直後に insert が入り、
    insert の直後に vector_linear の 5' 末端が来る。

        [--------- vector_linear --------->[--- insert --->] (先頭に戻る)
                              ^ここに Fw 側の 15 nt    ^ここに Rv 側の 15 nt

    したがって In-Fusion では
        Fw プライマーの 5' 側 15 nt = vector_linear の**末尾** 15 nt
        Rv プライマーの 5' 側 15 nt = vector_linear の**先頭** 15 nt の相補鎖
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ylabcommon.seq.primer import Violation
from ylabcommon.seq.sequence import find_all, normalise, reverse_complement

#: In-Fusion の相同配列長。07-in-fusion.md の「15 nt ルール」。
INFUSION_HOMOLOGY = 15
#: overlap extension PCR の相同配列長。05-overlap-extension-pcr.md。
OVERLAP_MIN, OVERLAP_MAX = 15, 30


@dataclass
class PcrProduct:
    """PCR 産物。"""

    sequence: str
    length: int
    #: 鋳型上で Fw の priming 部分が当たった位置(+ 鎖座標)
    fw_site: int
    rv_site: int

    @property
    def core(self) -> str:
        """鋳型由来の部分(付加配列を除く)。"""
        return self.sequence


def pcr_product(
    template: str, fw_primer: str, rv_primer: str, *, priming_length: int = 18
) -> PcrProduct:
    """鋳型とプライマー2本から PCR 産物を組み立てる。

    priming 部分は **各プライマーの 3' 側 `priming_length` 塩基**が鋳型に
    完全一致するものとして探す。付加配列(制限酵素サイト・In-Fusion の 15 nt)は
    5' 側に付いている前提で、産物にはそのまま乗る。

    **一致が 0 箇所または 2 箇所以上ならエラーにする。** 黙って先頭を採ると、
    間違った産物長を「検証済み」として返してしまう。
    """
    t = normalise(template)
    fw = normalise(fw_primer, allow_n=False)
    rv = normalise(rv_primer, allow_n=False)
    if priming_length > min(len(fw), len(rv)):
        raise ValueError(
            "priming_length %d exceeds a primer length (%d, %d)"
            % (priming_length, len(fw), len(rv)))

    fw_probe = fw[-priming_length:]
    rv_probe = reverse_complement(rv[-priming_length:])
    fw_hits = find_all(t, fw_probe)
    rv_hits = find_all(t, rv_probe)
    if len(fw_hits) != 1:
        raise ValueError(
            "Fw priming sequence matches the template %d time(s); expected exactly 1"
            % len(fw_hits))
    if len(rv_hits) != 1:
        raise ValueError(
            "Rv priming sequence matches the template %d time(s); expected exactly 1"
            % len(rv_hits))

    fw_start = fw_hits[0]
    rv_end = rv_hits[0] + priming_length
    if rv_end <= fw_start:
        raise ValueError(
            "the Rv site (%d) is not downstream of the Fw site (%d); "
            "check the primer orientation" % (rv_end, fw_start))

    body = t[fw_start:rv_end]
    seq = fw[:-priming_length] + body + reverse_complement(rv[:-priming_length]) \
        if rv[:-priming_length] else fw[:-priming_length] + body
    return PcrProduct(sequence=seq, length=len(seq), fw_site=fw_start, rv_site=rv_end)


@dataclass
class AssemblyReport:
    """組み立ての検証結果。"""

    circular_length: int
    upstream_junction: str
    downstream_junction: str
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def verify_infusion(
    vector_linear: str,
    insert_with_tails: str,
    *,
    homology: int = INFUSION_HOMOLOGY,
    junction_window: int = 12,
) -> AssemblyReport:
    """In-Fusion の組み立てを検証し、環状産物長と junction を返す。

    Args:
        vector_linear: 線状化したベクター(モジュール冒頭の向きの約束を読むこと)
        insert_with_tails: PCR 産物。両端に 15 nt の相同配列が乗っているもの
        junction_window: junction として前後に何塩基を切り出すか

    重なり合う相同配列は**1回だけ数える**。ここを二重に数えるのが
    産物長を間違える典型で、暗算だと気づけない。
    """
    v = normalise(vector_linear)
    ins = normalise(insert_with_tails)
    report_violations: list[Violation] = []

    if len(ins) < 2 * homology:
        raise ValueError(
            "insert (%d nt) is shorter than the two homology arms (2 x %d)"
            % (len(ins), homology))

    left_arm = ins[:homology]
    right_arm = ins[-homology:]
    if left_arm != v[-homology:]:
        report_violations.append(Violation(
            "upstream_homology",
            "the insert's first %d nt (%s) do not match the vector's last %d nt (%s)"
            % (homology, left_arm, homology, v[-homology:])))
    if right_arm != v[:homology]:
        report_violations.append(Violation(
            "downstream_homology",
            "the insert's last %d nt (%s) do not match the vector's first %d nt (%s)"
            % (homology, right_arm, homology, v[:homology])))

    # 相同配列は重なるので、環状長は vector + insert - 2 * homology。
    circular_length = len(v) + len(ins) - 2 * homology
    circular = v + ins[homology:len(ins) - homology]
    assert len(circular) == circular_length

    w = junction_window
    upstream = circular[max(0, len(v) - w):len(v) + w]
    # 下流 junction は環をまたぐので、先頭に回り込ませて切り出す。
    tail = circular[-w:] if w <= len(circular) else circular
    head = circular[:w]
    downstream = tail + head

    return AssemblyReport(
        circular_length=circular_length,
        upstream_junction=upstream,
        downstream_junction=downstream,
        violations=report_violations,
    )


def verify_overlap_chain(
    fragments: list[str], *, min_overlap: int = OVERLAP_MIN, max_overlap: int = OVERLAP_MAX
) -> AssemblyReport:
    """overlap extension PCR で繋ぐ断片列を検証し、産物長を返す。

    隣り合う断片の重なりを実際に測る。**15-30 bp の範囲に無ければ違反。**
    「長ければ良いわけではない」と 05-overlap-extension-pcr.md にある。
    """
    frags = [normalise(f) for f in fragments]
    if len(frags) < 2:
        raise ValueError("need at least 2 fragments")
    violations: list[Violation] = []
    total = len(frags[0])
    for i in range(len(frags) - 1):
        a, b = frags[i], frags[i + 1]
        overlap = 0
        for n in range(min(max_overlap, len(a), len(b)), 0, -1):
            if a[-n:] == b[:n]:
                overlap = n
                break
        if overlap == 0:
            violations.append(Violation(
                "overlap", "fragments %d and %d do not overlap at all" % (i, i + 1)))
        elif overlap < min_overlap:
            violations.append(Violation(
                "overlap", "fragments %d and %d overlap by only %d bp (min %d)"
                % (i, i + 1, overlap, min_overlap)))
        total += len(b) - overlap
    return AssemblyReport(
        circular_length=total,  # 線状産物なので「長さ」として使う
        upstream_junction="", downstream_junction="", violations=violations)

"""プライマーがラボ基準を満たすかの機械チェック。

基準の出典は ylabjp/general の
docs/protocols/molecular_biology/cloning/primer-design.md。
**基準を勝手に変えないこと。** 変えるならまず正のドキュメントを直す。

このモジュールは「合否を出す」だけで、**設計はしない**。設計は人と SnapGene の
仕事で、「発注前に SnapGene で再現して確認。計算しただけの数字をそのまま
発注に使わない」というラボの規約は、このコードがあっても変わらない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ylabcommon.seq.sequence import gc_fraction, normalise, reverse_complement
from ylabcommon.seq.tm import DEFAULT_METHOD, tm


@dataclass(frozen=True)
class Violation:
    """基準を外れた1件。"""

    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True)
class PcrPrimerCriteria:
    """PCR プライマーの priming 部分に対する基準(primer-design.md)。

    **priming 部分だけが対象。** 制限酵素サイトや In-Fusion の 15 nt などの
    付加配列は、長さにも Tm にも数えない。付加によって 70 mer になっても
    全長 Tm が 80 度を超えても問題ないと明記されている。
    """

    min_length: int = 16
    max_length: int = 28
    min_tm: float = 55.0
    max_tm: float = 70.0
    require_gc_3prime: bool = True
    tm_method: str = DEFAULT_METHOD
    #: ペアの Tm 差。10 度以内が基準、5 度以内が望ましい。
    max_pair_tm_diff: float = 10.0
    preferred_pair_tm_diff: float = 5.0


@dataclass(frozen=True)
class SequencingPrimerCriteria:
    """シークエンス用プライマーの基準(primer-design.md)。

    Tm は「確認しなくてよい」と明記されているので**チェックしない**。
    代わりに GC/AT の偏りを見る。
    """

    min_length: int = 18
    max_length: int = 20
    require_gc_3prime: bool = True
    #: 「GC か AT に極端に偏っていたら位置をずらす」の機械化。
    min_gc_fraction: float = 0.30
    max_gc_fraction: float = 0.70


@dataclass
class PrimerReport:
    """1本ぶんの判定結果。"""

    sequence: str
    length: int
    gc_fraction: float
    tm: float | None
    tm_method: str | None
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def check_pcr_primer(
    priming_sequence: str, criteria: PcrPrimerCriteria | None = None
) -> PrimerReport:
    """PCR プライマーの **priming 部分**を基準に照らす。

    Args:
        priming_sequence: 付加配列を除いた、鋳型にアニールする部分だけ。
    """
    c = criteria or PcrPrimerCriteria()
    s = normalise(priming_sequence, allow_n=False)
    value = tm(s, c.tm_method)
    report = PrimerReport(
        sequence=s, length=len(s), gc_fraction=gc_fraction(s),
        tm=value, tm_method=c.tm_method,
    )
    if not (c.min_length <= len(s) <= c.max_length):
        report.violations.append(Violation(
            "length", f"{len(s)} nt is outside {c.min_length}-{c.max_length}"))
    if not (c.min_tm <= value <= c.max_tm):
        report.violations.append(Violation(
            "tm", f"{value:.1f} C ({c.tm_method}) is outside "
                  f"{c.min_tm:.0f}-{c.max_tm:.0f}"))
    if c.require_gc_3prime and s[-1] not in "GC":
        report.violations.append(Violation(
            "gc_3prime", f"ends in {s[-1]}; the priming sequence must end in G or C"))
    return report


def check_primer_pair(
    fw_priming: str, rv_priming: str, criteria: PcrPrimerCriteria | None = None
) -> tuple[PrimerReport, PrimerReport, list[Violation]]:
    """2本を個別に見たうえで、ペアとしての Tm 差を見る。

    Returns:
        (Fw の判定, Rv の判定, ペアとしての違反)
    """
    c = criteria or PcrPrimerCriteria()
    fw = check_pcr_primer(fw_priming, c)
    rv = check_pcr_primer(rv_priming, c)
    pair: list[Violation] = []
    assert fw.tm is not None and rv.tm is not None  # check_pcr_primer が必ず入れる
    diff = abs(fw.tm - rv.tm)
    if diff > c.max_pair_tm_diff:
        pair.append(Violation(
            "pair_tm", f"Tm differ by {diff:.1f} C (limit {c.max_pair_tm_diff:.0f})"))
    elif diff > c.preferred_pair_tm_diff:
        # 基準内だが望ましくない。**違反ではないので合否は変えない**が、
        # 黙って通すと「5 度以内が望ましい」が死文になる。
        pair.append(Violation(
            "pair_tm_preferred",
            f"Tm differ by {diff:.1f} C; within the {c.max_pair_tm_diff:.0f} C limit "
            f"but over the preferred {c.preferred_pair_tm_diff:.0f} C"))
    return fw, rv, pair


def check_sequencing_primer(
    sequence: str, criteria: SequencingPrimerCriteria | None = None
) -> PrimerReport:
    """シークエンス用プライマーを基準に照らす。Tm は見ない。"""
    c = criteria or SequencingPrimerCriteria()
    s = normalise(sequence, allow_n=False)
    gc = gc_fraction(s)
    report = PrimerReport(
        sequence=s, length=len(s), gc_fraction=gc, tm=None, tm_method=None)
    if not (c.min_length <= len(s) <= c.max_length):
        report.violations.append(Violation(
            "length", f"{len(s)} nt is outside {c.min_length}-{c.max_length}"))
    if c.require_gc_3prime and s[-1] not in "GC":
        report.violations.append(Violation(
            "gc_3prime", f"ends in {s[-1]}; must end in G or C"))
    if not (c.min_gc_fraction <= gc <= c.max_gc_fraction):
        report.violations.append(Violation(
            "gc_content", f"GC {gc * 100:.0f}% is outside "
                          f"{c.min_gc_fraction * 100:.0f}-{c.max_gc_fraction * 100:.0f}%"))
    return report


def unique_binding_site(template: str, priming_sequence: str) -> list[int]:
    """鋳型上で priming 部分が何箇所に当たるかを返す(両鎖)。

    **0 箇所も 2 箇所以上も設計ミス。** primer-design.md が
    「second binding site が無いことを確かめる」と書いている操作の機械化。

    Returns:
        [(+鎖の出現位置...), (-鎖の出現位置...)] を連結した位置のリスト。
        位置は + 鎖座標。**長さだけを見て 1 かどうかを判定するのが使い方。**
    """
    from ylabcommon.seq.sequence import find_all
    t = normalise(template)
    p = normalise(priming_sequence, allow_n=False)
    return find_all(t, p) + find_all(t, reverse_complement(p))

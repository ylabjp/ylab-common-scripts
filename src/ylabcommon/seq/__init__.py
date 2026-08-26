"""配列計算。**LLM に塩基配列の暗算をさせないための置き換え。**

`ylabjp/general` の issue #72。**設計ではなく検算**を提供する。
「発注前に SnapGene で再現して確認する」という規約は不変
(ylabjp/general の CLAUDE.md)。

配列そのものはここに置かない(公開リポジトリのため)。
"""
from ylabcommon.seq.assembly import (
    AssemblyReport, PcrProduct, pcr_product, verify_infusion, verify_overlap_chain,
)
from ylabcommon.seq.primer import (
    PcrPrimerCriteria, PrimerReport, SequencingPrimerCriteria, Violation,
    check_pcr_primer, check_primer_pair, check_sequencing_primer, unique_binding_site,
)
from ylabcommon.seq.sequence import (
    SequenceError, find_all, gc_fraction, normalise, reverse_complement,
)
from ylabcommon.seq.tm import DEFAULT_METHOD, METHODS, nearest_neighbour, tm, wallace

__all__ = [
    "AssemblyReport", "PcrProduct", "pcr_product", "verify_infusion",
    "verify_overlap_chain", "PcrPrimerCriteria", "PrimerReport",
    "SequencingPrimerCriteria", "Violation", "check_pcr_primer",
    "check_primer_pair", "check_sequencing_primer", "unique_binding_site",
    "SequenceError", "find_all", "gc_fraction", "normalise", "reverse_complement",
    "DEFAULT_METHOD", "METHODS", "nearest_neighbour", "tm", "wallace",
]

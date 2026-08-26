"""ylabcommon.seq のテスト。

**固定値はラボの実記録から取っている。** 出典は ylabjp/general の
docs/protocols/molecular_biology/cloning/07-in-fusion.md の worked example
(#146 の EGFP を #262 へ In-Fusion する設計)。作り話の配列でテストすると、
向きの約束を取り違えたまま緑になる。
"""
from __future__ import annotations

import pytest

from ylabcommon.seq import (
    PcrPrimerCriteria, SequencingPrimerCriteria, SequenceError,
    check_pcr_primer, check_primer_pair, check_sequencing_primer,
    find_all, gc_fraction, nearest_neighbour, normalise, pcr_product,
    reverse_complement, tm, unique_binding_site, verify_infusion,
    verify_overlap_chain, wallace,
)

# --- 07-in-fusion.md の worked example から ---------------------------------
FW_PRIMER = "CCCTCGTAAAGCTAGCCACCATGGTGAGCAAG"          # 32 mer
FW_PRIMING = "GCTAGCCACCATGGTGAGCAAG"                     # 22 mer, 記録 Tm 70
FW_HOMOLOGY = "CCCTCGTAAAGCTAG"                           # 15 nt
RV_PRIMER = "ATCGAATTCGGCGCGCCTTACTTGTACAGCTCGTCCATGCC"  # 41 mer
RV_PRIMING = "CTTGTACAGCTCGTCCATGCC"                      # 21 mer, 記録 Tm 67
RV_HOMOLOGY = "ATCGAATTCGGCGCG"                           # 15 nt


class TestSequence:
    def test_normalise_strips_whitespace_and_upcases(self):
        assert normalise("  acgt\nACGT ") == "ACGTACGT"

    def test_normalise_rejects_ambiguity_codes(self):
        # 曖昧塩基を黙って通すと Tm も一致判定も意味を失う。
        with pytest.raises(SequenceError):
            normalise("ACGTR")

    def test_normalise_rejects_empty(self):
        with pytest.raises(SequenceError):
            normalise("   ")

    def test_reverse_complement_is_an_involution(self):
        assert reverse_complement(reverse_complement(FW_PRIMING)) == FW_PRIMING

    def test_reverse_complement_known_value(self):
        assert reverse_complement("GCTAGC") == "GCTAGC"   # 回文(NheI)
        assert reverse_complement("AAAC") == "GTTT"

    def test_gc_fraction(self):
        assert gc_fraction("GGCC") == 1.0
        assert gc_fraction("ATAT") == 0.0

    def test_find_all_reports_overlapping_hits(self):
        # 重なりを見落とすと「2箇所ある」ことに気づけない。
        assert find_all("AAAA", "AA") == [0, 1, 2]


class TestTm:
    """**基準値のスケールがどれかを固定する回帰テスト。**"""

    @pytest.mark.parametrize("seq,recorded", [(FW_PRIMING, 70), (RV_PRIMING, 67)])
    def test_wallace_reproduces_the_recorded_lab_values(self, seq, recorded):
        # ラボの記録は Wallace スケール。1 度以内で一致する。
        assert abs(wallace(seq) - recorded) <= 1.0

    @pytest.mark.parametrize("seq,recorded", [(FW_PRIMING, 70), (RV_PRIMING, 67)])
    def test_nearest_neighbour_is_about_ten_degrees_lower(self, seq, recorded):
        # **これが本題。** NN で計算して 55-70 の窓を当てると良いプライマーを落とす。
        assert recorded - nearest_neighbour(seq) > 5.0

    def test_default_method_is_the_lab_scale(self):
        assert tm(FW_PRIMING) == wallace(FW_PRIMING)

    def test_unknown_method_is_an_error(self):
        with pytest.raises(ValueError):
            tm(FW_PRIMING, "santalucia")


class TestPcrPrimerCriteria:
    def test_recorded_primers_pass(self):
        fw, rv, pair = check_primer_pair(FW_PRIMING, RV_PRIMING)
        assert fw.ok, [str(v) for v in fw.violations]
        assert rv.ok, [str(v) for v in rv.violations]
        # 70 と 66 で 4 度差。望ましい 5 度以内に収まる。
        assert pair == []

    def test_priming_sequence_must_end_in_g_or_c(self):
        report = check_pcr_primer("GCTAGCCACCATGGTGAGCAA")  # 末尾 A
        assert not report.ok
        assert any(v.rule == "gc_3prime" for v in report.violations)

    def test_too_short_is_flagged(self):
        report = check_pcr_primer("GCTAGCCACCATGG")  # 14 mer
        assert any(v.rule == "length" for v in report.violations)

    def test_low_tm_is_flagged(self):
        report = check_pcr_primer("ATATATATATATATATAC")  # Wallace で 38
        assert any(v.rule == "tm" for v in report.violations)

    def test_pair_tm_difference_over_the_limit(self):
        # Wallace 88 対 70 で 18 度差。ちょうど 10 度差は基準内(「10 度以内」)なので、
        # 限界を跨がせるには 10 度を**超える**差が要る。
        hot = "GGCGGCGGCGGCGGCGGCGCGC"     # 22 mer, 全 GC -> Wallace 88
        fw, rv, pair = check_primer_pair(FW_PRIMING, hot)
        assert any(v.rule == "pair_tm" for v in pair)

    def test_exactly_ten_degrees_apart_is_within_the_limit(self):
        # 境界。「10 度以内」なので 10 度ちょうどは通し、望ましくない旨だけ出す。
        hot = "GGCGGCGGCGGCGGCGGCGC"       # 20 mer, 全 GC -> Wallace 80
        fw, rv, pair = check_primer_pair(FW_PRIMING, hot)
        assert [v.rule for v in pair] == ["pair_tm_preferred"]

    def test_preferred_difference_is_reported_without_failing(self):
        # 5 度超 10 度以内は「望ましくない」だけで、個々の合否は変えない。
        mild = "GCTAGCCACCATGGTGAGC"       # 19 mer, Wallace 62
        fw, rv, pair = check_primer_pair(FW_PRIMING, mild)
        assert fw.ok and rv.ok
        assert [v.rule for v in pair] == ["pair_tm_preferred"]

    def test_criteria_are_overridable(self):
        loose = PcrPrimerCriteria(min_length=10, max_length=40, min_tm=30, max_tm=90)
        assert check_pcr_primer("GCTAGCCACCATGG", loose).ok


class TestSequencingPrimerCriteria:
    def test_tm_is_not_checked(self):
        report = check_sequencing_primer("GCTAGCCACCATGGTGAG")  # 18 mer
        assert report.tm is None
        assert report.ok, [str(v) for v in report.violations]

    def test_skewed_gc_is_flagged(self):
        report = check_sequencing_primer("GGGGGGGGGGGGGGGGGG")
        assert any(v.rule == "gc_content" for v in report.violations)

    def test_length_window_is_narrow(self):
        assert any(v.rule == "length"
                   for v in check_sequencing_primer("GCTAGCCACCATGGTGAGCAAG").violations)


class TestUniqueBindingSite:
    def test_single_hit(self):
        template = "TTTT" + FW_PRIMING + "AAAA"
        assert len(unique_binding_site(template, FW_PRIMING)) == 1

    def test_second_site_is_visible(self):
        template = FW_PRIMING + "TTTT" + FW_PRIMING
        assert len(unique_binding_site(template, FW_PRIMING)) == 2

    def test_reverse_strand_counts(self):
        template = "TTTT" + reverse_complement(FW_PRIMING) + "AAAA"
        assert len(unique_binding_site(template, FW_PRIMING)) == 1


class TestPcrProduct:
    TEMPLATE = "GGGG" + FW_PRIMING + "ACGTACGTACGT" + reverse_complement(RV_PRIMING) + "CCCC"

    def test_product_carries_the_tails_and_has_the_expected_length(self):
        p = pcr_product(self.TEMPLATE, FW_PRIMER, RV_PRIMER)
        body_len = len(FW_PRIMING) + 12 + len(RV_PRIMING)
        tails = (len(FW_PRIMER) - len(FW_PRIMING)) + (len(RV_PRIMER) - len(RV_PRIMING))
        assert p.length == body_len + tails
        assert p.sequence.startswith(FW_HOMOLOGY)
        assert p.sequence.endswith(reverse_complement(RV_HOMOLOGY))

    def test_ambiguous_priming_site_is_an_error(self):
        # **黙って先頭を採らない。** 間違った産物長を「検証済み」にしないため。
        doubled = self.TEMPLATE + self.TEMPLATE
        with pytest.raises(ValueError, match="matches the template 2"):
            pcr_product(doubled, FW_PRIMER, RV_PRIMER)

    def test_missing_priming_site_is_an_error(self):
        with pytest.raises(ValueError, match="matches the template 0"):
            pcr_product("ACGT" * 20, FW_PRIMER, RV_PRIMER)

    def test_primers_facing_away_from_each_other_are_an_error(self):
        # 両方とも当たるが順序が逆。**この場合だけ orientation の分岐に入る。**
        backwards = ("GGGG" + reverse_complement(RV_PRIMING) + "ACGTACGTACGT"
                     + FW_PRIMING + "CCCC")
        with pytest.raises(ValueError, match="orientation"):
            pcr_product(backwards, FW_PRIMER, RV_PRIMER)

    def test_swapped_primers_do_not_find_their_sites(self):
        # 役割を入れ替えると、そもそも + 鎖に当たらない。これも黙って通さない。
        with pytest.raises(ValueError, match="matches the template 0"):
            pcr_product(self.TEMPLATE, RV_PRIMER, FW_PRIMER)


class TestVerifyInfusion:
    # ベクターの末尾 15 nt = Fw 側の相同、先頭 15 nt = Rv 側の相同(相補鎖)。
    VECTOR = reverse_complement(RV_HOMOLOGY) + "TTTTTTTTTT" + FW_HOMOLOGY
    INSERT = FW_HOMOLOGY + "AAACCCGGGTTT" + reverse_complement(RV_HOMOLOGY)

    def test_matching_arms_pass(self):
        r = verify_infusion(self.VECTOR, self.INSERT)
        assert r.ok, [str(v) for v in r.violations]

    def test_homology_is_counted_once(self):
        # ここを二重に数えるのが産物長を間違える典型。
        r = verify_infusion(self.VECTOR, self.INSERT)
        assert r.circular_length == len(self.VECTOR) + len(self.INSERT) - 30

    def test_mismatched_upstream_arm_is_flagged(self):
        bad = "GGGGGGGGGGGGGGG" + self.INSERT[15:]
        r = verify_infusion(self.VECTOR, bad)
        assert any(v.rule == "upstream_homology" for v in r.violations)

    def test_mismatched_downstream_arm_is_flagged(self):
        bad = self.INSERT[:-15] + "GGGGGGGGGGGGGGG"
        r = verify_infusion(self.VECTOR, bad)
        assert any(v.rule == "downstream_homology" for v in r.violations)

    def test_insert_shorter_than_the_arms_is_an_error(self):
        with pytest.raises(ValueError):
            verify_infusion(self.VECTOR, "ACGT")

    def test_downstream_junction_spans_the_circle(self):
        r = verify_infusion(self.VECTOR, self.INSERT, junction_window=6)
        # 環をまたぐので、ベクター先頭が junction の後半に現れる。
        assert self.VECTOR[:6] in r.downstream_junction


class TestVerifyOverlapChain:
    def test_fifteen_bp_overlap_passes(self):
        a = "ACGT" * 5 + "GGGGGCCCCCAAAAA"
        b = "GGGGGCCCCCAAAAA" + "TTTT" * 5
        r = verify_overlap_chain([a, b])
        assert r.ok
        assert r.circular_length == len(a) + len(b) - 15

    def test_short_overlap_is_flagged(self):
        a = "ACGT" * 5 + "GGGGG"
        b = "GGGGG" + "TTTT" * 5
        r = verify_overlap_chain([a, b])
        assert any(v.rule == "overlap" for v in r.violations)

    def test_no_overlap_is_flagged(self):
        r = verify_overlap_chain(["AAAAAAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCCCCCC"])
        assert any("do not overlap" in v.detail for v in r.violations)

    def test_single_fragment_is_an_error(self):
        with pytest.raises(ValueError):
            verify_overlap_chain(["ACGT"])

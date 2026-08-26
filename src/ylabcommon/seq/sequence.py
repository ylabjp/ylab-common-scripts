"""DNA 配列の最小限の操作。

**このモジュールに配列そのものを置かない。** ylab-common-scripts は公開
リポジトリなので、プラスミドの配列は引数として渡されるだけで、ここには残らない
(置き場の正は ylabjp/general の docs/resources/plasmids/sequences/)。

外部依存を持たない。Biopython や pydna を入れる案もあったが、ここで必要なのは
相補鎖・GC 含量・部分一致だけで、**共有ライブラリ(GUI と解析パイプラインが
両方 import する)に重い依存を増やす理由にならない**と判断した。
"""
from __future__ import annotations

_COMPLEMENT = str.maketrans("ACGTacgtNn", "TGCAtgcaNn")

#: 受け付ける塩基。曖昧塩基(R/Y/S/W...)は**受け付けない**。
#: プライマー設計で曖昧塩基が出てくる場面は本ラボに無く、
#: 黙って通すと Tm も一致判定も意味を失う。
_ALLOWED = set("ACGTN")


class SequenceError(ValueError):
    """配列として受け付けられない入力。"""


def normalise(seq: str, *, allow_n: bool = True) -> str:
    """空白を除き大文字にして検証する。

    空白と改行は落とす(GenBank やドキュメントから貼ると必ず混ざる)。
    """
    cleaned = "".join(seq.split()).upper()
    if not cleaned:
        raise SequenceError("empty sequence")
    bad = sorted(set(cleaned) - _ALLOWED)
    if bad:
        raise SequenceError(
            "unexpected character(s) %s; only A/C/G/T%s are accepted"
            % (", ".join(repr(b) for b in bad), "/N" if allow_n else "")
        )
    if not allow_n and "N" in cleaned:
        raise SequenceError("N is not allowed here")
    return cleaned


def reverse_complement(seq: str) -> str:
    """相補鎖を 5'->3' で返す。"""
    return normalise(seq).translate(_COMPLEMENT)[::-1]


def gc_fraction(seq: str) -> float:
    """GC 含量(0-1)。N は分母に数えるが GC には数えない。"""
    s = normalise(seq)
    return (s.count("G") + s.count("C")) / len(s)


def find_all(haystack: str, needle: str) -> list[int]:
    """重なりを許して部分一致の開始位置を全部返す。

    重なりを許すのは、**一致が2箇所以上ないことを確かめるのが目的**だから。
    str.find の単純ループでは重なった2つ目を見落とす。
    """
    h, n = normalise(haystack), normalise(needle)
    out: list[int] = []
    start = 0
    while True:
        i = h.find(n, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1

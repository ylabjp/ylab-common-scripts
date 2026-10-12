"""ファイルの指紋: 記録に残し、あとで「同じ中身のファイルか」を確かめる。OS にもコピーにもよらない。

なぜ
----
更新時刻は OS とファイルシステムで細かさが違う。Linux は 1 ns、Windows は共有ドライブ (SMB) の時刻を
100 ns に切り捨てて読み、FAT / exFAT はさらに粗い。コピーしても変わる。slice-analysis の
``shaft_reference.json`` は画像の更新時刻 (ns) を指紋にしていたので、Windows で回した集計では
同じ画像が「別の画像」とされ、値が黙って NaN になっていた (TNFa_bath、2026-10-12)。

そこで指紋は **ファイルの大きさと中身の一部のハッシュ** にする (SY 2026-10-12、案 B)。更新時刻は使わない。
「同じファイルか」を記録に残すところは、自前で指紋を作らずここを使う。

決め方
------
``sha256(大きさ (8 byte、little endian) + 先頭 N byte + 末尾 N byte)``。2N byte 以下のファイルは全体。
N は記録に残し (``sample_bytes``)、照合では記録の N で計算し直す —— N を変えても古い記録と比べられる。
大きな画像でも読むのは 2N byte だけ。

**見分けられないもの:** 大きさが同じで、先頭と末尾の N byte が同じで、間だけが違うファイル。
画像を書き直せば、ヘッダと最初・最後の面が入る先頭と末尾も変わる。

使い方
------
記録するとき :func:`file_fingerprint` の dict をそのまま書く。照合は :func:`fingerprint_differences`
(違いを人が読める行で返す。同じなら空) か :func:`same_file`。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

#: 指紋の形の版。形を変えたら上げる (古い版の記録は「比べられない」と言う)。
FINGERPRINT_VERSION = 1
#: 先頭と末尾それぞれで読む量 [byte]。
DEFAULT_SAMPLE_BYTES = 4 * 1024 * 1024


def _sample_sha256(path: Path, size: int, sample_bytes: int) -> str:
    """大きさ + 先頭と末尾 ``sample_bytes`` ずつ (2 倍以下なら全体) の sha256。"""
    digest = hashlib.sha256(int(size).to_bytes(8, "little"))
    with open(path, "rb") as f:
        if size <= 2 * sample_bytes:
            digest.update(f.read())
        else:
            digest.update(f.read(sample_bytes))
            f.seek(size - sample_bytes)
            digest.update(f.read(sample_bytes))
    return digest.hexdigest()


def file_fingerprint(path: str | Path, sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> dict[str, Any]:
    """``path`` の指紋 (JSON にそのまま書ける dict)。

    Raises:
        FileNotFoundError: ファイルが無い。無いものの指紋は作らない。
        ValueError: ``sample_bytes`` が正でない。
    """
    if int(sample_bytes) <= 0:
        raise ValueError("sample_bytes must be > 0, got %r" % (sample_bytes,))
    p = Path(path)
    size = p.stat().st_size
    return {"fingerprint": FINGERPRINT_VERSION, "name": p.name, "size": int(size),
            "sample_bytes": int(sample_bytes),
            "sha256": _sample_sha256(p, size, int(sample_bytes))}


def fingerprint_differences(recorded: dict[str, Any], path: str | Path) -> list[str]:
    """記録した指紋と、いまの ``path`` の違い。同じなら空のリスト。

    違いは人が読める行で返す (どこが違うかを警告にそのまま書けるように)。名前・大きさ・中身の順に見て、
    大きさが違えば中身は計算しない。
    """
    p = Path(path)
    version = recorded.get("fingerprint")
    if version != FINGERPRINT_VERSION:
        return ["the record has fingerprint version %r; this code reads %d"
                % (version, FINGERPRINT_VERSION)]
    if not p.exists():
        return ["the file is missing: %s" % p]
    out = []
    if recorded.get("name") != p.name:
        out.append("name: recorded %r, now %r" % (recorded.get("name"), p.name))
    size = p.stat().st_size
    if int(recorded.get("size", -1)) != size:
        out.append("size: recorded %s bytes, now %d bytes" % (recorded.get("size"), size))
        return out
    n = int(recorded["sample_bytes"])
    if _sample_sha256(p, size, n) != recorded.get("sha256"):
        out.append("content: the sha256 of the size and the first and last %d bytes differs" % n)
    return out


def same_file(recorded: dict[str, Any], path: str | Path) -> bool:
    """記録した指紋と、いまの ``path`` が同じか。"""
    return not fingerprint_differences(recorded, path)

"""ファイルの指紋 (``ylabcommon.fingerprint``): 大きさと中身の一部のハッシュ。更新時刻は使わない。

SY 2026-10-12 (案 B)。slice-analysis の ``shaft_reference.json`` が更新時刻 (ns) を指紋にしていて、
Windows (SMB は 100 ns に切り捨てて読む) で回した集計では同じ画像が別物とされた。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ylabcommon.fingerprint import (
    DEFAULT_SAMPLE_BYTES,
    FINGERPRINT_VERSION,
    file_fingerprint,
    fingerprint_differences,
    same_file,
)

N = 16  # テストでは小さな sample_bytes で、先頭・末尾・間を作り分ける


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_the_record_is_plain_json_and_names_its_parts(tmp_path: Path) -> None:
    f = _write(tmp_path / "img.tif", bytes(range(200)))

    rec = file_fingerprint(f, sample_bytes=N)

    assert json.loads(json.dumps(rec)) == rec
    assert rec["fingerprint"] == FINGERPRINT_VERSION
    assert (rec["name"], rec["size"], rec["sample_bytes"]) == ("img.tif", 200, N)
    assert file_fingerprint(f)["sample_bytes"] == DEFAULT_SAMPLE_BYTES


def test_the_modification_time_does_not_matter(tmp_path: Path) -> None:
    """更新時刻を 100 ns に切り捨てても (Windows)、別の時刻にしても、コピーしても同じファイル。"""
    f = _write(tmp_path / "img.tif", os.urandom(300))
    rec = file_fingerprint(f, sample_bytes=N)
    st = f.stat()

    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns // 100 * 100 + 1))
    assert same_file(rec, f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 3_600 * 10**9))
    assert same_file(rec, f)
    copy = tmp_path / "copy"
    copy.mkdir()
    shutil.copyfile(f, copy / "img.tif")                       # 時刻は引き継がない
    assert same_file(rec, copy / "img.tif")


@pytest.mark.parametrize("where", [0, 299])
def test_a_change_at_the_start_or_the_end_is_found(tmp_path: Path, where: int) -> None:
    data = bytearray(os.urandom(300))
    f = _write(tmp_path / "img.tif", bytes(data))
    rec = file_fingerprint(f, sample_bytes=N)
    data[where] ^= 0xFF
    f.write_bytes(bytes(data))

    assert fingerprint_differences(rec, f) == [
        "content: the sha256 of the size and the first and last %d bytes differs" % N]


def test_a_change_in_the_middle_of_a_large_file_is_not_seen(tmp_path: Path) -> None:
    """限界: 大きさも先頭と末尾 N byte も同じなら見分けられない (説明に書いたとおり)。"""
    data = bytearray(os.urandom(300))
    f = _write(tmp_path / "img.tif", bytes(data))
    rec = file_fingerprint(f, sample_bytes=N)
    data[150] ^= 0xFF
    f.write_bytes(bytes(data))

    assert same_file(rec, f)


def test_a_small_file_is_hashed_whole(tmp_path: Path) -> None:
    data = bytearray(os.urandom(2 * N))
    f = _write(tmp_path / "small.json", bytes(data))
    rec = file_fingerprint(f, sample_bytes=N)
    data[N] ^= 0xFF                                          # 2N 以下なら間も見る
    f.write_bytes(bytes(data))

    assert not same_file(rec, f)


def test_size_name_and_missing_are_said_plainly(tmp_path: Path) -> None:
    f = _write(tmp_path / "img.tif", os.urandom(300))
    rec = file_fingerprint(f, sample_bytes=N)

    f.write_bytes(os.urandom(301))
    assert fingerprint_differences(rec, f) == ["size: recorded 300 bytes, now 301 bytes"]
    other = _write(tmp_path / "other.tif", f.read_bytes())
    assert fingerprint_differences(rec, other)[0] == "name: recorded 'img.tif', now 'other.tif'"
    assert fingerprint_differences(rec, tmp_path / "gone.tif") == [
        "the file is missing: %s" % (tmp_path / "gone.tif")]


def test_the_recorded_sample_size_is_used_and_an_unknown_version_is_refused(tmp_path: Path) -> None:
    """照合は記録の sample_bytes で計算し直す (既定を変えても古い記録と比べられる)。"""
    f = _write(tmp_path / "img.tif", os.urandom(300))
    rec = file_fingerprint(f, sample_bytes=7)

    assert same_file(rec, f)
    assert fingerprint_differences(dict(rec, fingerprint=0), f) == [
        "the record has fingerprint version 0; this code reads %d" % FINGERPRINT_VERSION]
    with pytest.raises(FileNotFoundError):
        file_fingerprint(tmp_path / "gone.tif")
    with pytest.raises(ValueError, match="sample_bytes"):
        file_fingerprint(f, sample_bytes=0)

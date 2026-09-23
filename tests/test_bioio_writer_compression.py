"""Which arguments reach tifffile, and that every codec offered is lossless.

The writer used to send ``compressionargs={"level": ...}`` whatever the codec
was. ``lzma`` does not take ``level``, so asking for the codec with the best
ratio was an error rather than a setting.
"""
import numpy as np
import pytest
import tifffile

from ylabcommon.bioio.core.bioio_writer import BioIOWriter


def test_a_codec_with_a_level_gets_one(tmp_path):
    writer = BioIOWriter(tmp_path / "x", compression="zstd", compression_level=22)
    assert writer._tiff_compression_kwargs() == {
        "compression": "zstd", "compressionargs": {"level": 22}}


def test_lzma_takes_a_level_like_the_others(tmp_path):
    writer = BioIOWriter(tmp_path / "x", compression="lzma", compression_level=9)
    assert writer._tiff_compression_kwargs() == {
        "compression": "lzma", "compressionargs": {"level": 9}}


def test_a_codec_with_no_level_gets_no_args(tmp_path):
    """``lzw_encode() got an unexpected keyword argument 'level'`` — sending
    one is a TypeError, not a no-op, so a writer that always sends it cannot
    reach lzw at all."""
    writer = BioIOWriter(tmp_path / "x", compression="lzw", compression_level=9)
    assert writer._tiff_compression_kwargs() == {"compression": "lzw"}


def test_no_compression_sends_nothing(tmp_path):
    for value in (None, "none", "None"):
        writer = BioIOWriter(tmp_path / "x", compression=value)
        assert writer._tiff_compression_kwargs() == {}


def test_the_predictor_is_only_sent_when_it_was_asked_for(tmp_path):
    assert "predictor" not in BioIOWriter(tmp_path / "x")._tiff_compression_kwargs()
    assert BioIOWriter(tmp_path / "x", predictor=True
                       )._tiff_compression_kwargs()["predictor"] is True


@pytest.mark.parametrize("compression,level,predictor", [
    ("zlib", 6, None), ("zlib", 9, True), ("zstd", 22, True),
    ("lzma", 9, True), ("lzw", None, None), (None, None, None),
])
def test_every_offered_codec_round_trips_the_pixels(tmp_path, compression,
                                                    level, predictor):
    """Lossless is the requirement, not the hope: the array that comes back has
    to be the array that went in, bit for bit."""
    rng = np.random.default_rng(0)
    array = rng.integers(0, 4000, (8, 32, 48)).astype(np.uint16)
    writer = BioIOWriter(tmp_path / "x", compression=compression,
                         compression_level=level, predictor=predictor)
    path = tmp_path / "x.tif"
    tifffile.imwrite(str(path), array, **writer._tiff_compression_kwargs())
    assert np.array_equal(tifffile.imread(str(path)), array)

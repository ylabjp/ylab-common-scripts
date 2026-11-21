"""Tiles on disk, the same pixels back, and no Z spacing for a single plane."""
import numpy as np
import pytest
import tifffile

import ylabcommon.bioio.core.bioio_writer as W
from ylabcommon.bioio.core.bioio_writer import BioIOWriter

# Not a multiple of the tile, so the edge tiles are padded on the way in.
T, C, Z, Y, X = 1, 2, 3, 70, 100


def _data(z=Z, seed=0):
    return np.random.default_rng(seed).integers(0, 4000, (T, C, z, Y, X)).astype(np.uint16)


def _read(path):
    with tifffile.TiffFile(str(path)) as tif:
        page = tif.pages[0]
        return tif.series[0].asarray(), page.is_tiled, (page.tilelength, page.tilewidth), tif.ome_metadata


def test_tiles_are_sent_only_when_asked_for(tmp_path):
    assert BioIOWriter(tmp_path / "x")._tiff_layout_kwargs() == {}
    assert BioIOWriter(tmp_path / "x", tile=512)._tiff_layout_kwargs() == {"tile": (512, 512)}


@pytest.mark.parametrize("bad", [0, 100, (16, 15), (16, 16, 16)])
def test_a_tile_tiff_cannot_store_is_refused_up_front(tmp_path, bad):
    with pytest.raises(ValueError):
        BioIOWriter(tmp_path / "x", tile=bad)


def test_a_tiled_section_reads_back_bit_for_bit(tmp_path):
    data = _data()
    BioIOWriter(tmp_path / "s", compression="zstd", compression_level=3,
                predictor=True, tile=32).write(data, dim_order="TCZYX",
                                              physical_pixel_sizes=(1.0, 0.5, 0.5))
    back, tiled, tile, _ = _read(tmp_path / "s.ome.tif")
    assert tiled and tile == (32, 32)
    assert np.array_equal(back.reshape(data.shape), data)


def test_the_streamed_path_tiles_too_and_loses_nothing(tmp_path):
    da = pytest.importorskip("dask.array")
    data = _data()
    lazy = da.from_array(data, chunks=(1, C, Z, Y, X))
    writer = BioIOWriter(tmp_path / "s", compression="zstd", predictor=True, tile=32)
    writer._write_ometiff_streaming(lazy, tmp_path / "s.ome.tif", channel_names=["A", "B"],
                                    physical_pixel_sizes=(1.0, 0.5, 0.5))
    back, tiled, tile, _ = _read(tmp_path / "s.ome.tif")
    assert tiled and tile == (32, 32)
    assert np.array_equal(back.reshape(data.shape), data)


def test_edge_tiles_are_padded_to_the_full_shape():
    plane = np.arange(70 * 100, dtype=np.uint16).reshape(70, 100)
    tiles = list(W._tiles(plane, (32, 32)))
    assert len(tiles) == 3 * 4 and all(t.shape == (32, 32) for t in tiles)
    assert tiles[-1][5, 3] == plane[69, 99] and tiles[-1][6, 4] == 0


def test_one_plane_gets_no_z_spacing_and_several_keep_it(tmp_path):
    for z, want in ((1, False), (3, True)):
        path = tmp_path / ("z%d" % z)
        BioIOWriter(path, compression="zlib").write(
            _data(z=z), dim_order="TCZYX", physical_pixel_sizes=(1.61, 0.5, 0.5))
        assert ("PhysicalSizeZ=" in _read(str(path) + ".ome.tif")[3]) is want

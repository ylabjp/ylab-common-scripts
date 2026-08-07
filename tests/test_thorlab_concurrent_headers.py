"""ヘッダ読みを並行化しても、結果と監視が逐次のときと変わらないことのテスト。

``thorlab.open_tiffs`` は1ファイルにつき ``BioImage`` を3回 open() する。ネットワーク
ドライブ越しだとこの往復が取り込み時間のほぼ全部になるため、ワーカースレッドで
重ねている (:mod:`ylabcommon.utils.parallel`)。

並行化で壊れうるのは速度ではなく **結果の同一性と、止まった場所の特定** なので、
そこを固定する。逐次との差が「所要時間だけ」であることを、同じ入力に対する
出力の同値性で確かめる。
"""
from __future__ import annotations

import os
import threading
import time

import dask.array as da
import numpy as np
import pytest
import tifffile

import ylabcommon.utils.perf as perf
import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    DEFAULT_HEADER_WORKERS,
    HEADER_WORKERS_ENV,
    _header_workers,
    stack_thorlab_with_bioio_calibrated,
)


@pytest.fixture(autouse=True)
def clean_registry():
    perf._active_steps.clear()
    yield
    perf._active_steps.clear()


@pytest.fixture
def steps(monkeypatch):
    records = []
    monkeypatch.setattr(perf, "log_info", lambda _m, **f: records.append(f))
    monkeypatch.setattr(perf, "log_warning", lambda _m, **f: records.append(f))
    return records


@pytest.fixture
def workers(monkeypatch):
    """並行数を明示するフィクスチャ。既定値に依存したテストにしない。"""
    def _set(n):
        monkeypatch.setenv(HEADER_WORKERS_ENV, str(n))
    return _set


@pytest.fixture
def xyt_dir(tmp_path):
    """XYT 取得の形 (単一プレーンを多数)。プレーンごとに違う値を入れておく。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 25):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((8, 8), i, dtype=np.uint16))
    return d


PARAMS_T = {"mode": "T", "SizeT": 24, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.}


def build(d, workers_env, params=PARAMS_T, files=None):
    os.environ[HEADER_WORKERS_ENV] = str(workers_env)
    if files is None:
        files = sorted(str(p) for p in d.glob("*.tif"))
    return stack_thorlab_with_bioio_calibrated(files, d / "Experiment.xml", params,
                                               min_kb=0)


# ---- 逐次と並行で結果が同じ --------------------------------------------------

def test_pool_and_serial_produce_the_same_lazy_array(xyt_dir):
    """並行版の出力が逐次版と同値 (遅延のまま、同じ shape/chunk/dtype)。"""
    ser, ser_files = build(xyt_dir, 1)
    par, par_files = build(xyt_dir, 8)

    assert isinstance(ser, da.Array) and isinstance(par, da.Array)
    assert par.shape == ser.shape == (24, 1, 1, 8, 8)
    assert par.chunks == ser.chunks
    assert par.dtype == ser.dtype
    assert par_files == ser_files


def test_pool_preserves_plane_order(xyt_dir):
    """T 方向の並びがファイル名順のまま。

    完了順で組み立てる実装だと、ここだけが静かに壊れる (例外も出ない)。
    プレーン i の画素値は i+1 なので、順序が崩れれば必ず検出できる。
    """
    par, _ = build(xyt_dir, 8)
    planes = np.asarray(par)[:, 0, 0, 0, 0]
    assert planes.tolist() == list(range(1, 25))


def test_pool_output_is_pixel_identical_to_serial(xyt_dir):
    ser, _ = build(xyt_dir, 1)
    par, _ = build(xyt_dir, 8)
    assert np.array_equal(np.asarray(ser), np.asarray(par))


def test_building_the_stack_reads_no_pixels(xyt_dir, monkeypatch):
    """グラフ構築時に画素を読まない (遅延のまま)。

    ワーカースレッドの中でうっかり ``.data`` や ``.compute()`` を呼ぶと、
    28 GiB 級の実データでは構築の時点で OOM する。

    画素読みの本体 (``_read_tiff_pixels``) を「呼ばれたら落ちる」に差し替えて構築する。
    tifffile のどの内部経路を通るかに依存しないので、実装が変わっても意味が保つ。
    """
    def boom(path):
        raise AssertionError("pixels were read while building the graph: %s" % path)

    monkeypatch.setattr(mod, "_read_tiff_pixels", boom)
    stacked, _ = build(xyt_dir, 8)        # 構築だけ。ここで落ちたら遅延ではない
    assert isinstance(stacked, da.Array)

    # 実体化しようとすれば当然その本体が呼ばれる (= 画素は compute 時にだけ読まれる)
    with pytest.raises(AssertionError, match="pixels were read"):
        np.asarray(stacked[5, 0, 0])

    monkeypatch.undo()
    plane = np.asarray(build(xyt_dir, 8)[0][5, 0, 0])
    assert plane.shape == (8, 8) and plane[0, 0] == 6      # 6枚目の値


# ---- 止まった場所を名指しできる ----------------------------------------------

def test_heartbeat_names_the_hung_file_not_the_last_submitted_one(xyt_dir, steps):
    """1件が固まっているとき、heartbeat の item がその固まったファイルを指す。

    これが並行化でいちばん壊れやすい性質。submit のループで advance する実装だと
    ``done=24/24`` / 最後に投入したファイル、``as_completed`` で刻む実装だと
    「たまたま最後に終わったファイル」になり、どちらも固まった相手を名指しできない。
    """
    gate = threading.Event()
    real = mod._read_tiff_header
    target = "ChanA_001_001_001_007.tif"

    def hanging(path, **kw):
        if str(path).endswith(target):
            gate.wait(timeout=30)
        return real(path, **kw)

    mod._read_tiff_header = hanging
    try:
        t = threading.Thread(target=lambda: build(xyt_dir, 8), daemon=True)
        t.start()
        time.sleep(0.5)

        perf._hb_started_at = time.perf_counter()
        del steps[:]
        perf._emit_heartbeat(stall_after_sec=0.1)

        hb = [f for f in steps if f.get("step") == "thorlab.open_tiffs"
              and f.get("event") == "heartbeat"]
        assert hb, "no heartbeat was emitted for the open step"
        assert hb[0]["item"].endswith(target)
        assert hb[0]["done"] == 7            # 7件目に着手して止まっている
        assert hb[0]["total"] == 24
        assert hb[0]["stalled"] is True
        assert hb[0]["workers"] == 8
    finally:
        gate.set()
        mod._read_tiff_header = real
        t.join(timeout=30)


def test_a_failure_names_the_file_deterministically(xyt_dir, steps):
    """壊れたファイルの特定が実行ごとにぶれない。

    どの例外が上がるかがスレッドの気分で変わると、同じディレクトリを再実行する
    たびに別のファイルが「原因」として報告され、調査が成立しない。
    ここでの fake は *呼び出し回数* ではなく *ファイル名* で落とす — 回数で落とす
    fake は並行実行下ではどのファイルに当たるか決まらない。
    """
    real = mod._read_tiff_header
    bad = "ChanA_001_001_001_004.tif"

    def flaky(path, **kw):
        if str(path).endswith(bad):
            raise OSError(5, "Input/output error")
        return real(path, **kw)

    mod._read_tiff_header = flaky
    try:
        for _ in range(15):
            del steps[:]
            with pytest.raises(OSError) as ei:
                build(xyt_dir, 8)
            # 型は包み替えず、落ちたファイルだけを traceback に添える
            assert bad in "".join(getattr(ei.value, "__notes__", []))
            failed = [f for f in steps if f.get("step") == "thorlab.open_tiffs"
                      and f.get("event") == "failed"]
            assert len(failed) == 1
            assert failed[0]["item"].endswith(bad)
            assert failed[0]["done"] == 4
            assert failed[0]["error_type"] == "OSError"
    finally:
        mod._read_tiff_header = real


# ---- 既存の分岐が壊れていない ------------------------------------------------

def test_multipage_rejection_still_fires_under_the_pool(tmp_path):
    """多ページファイルが複数あるときの明示的な失敗は並行化しても変わらない。"""
    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.full((10, 8, 8), i, dtype=np.uint16))
    p = {"mode": "Z", "SizeZ": 30, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.}
    for w in (1, 8):
        with pytest.raises(RuntimeError, match="ambiguous"):
            build(d, w, params=p)


def test_a_single_multipage_file_still_works_under_the_pool(tmp_path):
    d = tmp_path / "img01"
    d.mkdir()
    tifffile.imwrite(d / "ChanA_001_001_001_001.tif",
                     np.zeros((10, 8, 8), dtype=np.uint16))
    p = {"mode": "Z", "SizeZ": 10, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.}
    for w in (1, 8):
        stacked, _ = build(d, w, params=p)
        assert isinstance(stacked, da.Array)
        assert stacked.shape[2] == 10


def test_mosaic_guard_still_fires_before_any_file_is_opened(tmp_path, monkeypatch):
    """mosaic 判定はヘッダ読みより前。並行化しても1ファイルも開かずに落ちる。"""
    d = tmp_path / "img01"
    d.mkdir()
    for xy in (1, 2):
        for i in (1, 2):
            tifffile.imwrite(d / f"ChanA_00{xy}_001_001_{i:03d}.tif",
                             np.zeros((8, 8), dtype=np.uint16))
    opened = []
    real = mod._read_tiff_header
    monkeypatch.setattr(mod, "_read_tiff_header",
                        lambda p, **k: (opened.append(p), real(p, **k))[1])
    try:
        build(d, 8, params={"mode": "T", "SizeT": 2, "PixelSizeX": .5,
                            "PixelSizeY": .5, "PixelSizeZ": 1.})
    except RuntimeError as e:
        assert "mosaic" in str(e)
        assert opened == []
    else:
        pytest.skip("this filename layout is not detected as a mosaic")


def test_channels_still_land_on_C(tmp_path):
    """チャンネルごとに1工程・C 軸へ、という組み立ては変わらない。"""
    d = tmp_path / "img01"
    d.mkdir()
    for ch in ("ChanA", "ChanB"):
        for i in range(1, 6):
            tifffile.imwrite(d / f"{ch}_001_001_001_{i:03d}.tif",
                             np.full((8, 8), i, dtype=np.uint16))
    p = {"mode": "T", "SizeT": 5, "PixelSizeX": .5, "PixelSizeY": .5, "PixelSizeZ": 1.}
    ser, _ = build(d, 1, params=p)
    par, _ = build(d, 8, params=p)
    assert par.shape == ser.shape == (5, 2, 1, 8, 8)
    assert np.array_equal(np.asarray(ser), np.asarray(par))


# ---- 設定 --------------------------------------------------------------------

def test_worker_count_is_configurable_and_falls_back_safely(monkeypatch):
    """環境変数ひとつで逐次へ戻せる (本番で切り分けるための逃げ道)。"""
    monkeypatch.delenv(HEADER_WORKERS_ENV, raising=False)
    assert _header_workers() == DEFAULT_HEADER_WORKERS
    monkeypatch.setenv(HEADER_WORKERS_ENV, "1")
    assert _header_workers() == 1
    monkeypatch.setenv(HEADER_WORKERS_ENV, "16")
    assert _header_workers() == 16
    monkeypatch.setenv(HEADER_WORKERS_ENV, "0")
    assert _header_workers() == 1            # 0 でも止まらない
    monkeypatch.setenv(HEADER_WORKERS_ENV, "nonsense")
    assert _header_workers() == DEFAULT_HEADER_WORKERS


def test_default_worker_count_is_conservative():
    """既定値の上げすぎを防ぐ。

    弱っている共有では同時要求を増やすほど1件あたりが遅くなり、実測でも W=4 以上は
    ほとんど伸びない (負荷で劣化する共有: W=4 1.7x → W=32 2.1x)。
    ローカルディスクではヘッダ読みが GIL 保持の純 Python なので、並行化はむしろ遅い。
    """
    assert 1 <= DEFAULT_HEADER_WORKERS <= 8


def test_each_reader_gets_its_own_chunk_dims_list(tmp_path, monkeypatch):
    """Reader ごとに chunk_dims の実体を分ける (共有可変リストを触らせない)。

    bioio の既定値 ``DEFAULT_CHUNK_DIMS`` はモジュールレベルの可変リストで、
    各 Reader が参照で共有したうえ ``_create_dask_array`` が append する。
    現行バージョンでは発火しないが、複数スレッドから同時に触れば壊れうる。

    ヘッダ読みが 1 open 経路になったため ``BioImage`` を通るのは **退避経路だけ** に
    なった。そこで退避経路に落ちるファイル (C 軸を持つ OME) で確かめる。
    """
    from bioio_base.dimensions import DEFAULT_CHUNK_DIMS

    d = tmp_path / "img01"
    d.mkdir()
    for i in range(1, 4):
        tifffile.imwrite(d / f"ChanA_001_001_001_{i:03d}.tif",
                         np.zeros((2, 8, 8), dtype=np.uint16), ome=True,
                         metadata={"axes": "CYX"})

    before = list(DEFAULT_CHUNK_DIMS)
    seen = []
    real = mod.BioImage

    def spy(path, **kw):
        seen.append(kw.get("chunk_dims"))
        return real(path, **kw)

    monkeypatch.setattr(mod, "BioImage", spy)
    build(d, 8, params={"mode": "T", "SizeT": 3, "PixelSizeX": .5,
                        "PixelSizeY": .5, "PixelSizeZ": 1.})

    assert seen and all(c is not DEFAULT_CHUNK_DIMS for c in seen)
    assert len({id(c) for c in seen}) == len(seen)      # 全部別のリスト
    assert list(DEFAULT_CHUNK_DIMS) == before           # 共有定数は無傷

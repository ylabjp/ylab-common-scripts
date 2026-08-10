"""ThorLabs 取り込みが工程ごとに計測され、ファイル単位の進捗を出すことのテスト。

sorter が「途中で止まる」ときの最有力箇所が、取り込みライブラリが生データを1ファイル
ずつ開くループ (ネットワークドライブ越しに数万回) である。ここに計測が入っていないと
Better Stack には「取り込みを開始した」以降が何も残らず、止まった場所を特定できない。

perf 側の単体テスト (test_perf.py) だけでは、取り込み側の ``with timed_step(...)`` を
消しても気付けないので、工程名と進捗が実際に出ることをここで固定する。
"""
from __future__ import annotations

import numpy as np
import pytest
import tifffile

import ylabcommon.utils.perf as perf
from ylabcommon.bioio.thorlab import builder as builder_mod
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import (
    stack_thorlab_with_bioio_calibrated,
)


@pytest.fixture(autouse=True)
def clean_registry():
    perf._active_steps.clear()
    yield
    perf._active_steps.clear()


@pytest.fixture
def steps(monkeypatch):
    """perf の送信を捕まえて (step, event, fields) で集める。"""
    records = []

    def capture(_msg, **f):
        records.append((f.get("step"), f.get("event"), f))

    monkeypatch.setattr(perf, "log_info", capture)
    monkeypatch.setattr(perf, "log_warning", capture)
    return records


@pytest.fixture
def thorlab_dir(tmp_path):
    """ChanA 3枚 + ChanB 3枚の、実際に開ける最小の ThorLabs 取得ディレクトリ。"""
    d = tmp_path / "img01"
    d.mkdir()
    for ch in ("ChanA", "ChanB"):
        for i in range(1, 4):
            tifffile.imwrite(
                d / f"{ch}_001_001_001_{i:03d}.tif",
                np.zeros((8, 8), dtype=np.uint16),
            )
    return d


PARAMS = {
    "mode": "Z",
    "SizeZ": 3,
    "PixelSizeX": 0.5,
    "PixelSizeY": 0.5,
    "PixelSizeZ": 1.0,
}


def done_fields(steps, name):
    for step, event, fields in steps:
        if step == name and event == "done":
            return fields
    raise AssertionError(
        "step %r never completed; got %r" % (name, [(s, e) for s, e, _ in steps])
    )


# ---- 1ファイルずつ開くループ (止まる最有力箇所) ------------------------------

def test_stacking_measures_the_size_filter_and_the_per_file_open(steps, thorlab_dir):
    """stat のループと BioImage を開くループが別々の工程として計測される。

    どちらもネットワークドライブへの往復なので、まとめて1工程にすると
    「ファイル一覧を見ている最中」なのか「画像を開いている最中」なのか切り分けられない。
    """
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )

    completed = [s for s, event, _f in steps if event == "done"]
    assert "thorlab.filter_by_size" in completed
    assert "thorlab.open_tiffs" in completed


def test_per_file_progress_records_position_and_the_current_file(steps, thorlab_dir):
    """止まったときに「何件目のどのファイルを掴んでいるか」が言えること。

    heartbeat はこの done/total/item を読んで停止箇所を名指しするので、
    ループが1件ずつ advance を呼んでいることがこの機能の前提になる。
    """
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )

    size_filter = done_fields(steps, "thorlab.filter_by_size")
    assert size_filter["total"] == 6           # ChanA/ChanB 各3枚
    assert size_filter["done"] == 6            # 1件ずつ進捗を刻んでいる
    assert size_filter["item"].endswith(".tif")

    # チャンネルごとに1工程。3枚ずつなので total=3 が2回。
    opens = [f for s, event, f in steps if s == "thorlab.open_tiffs" and event == "done"]
    assert len(opens) == 2
    assert [f["total"] for f in opens] == [3, 3]
    assert [f["done"] for f in opens] == [3, 3]
    assert {f["channel"] for f in opens} == {"ChanA", "ChanB"}
    assert all(f["item"].endswith(".tif") for f in opens)


def test_stacking_steps_point_at_the_acquisition_directory(steps, thorlab_dir):
    """target は img* ディレクトリ。sorter 側の load_image と同じ値で突き合わせられる。"""
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )

    assert done_fields(steps, "thorlab.open_tiffs")["target"] == str(thorlab_dir)


def test_stacking_still_produces_the_expected_stack(steps, thorlab_dir):
    """計測を挟んでも結果 (TCZYX の遅延スタック) は変わらない。"""
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    stacked, filtered = stack_thorlab_with_bioio_calibrated(
        files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
    )

    assert stacked.shape == (1, 2, 3, 8, 8)    # T, C(ChanA/ChanB), Z, Y, X
    assert len(filtered) == 6


def test_size_filter_still_drops_small_files(steps, thorlab_dir):
    """明示ループへ書き換えても min_kb のフィルタ動作は変わらない。"""
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    with pytest.raises(Exception):
        # 8x8 の TIFF は 100 KB に満たないので全部落ちる → スタックできない
        stack_thorlab_with_bioio_calibrated(
            files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=100
        )

    assert done_fields(steps, "thorlab.filter_by_size")["done"] == 6


def test_a_failure_inside_the_open_loop_is_reported_with_the_file_it_died_on(
    steps, thorlab_dir, monkeypatch
):
    """読み込み中に落ちたとき、失敗ログがどこまで進んでいたかを持っている。

    fake は *呼び出し回数* ではなく *ファイル名* で落とす。ヘッダ読みはワーカー
    スレッドで並行に走るので、``calls["n"] == 2`` のような回数条件だとどのファイルに
    当たるかが実行ごとに変わる (実測: 3ファイルを同時入場させると 001/002/003 の
    いずれにも当たり、カウンタ自体の競合で一度も落ちない回すらあった)。
    """
    import ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder as mod

    real = mod._read_tiff_header

    def flaky(path, **kwargs):
        if str(path).endswith("_002.tif"):
            raise OSError(5, "Input/output error")
        return real(path, **kwargs)

    monkeypatch.setattr(mod, "_read_tiff_header", flaky)
    files = sorted(str(p) for p in thorlab_dir.glob("*.tif"))

    with pytest.raises(OSError):
        stack_thorlab_with_bioio_calibrated(
            files, thorlab_dir / "Experiment.xml", PARAMS, min_kb=0
        )

    failed = [f for s, event, f in steps if s == "thorlab.open_tiffs" and event == "failed"]
    assert len(failed) == 1
    assert failed[0]["done"] == 2                    # 2件目に着手して落ちた
    assert failed[0]["error_type"] == "OSError"
    assert failed[0]["item"].endswith("_002.tif")    # 落ちたファイルを名指しできる


# ---- Builder の工程分割 ------------------------------------------------------

def test_discover_and_stack_separates_listing_xml_and_stacking(steps, monkeypatch,
                                                               tmp_path):
    """一覧取得 / XML 解析 / スタックが別工程として出る。

    ネットワークドライブが応答しないと、まだ1枚も開いていない一覧取得の段階で止まる
    ことがある。段階が分かれていないとその切り分けができない。
    """
    lazy = np.zeros((1, 1, 3, 4, 4), dtype=np.uint16)

    monkeypatch.setattr(builder_mod, "collect_valid_tiffs", lambda d: ["a.tif", "b.tif"])
    monkeypatch.setattr(
        builder_mod, "stack_thorlab_with_bioio_calibrated",
        lambda files, xml, params: (lazy, files),
    )

    b = builder_mod.ThorlabBioioBuilder(tmp_path)
    monkeypatch.setattr(b, "_get_params", lambda: PARAMS)

    b._discover_and_stack()

    completed = [s for s, event, _f in steps if event == "done"]
    assert completed == [
        "thorlab.discover_tiffs",
        "thorlab.parse_params",
        "thorlab.stack",
    ]
    assert done_fields(steps, "thorlab.stack")["n_files"] == 2

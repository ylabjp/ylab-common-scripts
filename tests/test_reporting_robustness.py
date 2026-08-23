"""レビューで見つかった記録の信頼性の欠陥に対する回帰テスト。

いずれも「動くが、記録が静かに嘘になる/消える」たぐいで、
既存の 71 テストを素通りしていた。
"""
import json
import math
import os

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pytest

from ylabcommon.reporting import FigureStore, figure_id, manifest_path, read_manifest
from ylabcommon.reporting.manifest import dumps_record


@pytest.fixture
def fig():
    f, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    yield f
    plt.close(f)


def _lines(prj_dir):
    return [l for l in manifest_path(prj_dir).read_text().splitlines() if l.strip()]


# --- 1. manifest が JSON Lines であり続ける ------------------------------- #

def test_nonfinite_p_becomes_null_and_manifest_stays_strict_json(tmp_path, fig):
    """scipy が nan を返しても `NaN` を書かない。null にして理由を params に残す。

    `NaN` は RFC 8259 に無い。しかも jq と pandas はそれをエラーにせず null へ
    変換するので、spec が「検定はしたが値が出なかった」に予約した null と
    区別が付かなくなる。
    """
    with FigureStore(tmp_path, pdf_name="r.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"),
                   stats=[{"name": "x", "test": "ttest_ind",
                           "p": float("nan"), "statistic": float("inf")}])

    line = _lines(tmp_path)[0]
    assert "NaN" not in line and "Infinity" not in line
    # 厳格なパーサでも読めること
    rec = json.loads(line, parse_constant=lambda c: pytest.fail(f"non-JSON token {c}"))
    stat = rec["stats"][0]
    assert stat["p"] is None
    assert "statistic" not in stat            # 非有限なので落ちる
    assert stat["params"]["nonfinite"] == ["p", "statistic"]


def test_numpy_scalars_do_not_crash_the_run(tmp_path, fig):
    """`n=[np.sum(mask), ...]` は自然な書き方。np.int64 で落ちてはいけない。

    np.float64 は float を継承するので通るのに np.int64 は int を継承しないため
    TypeError になり、しかも savefig の後なので図だけ残って孤児になっていた。
    """
    with FigureStore(tmp_path, pdf_name="r.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"),
                   stats=[{"name": "x", "test": "mannwhitneyu",
                           "n": [np.int64(12), np.int64(11)],
                           "p": np.float64(0.031)}])
    stat = read_manifest(tmp_path)[0]["stats"][0]
    assert stat["n"] == [12, 11]
    assert stat["p"] == pytest.approx(0.031)


def test_unserialisable_stat_fails_before_any_file_is_written(tmp_path, fig):
    """直列化できない値は savefig の前に弾く。図だけ残る孤児を作らない。"""
    with FigureStore(tmp_path, pdf_name="r.pdf") as store:
        with pytest.raises((TypeError, ValueError)):
            store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"),
                       stats=[{"name": "x", "test": "t", "params": {"bad": object()}}])
    assert not list((tmp_path / "figures").glob("*")) or \
        not any((tmp_path / "figures").iterdir())


def test_dumps_record_refuses_nonfinite_rather_than_writing_nan():
    with pytest.raises(ValueError):
        dumps_record({"id": "a_b_c", "x": float("nan")})


# --- 2. PDF が開けないときに前回の記録を壊さない --------------------------- #

def test_failed_pdf_open_leaves_the_previous_manifest_intact(tmp_path, fig):
    """PDF がロックされていても、前回の run のレコードは消さない。

    以前は manifest を先に切り詰めてから PdfPages を開いていたので、
    ここで例外になると図も PDF も無いまま前回の記録だけ失われた。
    """
    with FigureStore(tmp_path, scope="r.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"))
    before = _lines(tmp_path)
    assert len(before) == 1

    # 開けない PDF を作る。chmod は root だと効かないのでディレクトリにする
    # (同じ scope="r.pdf" なので、以前ならこの run が上のレコードを消していた)。
    #
    # なお `PdfPages(...)` はファイルを遅延オープンするので、構築が成功しても
    # 検査にはならない。FigureStore 側で追記オープンして確かめている。
    (tmp_path / "r.pdf").mkdir()
    with pytest.raises(OSError):
        FigureStore(tmp_path, pdf_name="r.pdf")
    assert _lines(tmp_path) == before


# --- 3. 壊れた行が他の scope を巻き添えにしない --------------------------- #

def test_a_truncated_line_does_not_delete_another_scopes_record(tmp_path, fig):
    """追記の途中で切れた行があっても、別 run のレコードは残す。

    以前は read_manifest が壊れた行を捨て、その結果を丸ごと書き戻していたので、
    無関係なレコードまで消えて図IDが未使用に戻っていた。
    """
    with FigureStore(tmp_path, scope="other.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "othergroup", "psth"))
    with manifest_path(tmp_path).open("a") as f:
        f.write('{"id": "prj1-2-3_broken_psth", "scope": "othe')   # 切れた行

    FigureStore(tmp_path, scope="mine.pdf")          # 自分の scope を書き直す

    text = manifest_path(tmp_path).read_text()
    assert "prj1-2-3_othergroup_psth" in text        # 別 scope は残る
    assert '"scope": "othe' in text                  # 読めない行も温存する


def test_foreign_id_collision_still_detected_after_rewrite(tmp_path, fig):
    with FigureStore(tmp_path, scope="a.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"))
    with FigureStore(tmp_path, scope="b.pdf") as store:
        with pytest.raises(ValueError, match="already used by another report"):
            store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"))


# --- 4. 一時ファイルが並行 run で衝突しない ------------------------------- #

def test_manifest_tmp_file_is_process_unique(tmp_path, fig):
    from ylabcommon.reporting import manifest as m

    with FigureStore(tmp_path, scope="a.pdf") as store:
        store.save(fig, key=figure_id("prj1-2-3", "conda", "psth"))
    m.write_manifest(tmp_path, read_manifest(tmp_path))
    leftovers = list((tmp_path / "report").glob("*.tmp"))
    assert not leftovers, f"一時ファイルが残っている: {leftovers}"
    assert str(os.getpid()) in str(
        manifest_path(tmp_path).with_suffix(f".jsonl.{os.getpid()}.tmp"))


# --- 5. 移行しても PDF のフォント設定が変わらない -------------------------- #

def test_figure_store_applies_the_house_rcparams(tmp_path):
    """create_pdf_pages から移行しても Type-42 / Arial のままであること。

    matplot_util は import 時に rcParams を当てていた。FigureStore がそれを
    当てないと、移行した瞬間 PDF が Type-3 / DejaVu に黙って変わる。
    """
    matplotlib.rcParams["pdf.fonttype"] = 3
    matplotlib.rcParams["font.family"] = "sans-serif"
    FigureStore(tmp_path, scope="a.pdf")
    assert matplotlib.rcParams["pdf.fonttype"] == 42
    assert matplotlib.rcParams["font.family"] == ["Arial"]


def test_house_style_can_be_declined(tmp_path):
    matplotlib.rcParams["pdf.fonttype"] = 3
    try:
        FigureStore(tmp_path, scope="a.pdf", house_style=False)
        assert matplotlib.rcParams["pdf.fonttype"] == 3
    finally:
        from ylabcommon.utils.mpl_style import apply_house_style
        apply_house_style()


def test_matplot_util_still_applies_the_style_on_import():
    matplotlib.rcParams["pdf.fonttype"] = 3
    import importlib

    import ylabcommon.utils.matplot_util as mu

    importlib.reload(mu)
    assert matplotlib.rcParams["pdf.fonttype"] == 42


# --- 6. figure を閉じられる ---------------------------------------------- #

def test_close_figure_releases_the_figure(tmp_path):
    f, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    n_before = len(plt.get_fignums())
    with FigureStore(tmp_path, scope="a.pdf") as store:
        store.save(f, key=figure_id("prj1-2-3", "conda", "psth"), close_figure=True)
    assert len(plt.get_fignums()) == n_before - 1

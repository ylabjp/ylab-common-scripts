"""standard_bar: ``edgecolor`` を渡したときだけ、棒の縁・誤差棒・点が変わる。"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.collections import PathCollection  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.container import BarContainer, ErrorbarContainer  # noqa: E402

from ylabcommon.utils.matplot_util import darken_color, standard_bar  # noqa: E402

FILL = "#808080"


def _draw(y_data, **kw):
    fig, ax = plt.subplots()
    standard_bar(ax, "A", FILL, y_data, **kw)
    return fig, ax


def _bar_parts(ax):
    (bars,) = [c for c in ax.containers if isinstance(c, BarContainer)]
    (err,) = [c for c in ax.containers if isinstance(c, ErrorbarContainer)]
    _, caps, barcols = err.lines
    return bars.patches[0], caps, barcols


def _points(ax):
    return [c for c in ax.collections if isinstance(c, PathCollection)]


def test_default_draws_edge_errorbar_and_points_in_the_fill_color():
    fig, ax = _draw(pd.Series([0.1, 0.2, 0.4]))
    bar, caps, barcols = _bar_parts(ax)
    assert bar.get_facecolor() == to_rgba(FILL)
    assert bar.get_edgecolor() == to_rgba(FILL)
    assert all(to_rgba(c.get_markeredgecolor()) == to_rgba(FILL) for c in caps)
    assert all(np.allclose(col.get_color(), to_rgba(FILL)) for col in barcols)
    (pts,) = _points(ax)
    assert np.allclose(pts.get_facecolors(), to_rgba(darken_color(FILL, amount=0.4)))
    plt.close(fig)


def test_edgecolor_keeps_the_fill_and_outlines_bar_errorbar_and_points():
    fig, ax = _draw(pd.Series([0.1, 0.2, 0.4]), edgecolor="black")
    bar, caps, barcols = _bar_parts(ax)
    assert bar.get_facecolor() == to_rgba(FILL)
    assert bar.get_edgecolor() == to_rgba("black")
    assert all(to_rgba(c.get_markeredgecolor()) == to_rgba("black") for c in caps)
    assert all(np.allclose(col.get_color(), to_rgba("black")) for col in barcols)
    (pts,) = _points(ax)
    assert len(pts.get_offsets()) == 3
    assert np.allclose(pts.get_facecolors(), to_rgba("white"))   # 黒い棒の上でも見える
    assert np.allclose(pts.get_edgecolors(), to_rgba("black"))
    plt.close(fig)


def test_edgecolor_for_an_empty_condition_outlines_the_zero_bar():
    fig, ax = _draw(None, edgecolor="black")
    bar, _, _ = _bar_parts(ax)
    assert bar.get_facecolor() == to_rgba(FILL)
    assert bar.get_edgecolor() == to_rgba("black")
    assert _points(ax) == []
    plt.close(fig)

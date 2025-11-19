from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest

import ylabcommon.matplot_util as mplutil


# --------------------------------------------------------------------------- #
# darken_color                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("color", ["#808080", "red"])
def test_darken_color_outputs_darker(color):
    """Ensure the returned value differs from the original and is HEX."""
    darker = mplutil.darken_color(color, amount=0.3)
    assert darker != color
    assert darker.startswith("#")


# --------------------------------------------------------------------------- #
# convert_pg2mpl                                                              #
# --------------------------------------------------------------------------- #
def test_convert_pg2mpl_rotation():
    # 90-degree rotation should map (1,2) → (−2,1) relative to origin
    xy = mplutil.convert_pg2mpl(x0=0, y0=0, width=2, height=4, angle_deg=90)
    assert np.allclose(xy, np.array([-2.0, 1.0]), atol=1e-6)


# --------------------------------------------------------------------------- #
# set_axis_properties                                                         #
# --------------------------------------------------------------------------- #
def test_set_axis_properties_hides_right_and_top():
    fig, ax = plt.subplots()
    mplutil.set_axis_properties(ax)

    assert ax.spines["right"].get_color() == "none"
    assert ax.spines["top"].get_color() == "none"
    plt.close(fig)

import matplotlib
import matplotlib.pyplot as plt
import colorsys
import re
import matplotlib.colors as mcolors
from matplotlib.markers import MarkerStyle
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.axes import Axes
import seaborn as sns
from scipy.stats import mannwhitneyu, kruskal
import pandas as pd
import numpy as np

matplotlib.rcParams["font.family"] = "Arial"
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42


class STANDARD_FIGURE_SIZE:
    LINE_WIDTH = 0.75
    LINE_WIDTH_THIN = 0.5
    FONT_SIZE_L = 7
    FONT_SIZE_M = 6
    FONT_SIZE_S = 5  # Legend, pval
    BAR_WIDTH = 0.5


marker_type_group = [
    MarkerStyle('o'),    # circle
    MarkerStyle('^'),    # triangle_up
    MarkerStyle('X'),    # x (filled)
    MarkerStyle('s'),    # square
    MarkerStyle('P'),    # plus (filled)
    MarkerStyle('D'),    # diamond
    MarkerStyle('*'),    # star
    MarkerStyle('v'),    # triangle_down
    MarkerStyle('8'),    # octagon
    MarkerStyle('H'),    # hexagon2
    MarkerStyle('.'),    # point
    MarkerStyle(','),    # pixel
    MarkerStyle('<'),    # triangle_left
    MarkerStyle('>'),    # triangle_right
    MarkerStyle('1'),    # tri_down
    MarkerStyle('2'),    # tri_up
    MarkerStyle('3'),    # tri_left
    MarkerStyle('4'),    # tri_right
    MarkerStyle('p'),    # pentagon
    MarkerStyle('h'),    # hexagon1
    MarkerStyle('+'),    # plus
    MarkerStyle('x'),    # x
    MarkerStyle('d'),    # thin_diamond
    MarkerStyle('|'),    # vline
    MarkerStyle('_'),    # hline
]


def create_pdf_pages(fig_name_base: str) -> PdfPages:
    """
    Creates a PdfPages object for saving figures. Tries to create a PdfPages object 
    with a specific filename. If it fails, creates a PdfPages object with a filename 
    that includes the current datetime.

    Parameters:
        fig_name_base (str): The base name for the figure files. without suffix

    Returns:
        PdfPages: The created PdfPages object.
    """
    # Check if file is writable
    try:
        # Try to open the file in append mode to check write permission
        fname = f"{fig_name_base}.pdf"
        with open(fname, 'a'):
            pass
        pp = PdfPages(fname)
    except (IOError, PermissionError):
        # If file is not writable, use alternative filename with timestamp
        current_datetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{fig_name_base}_{current_datetime}.pdf"
        pp = PdfPages(fname)

    return pp


def close_fig(pp, wspace=0.5, hspace=1.5, bottom=0.15, top=0.85, left=0.07, right=0.93):

    plt.subplots_adjust(
        wspace=wspace,
        hspace=hspace,
        bottom=bottom,
        top=top,
        left=left,
        right=right
    )
    try:
        plt.savefig(pp, format="pdf")
    except PermissionError as e:
        print("Error saving figure to PDF:", e)
    plt.close()


def standard_bar(ax: Axes, cond_label, color, y_data: pd.DataFrame):
    if y_data is None:
        ax.bar(
            cond_label,
            0,
            yerr=[[0], [0]],
            width=0.5,
            color=color,
            edgecolor=color,
            ecolor=color,
            align="center",
            alpha=1,
            zorder=-1,
            capsize=2.5,
            linewidth=STANDARD_FIGURE_SIZE.LINE_WIDTH,
            error_kw={
                "elinewidth": STANDARD_FIGURE_SIZE.LINE_WIDTH,
                "capthick": STANDARD_FIGURE_SIZE.LINE_WIDTH
            }
        )
        return
    mean = y_data.mean()
    y_err = [[0], [0]]
    if mean > 0:
        y_err[1][0] = y_data.sem()
    else:
        y_err[0][0] = y_data.sem()
    ax.bar(
        cond_label,
        mean,
        yerr=y_err,
        width=0.5,
        color=color,
        edgecolor=color,
        ecolor=color,
        align="center",
        alpha=1,
        zorder=-1,
        capsize=2.5,
        linewidth=STANDARD_FIGURE_SIZE.LINE_WIDTH,
        error_kw={
            "elinewidth": STANDARD_FIGURE_SIZE.LINE_WIDTH,
            "capthick": STANDARD_FIGURE_SIZE.LINE_WIDTH
        }
    )
    sns.stripplot(
        x=[cond_label] * len(y_data),
        y=y_data.values,
        ax=ax,
        # order=xlabel_list,
        marker=".",
        facecolor=darken_color(color, amount=0.4),
        size=3.0,
        jitter=0.2  # 横幅を指定できる
    )


def generate_stat_text(data_for_stat):
    out = ""
    p_str = ""
    if len(data_for_stat) == 2:
        stat, p = mannwhitneyu(*data_for_stat)
        out = "MW test, U = %d" % stat
        p_str = r"$\lt{P}$ = %.3f" % p
    elif len(data_for_stat) > 2:
        stat, p = kruskal(*data_for_stat)
        out = "KW test, H = " % stat
    else:
        out = ""
    return out, p_str


def darken_color(color: str, amount: float = 0.25) -> str:
    """
    Darkens the given color by a specified amount.

    Parameters:
        color (str): The color to darken. Can be a HEX code (e.g., '#RRGGBB' or '#RGB') 
                     or a named color recognized by matplotlib (e.g., 'red', 'blue').
        amount (float, optional): The amount to darken the color. Must be between 0 and 1.
                                  Default is 0.25.

    Returns:
        str: The darkened color as a HEX code.
    """
    # Check if the input color is a HEX code using a regular expression
    if re.match(r'^#(?:[0-9a-fA-F]{3}){1,2}$', color):
        # If it is a HEX code, remove the '#' character
        hex_color = color.lstrip('#')
    else:
        # If it is a named color, convert it to HEX using matplotlib
        hex_color = mcolors.to_hex(color).lstrip('#')

    # Convert the HEX color to RGB (values between 0 and 1)
    r, g, b = [int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4)]

    # Convert the RGB color to HLS (Hue, Lightness, Saturation)
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    # Decrease the lightness to darken the color
    l = max(0, l - amount)

    # Convert the HLS color back to RGB
    new_r, new_g, new_b = colorsys.hls_to_rgb(h, l, s)

    # Convert the new RGB color back to HEX and return it
    return "#{:02x}{:02x}{:02x}".format(int(new_r * 255), int(new_g * 255), int(new_b * 255))


def set_axis_properties(ax: plt.Axes) -> None:
    """
    Sets various properties for the given axis, accommodating both line and bar plots.
    """
    # Hide specified spines
    for position in ["right", "top"]:
        ax.spines[position].set_color("none")

    # Set the line width for all spines
    for spine in ax.spines.values():
        spine.set_linewidth(STANDARD_FIGURE_SIZE.LINE_WIDTH)

    # Customize tick parameters
    # Adjust padding between ticks and axis
    ax.tick_params(axis='both', which='major', pad=1)
    # Shorten the tick length
    ax.tick_params(axis='both', which='both', length=2)
    ax.tick_params(axis='y', labelsize=STANDARD_FIGURE_SIZE.FONT_SIZE_S)
    ax.tick_params(axis='x', labelsize=STANDARD_FIGURE_SIZE.FONT_SIZE_S)
    # Ensure that all plot elements are not clipped
    for artist in ax.get_children():
        artist.set_clip_on(False)


def convert_pg2mpl(x0, y0, width, height, angle_deg) -> np.ndarray:
    # Converte pg ROI parameters to matplotlib ellipse parameters
    # x0, y0: center position
    # width, height: axes lengths
    # angle_deg: rotation angle in degrees
    # Returns: (xy, width, height, angle) for matplotlib Ellipse
    # Reference:
    # pyqtgraph
    # pos	(length-2 sequence) The position of the ROI’s origin.
    # size	(length-2 sequence) The size of the ROI’s bounding rectangle
    # angle	(float) The rotation of the ROI in degrees. Default is 0.
    # matplotlib
    # xy : (float, float) xy coordinates of ellipse centre.
    # width : float Total length (diameter) of horizontal axis.
    # height : float  Total length (diameter) of vertical axis.
    # angle : scalar, optional Rotation in degrees anti-clockwise.

    local_center = np.array([width / 2.0, height / 2.0])

    angle = np.deg2rad(angle_deg)
    R = np.array([
        [np.cos(angle), -np.sin(angle)],
        [np.sin(angle),  np.cos(angle)]
    ])
    rotated_center = R @ local_center
    return rotated_center+np.array([x0, y0])

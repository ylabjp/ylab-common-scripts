import matplotlib
import matplotlib.pyplot as plt
import colorsys
import re
import matplotlib.colors as mcolors
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

    # Ensure that all plot elements are not clipped
    for artist in ax.get_children():
        artist.set_clip_on(False)
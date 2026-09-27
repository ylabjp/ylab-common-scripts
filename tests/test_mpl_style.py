"""ラボ共通の図のフォント(utils/mpl_style.py)。

図の文字は数式を含めて Arial だけで描く。Arial が無い環境では止める
(置き換え先の分岐を作らない)。呼び出し側はフォントを設定し直さない。
"""
import re

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pytest
from matplotlib.backends.backend_pdf import PdfPages

from ylabcommon.utils import mpl_style
from ylabcommon.utils.mpl_style import (
    HOUSE_RCPARAMS,
    FontNotFoundError,
    apply_house_style,
    require_house_font,
)


def test_mathtext_is_arial_too():
    """既定の mathtext.fontset のままだと斜体の P と Δ だけ DejaVu になる。"""
    assert HOUSE_RCPARAMS["font.family"] == "Arial"
    assert HOUSE_RCPARAMS["mathtext.fontset"] == "custom"
    assert HOUSE_RCPARAMS["mathtext.rm"] == "Arial"
    assert HOUSE_RCPARAMS["mathtext.it"] == "Arial:italic"
    assert HOUSE_RCPARAMS["mathtext.bf"] == "Arial:bold"


def test_missing_font_stops(monkeypatch):
    monkeypatch.setattr(mpl_style, "FONT_FAMILY", "No Such Font For Test")
    with pytest.raises(FontNotFoundError, match="ttf-mscorefonts-installer"):
        require_house_font()


def test_nearest_regular_file_is_not_accepted_as_italic(monkeypatch):
    """斜体のファイルが無いと findfont は正体を返してくる。"""
    class _Font:
        family_name = "Arial"
        style_name = "Regular"

    monkeypatch.setattr(mpl_style.font_manager, "findfont", lambda *a, **k: "arial.ttf")
    monkeypatch.setattr(mpl_style.font_manager, "get_font", lambda path: _Font())
    with pytest.raises(FontNotFoundError, match="italic/normal"):
        require_house_font()


def test_style_is_not_applied_when_the_font_is_missing(monkeypatch):
    monkeypatch.setattr(mpl_style, "FONT_FAMILY", "No Such Font For Test")
    with matplotlib.rc_context({"pdf.fonttype": 3}):
        with pytest.raises(FontNotFoundError):
            apply_house_style()
        assert matplotlib.rcParams["pdf.fonttype"] == 3


def test_every_embedded_font_is_arial(tmp_path):
    """本文・太字・斜体の P・Δ・負の目盛まで、すべて Arial の TrueType で埋め込まれる。"""
    pdf = tmp_path / "t.pdf"
    with matplotlib.rc_context():
        apply_house_style()
        with PdfPages(pdf) as pp:
            for i in range(2):
                f, ax = plt.subplots()
                ax.plot([-1, 1], [-1, 1])
                ax.set_title(f"panel {i}", fontweight="bold")
                ax.set_ylabel(r"$\Delta$Lick")
                ax.text(0, 0, r"$\it{P}$ = 0.012")
                pp.savefig(f)
                plt.close(f)
    b = pdf.read_bytes()
    fonts = {n.decode().split("+", 1)[-1]
             for n in re.findall(rb"/BaseFont\s*/([^\s/<>\[\]()]+)", b)}
    assert fonts and all(f.startswith("Arial") for f in fonts), fonts
    assert len(fonts) >= 3, fonts          # 本文・太字・斜体
    assert b"/Subtype /Type3" not in b

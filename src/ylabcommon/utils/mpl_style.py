"""ラボ共通の matplotlib 既定値。

`matplot_util` はこれを import 時に適用してきた。図の出力経路が
`ylabcommon.reporting` にも増えたので、**設定の正本を1か所にして両方から呼ぶ**。
写しを2つ持つと、片方だけ直したときに PDF のフォントが経路で変わる。
**呼び出し側のリポジトリはフォントを設定し直さない。**

図の文字は Arial だけで描く。**Arial が無い環境では止める**——matplotlib は
見つからないフォントを DejaVu Sans に黙って置き換えるので、止めなければ
Arial のつもりの別フォントの図ができる。置き換え先を用意する分岐は作らない。

重い依存(seaborn / scipy / pandas)を持たないので、`reporting` から気軽に呼べる。
"""
from __future__ import annotations

from typing import Any

import matplotlib
from matplotlib import font_manager

FONT_FAMILY = "Arial"

#: `pdf.fonttype` / `ps.fonttype` の 42 は TrueType 埋め込み。既定の 3 (Type-3) は
#: Illustrator で文字を編集できず、投稿規定で弾かれることがある。
#: mathtext(p 値の斜体 *P*、`$\\Delta$` など)も Arial にする。既定の
#: `mathtext.fontset="dejavusans"` のままだと数式部分だけ DejaVu になる。
HOUSE_RCPARAMS: dict[str, Any] = {
    "font.family": FONT_FAMILY,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "mathtext.fontset": "custom",
    "mathtext.rm": FONT_FAMILY,
    "mathtext.it": f"{FONT_FAMILY}:italic",
    "mathtext.bf": f"{FONT_FAMILY}:bold",
    "mathtext.sf": FONT_FAMILY,
    "mathtext.cal": FONT_FAMILY,
}

#: 図が使う書体(本文・p 値の斜体・タイトルの太字)。
_REQUIRED_STYLES = (("normal", "normal"), ("italic", "normal"), ("normal", "bold"))


class FontNotFoundError(RuntimeError):
    """図に使うフォントが環境に無い。"""


def require_house_font() -> None:
    """Arial の本文・斜体・太字が無ければ `FontNotFoundError`。

    findfont は最も近いファイルを返すので、返ったファイルの family 名と書体名まで見る
    (斜体が無いと正体が返り、それを斜体として使うことになる)。
    """
    missing = []
    for style, weight in _REQUIRED_STYLES:
        prop = font_manager.FontProperties(family=FONT_FAMILY, style=style, weight=weight)
        try:
            font = font_manager.get_font(font_manager.findfont(prop, fallback_to_default=False))
        except ValueError:
            missing.append(f"{style}/{weight}")
            continue
        name = font.style_name.lower()
        if (font.family_name != FONT_FAMILY
                or (style == "italic" and "italic" not in name)
                or (weight == "bold" and "bold" not in name)):
            missing.append(f"{style}/{weight}")
    if missing:
        raise FontNotFoundError(
            f"{FONT_FAMILY} ({', '.join(missing)}) is not visible to matplotlib "
            f"{matplotlib.__version__}. Install it and delete the matplotlib font cache "
            f"({matplotlib.get_cachedir()}).\n"
            "  Ubuntu: sudo apt install ttf-mscorefonts-installer && sudo fc-cache -f\n"
            "  Windows: Arial ships with the OS (C:\\Windows\\Fonts\\arial*.ttf)"
        )


def apply_house_style() -> None:
    """Arial があることを確かめてから、`HOUSE_RCPARAMS` をグローバルな rcParams へ適用する。"""
    require_house_font()
    # rcParams のキーは matplotlib 側で Literal 列挙になっているので、素の
    # dict は渡せない (中身は正しいキーだけ)。
    matplotlib.rcParams.update(HOUSE_RCPARAMS)  # type: ignore[arg-type]

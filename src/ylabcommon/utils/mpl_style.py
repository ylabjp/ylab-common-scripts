"""ラボ共通の matplotlib 既定値。

`matplot_util` はこれを import 時に適用してきた。図の出力経路が
`ylabcommon.reporting` にも増えたので、**設定の正本を1か所にして両方から呼ぶ**。
写しを2つ持つと、片方だけ直したときに PDF のフォントが経路で変わる。

重い依存(seaborn / scipy / pandas)を持たないので、`reporting` から気軽に呼べる。
"""
from __future__ import annotations

import matplotlib

#: `pdf.fonttype` / `ps.fonttype` の 42 は TrueType 埋め込み。既定の 3 (Type-3) は
#: Illustrator で文字を編集できず、投稿規定で弾かれることがある。
HOUSE_RCPARAMS = {
    "font.family": "Arial",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
}


def apply_house_style() -> None:
    """`HOUSE_RCPARAMS` をグローバルな rcParams へ適用する。"""
    matplotlib.rcParams.update(HOUSE_RCPARAMS)

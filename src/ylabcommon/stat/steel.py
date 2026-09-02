"""
Kruskal-Wallis 検定 → 事後の多重比較 (Steel / Steel-Dwass)

  Steel 検定       : 対照群 vs 各処理群 (Dunnett のノンパラ版)
  Steel-Dwass 検定 : 全ペア総当たり     (Tukey  のノンパラ版)

必要: scipy >= 1.10 (multivariate_normal.cdf の lower_limit)
      pandas, scikit-posthocs (Steel-Dwass を使う場合)
        pip install scikit-posthocs
"""

from typing import Any
import numpy as np
import pandas as pd
from itertools import combinations
from scipy import stats


# ---------------------------------------------------------------
# Steel 検定 (対照群との比較) — scipy/scikit-posthocs に無いので自前実装
# ---------------------------------------------------------------
def steel_test(control: Any, treatments: Any, labels: Any = None) -> pd.DataFrame:
    """
    Parameters
    ----------
    control    : array-like            対照群のデータ
    treatments : list of array-like    各処理群のデータ
    labels     : list of str, optional 各処理群の名前

    Returns
    -------
    DataFrame : group, n, t (標準化順位和), p_adj (多重性調整済み両側p値)

    アルゴリズム
    ------------
    1. 「対照群 + 処理群 i」の 2 群だけで順位づけ → Wilcoxon 順位和 R_i
    2. t_i = (R_i - E[R_i]) / sqrt(V[R_i])   ※同順位補正あり
    3. t_1..t_k は互いに相関する (共通の対照群を使うため)。
       rho_ij = sqrt( n_i * n_j / ((n_0+n_i)(n_0+n_j)) )
       この相関を持つ多変量正規分布から max|t| の分布を求めて p 値とする。
       (等サンプルサイズなら rho = 0.5 で Dunnett と同じ構造)
    """
    control = np.asarray(control, dtype=float)
    n0 = len(control)
    k = len(treatments)
    if labels is None:
        labels = [f"treat{i + 1}" for i in range(k)]

    t_stats: Any = []
    ns: list[int] = []
    for tr in treatments:
        tr = np.asarray(tr, dtype=float)
        ni = len(tr)
        ns.append(ni)

        combined = np.concatenate([control, tr])
        ranks = stats.rankdata(combined)          # 同順位は平均順位
        Ri = ranks[n0:].sum()                     # 処理群側の順位和
        N = n0 + ni

        E = ni * (N + 1) / 2
        # 同順位を含む厳密な分散
        V = ni * n0 / (N * (N - 1)) * ((ranks ** 2).sum() - N * (N + 1) ** 2 / 4)

        t_stats.append((Ri - E) / np.sqrt(V))

    t_stats = np.array(t_stats)

    # 比較同士の相関行列
    R = np.eye(k)
    for i, j in combinations(range(k), 2):
        R[i, j] = R[j, i] = np.sqrt(
            ns[i] * ns[j] / ((n0 + ns[i]) * (n0 + ns[j]))
        )

    # p = P(max|Z| >= |t_i|),  Z ~ MVN(0, R)
    pvals = []
    for t in t_stats:
        a = abs(t)
        p = 1 - stats.multivariate_normal.cdf(
            np.full(k, a),
            mean=np.zeros(k),
            cov=R,
            lower_limit=np.full(k, -a),
            allow_singular=True,
        )
        pvals.append(float(np.clip(p, 0.0, 1.0)))

    return pd.DataFrame({"group": labels, "n": ns, "t": t_stats, "p_adj": pvals})


# ---------------------------------------------------------------
# 使用例
# ---------------------------------------------------------------
if __name__ == "__main__":
    df = pd.DataFrame({
        "value": [10, 12, 12, 13, 15, 15, 16,
                  14, 16, 16, 18, 19, 20, 21,
                  20, 22, 23, 23, 25, 26, 28],
        "group": ["ctrl"] * 7 + ["A"] * 7 + ["B"] * 7,
    })

    # --- 1) Kruskal-Wallis 検定 -------------------------------
    groups = [g["value"].values for _, g in df.groupby("group", sort=False)]
    h, p = stats.kruskal(*groups)
    print(f"Kruskal-Wallis: H = {h:.4f}, p = {p:.4g}\n")

    # --- 2a) Steel 検定 (対照群 ctrl と比較) -------------------
    ctrl = df.loc[df.group == "ctrl", "value"].values
    treats = [df.loc[df.group == g, "value"].values for g in ["A", "B"]]
    print("[Steel] 対照群との比較")
    print(steel_test(ctrl, treats, labels=["A", "B"]).to_string(index=False), "\n")

    # --- 2b) Steel-Dwass 検定 (全ペア) -------------------------
    # scikit-posthocs の posthoc_dscf が Dwass-Steel-Critchlow-Fligner 法
    import scikit_posthocs as sp

    print("[Steel-Dwass] 全ペア比較 (p値行列)")
    print(sp.posthoc_dscf(df, val_col="value", group_col="group").round(4))
"""sorter が仕分けた後の記録 (``*_df.h5`` とその相方の json) を読む。

取得直後の h5 は ``<train グループ>/data`` と ``<train グループ>/param/*`` という
並びだが、slice-analysis の sorter はそれを pandas の表に直して
``<base>_df.h5`` (キー ``fml_data``) に、パラメータを ``<base>.json`` に書く
(``slice_analysis.data_curation.sorter._process_fml_data``)。**形が違うので、
取得直後の h5 を読む口ではこのファイルを開けない。** 仕分け後のデータを解析に
掛けるには、ここを通す。

表の列 (sorter が作る):

- ``val``: チャンネル 0 の値。取得側の viewer が描いているのと同じ系列
- ``time_ms``: 記録の先頭からの時刻
- ``train_idx``: 0 から始まる train の番号
- ``rel_time_ms``: その train の先頭からの時刻

sorter は **完了した train だけ** を書く (途中で止めた記録の未取得ぶんは落ちる)
ので、ここで読める長さがそのまま「解析に掛けられる長さ」である。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

#: sorter が ``to_hdf`` に渡しているキー。
HDF_KEY = "fml_data"
#: 値の列の名前 (チャンネル 0)。
VALUE_COLUMN = "val"
#: ``*_df.h5`` の接尾辞。相方の json はここを外した名前。
DF_SUFFIX = "_df.h5"


def meta_path_for(df_path: str | Path) -> Path:
    """``<base>_df.h5`` の相方の ``<base>.json``。

    sorter は必ずこの組で書く (``_process_fml_data`` の ``to_base_fname``)。
    """
    p = Path(df_path)
    name = p.name
    if not name.endswith(DF_SUFFIX):
        raise ValueError(
            "%s is not a sorter output: the name must end with %r." % (p, DF_SUFFIX))
    return p.with_name(name[: -len(DF_SUFFIX)] + ".json")


@dataclass(frozen=True)
class SortedRecording:
    """1 つの ``*_df.h5`` と、その相方の json のパラメータ。"""

    path: Path
    frame: pd.DataFrame
    param: dict[str, Any]

    @classmethod
    def open(cls, df_path: str | Path) -> "SortedRecording":
        """``*_df.h5`` と相方の json を読む。

        Raises:
            FileNotFoundError: どちらかが無い。
            ValueError: 名前が ``*_df.h5`` でない、値の列が無い、json に
                パラメータが入っていない。**黙って既定値に倒さない** ——
                サンプリングレートを取り違えると時刻も頻度も全部ずれる。
        """
        p = Path(df_path)
        meta = meta_path_for(p)
        if not p.exists():
            raise FileNotFoundError("No sorter output at %s." % p)
        if not meta.exists():
            raise FileNotFoundError(
                "No metadata next to %s: %s is missing, so the sampling rate is "
                "unknown." % (p, meta))
        frame = pd.read_hdf(p, key=HDF_KEY)
        if not isinstance(frame, pd.DataFrame) or VALUE_COLUMN not in frame.columns:
            raise ValueError(
                "%s has no %r column; it does not look like a sorter output."
                % (p, VALUE_COLUMN))
        loaded = json.loads(meta.read_text(encoding="utf-8"))
        params = loaded.get("param") or []
        if not params:
            raise ValueError("%s holds no 'param' entry." % meta)
        return cls(path=p, frame=frame, param=dict(params[0]))

    @property
    def sampling_rate_khz(self) -> float:
        """サンプリングレート [kHz]。json の ``sampling_rate_in_kHz``。"""
        try:
            rate = float(self.param["sampling_rate_in_kHz"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "%s has no usable sampling_rate_in_kHz, so the time axis and the "
                "event frequency cannot be determined."
                % meta_path_for(self.path)) from None
        if not rate > 0:
            raise ValueError("sampling_rate_in_kHz in %s is %r, which is not usable."
                             % (meta_path_for(self.path), rate))
        return rate

    @property
    def dt_s(self) -> float:
        """1 標本の間隔 [s]。"""
        return 1.0 / (self.sampling_rate_khz * 1000.0)

    @property
    def group(self) -> str:
        """記録の名前。生 h5 の train グループ名と同じ位置づけ (1 記録 = 1 ファイル)。"""
        return self.path.name

    @property
    def config_name(self) -> str:
        """記録に使った ini の名前。無ければ空文字。"""
        return str(self.param.get("config_name", ""))

    @property
    def n_trains(self) -> int:
        return len(self.train_indices)

    @property
    def samples_per_train(self) -> int:
        """1 train の標本数。表に入っている長さから数える (設定値ではない)。"""
        count = self.n_trains
        total = int(self.frame.shape[0])
        return total // count if count else total

    @property
    def train_indices(self) -> list[int]:
        """表に入っている train の番号 (昇順)。"""
        if "train_idx" not in self.frame.columns:
            return [0]
        return [int(v) for v in np.unique(self.frame["train_idx"].to_numpy())]

    def values(self, train_idx: int | None = None) -> np.ndarray:
        """値の系列。``train_idx=None`` で **全 train を記録の順に連結** する。

        mEPSC のようにランダムに出るイベントは、train をまたいで数えないと
        頻度が出ない。一方、train ごとに見たいときは番号を渡す。

        **train の平均は返さない。** 平均するとランダムなイベントは消えるので、
        イベント検出に掛ける波形としては意味を持たない。
        """
        if train_idx is None:
            return np.asarray(self.frame[VALUE_COLUMN].to_numpy(), dtype=np.float64)
        if "train_idx" not in self.frame.columns:
            raise ValueError("%s has no train_idx column." % self.path)
        picked = self.frame.loc[self.frame["train_idx"] == train_idx, VALUE_COLUMN]
        if picked.empty:
            raise ValueError(
                "No train %d in %s; it holds %s."
                % (train_idx, self.path, self.train_indices))
        return np.asarray(picked.to_numpy(), dtype=np.float64)

    def duration_s(self, train_idx: int | None = None) -> float:
        """その波形の長さ [s] = 点数 ÷ サンプリングレート。頻度の分母になる。"""
        return self.values(train_idx).shape[0] * self.dt_s

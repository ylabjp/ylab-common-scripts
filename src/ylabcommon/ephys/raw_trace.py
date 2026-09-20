"""取得直後の h5 (slice-controller が書いたまま) を読む。

仕分け後の ``*_df.h5`` を読む :mod:`~ylabcommon.ephys.sorted_trace` と **同じ形**
にしてある。どちらで開いても ``sampling_rate_khz`` / ``train_indices`` /
``values`` / ``duration_s`` が同じ意味で使えるので、呼ぶ側 (画面・CLI・解析) は
形の違いを気にしなくてよい。振り分けは :func:`ylabcommon.ephys.recording.
open_recording`。

h5 の形:

- ``<train グループ>/data``: ``(チャンネル, 標本)``。チャンネル 0 を使う
- ``<train グループ>/param/*``: ``sampling_rate_in_kHz`` など

**1 ファイルに train グループが複数入る。** slice-controller は実行に並べた
train のぶんだけ ``train_param_000``, ``train_param_001``, ... を書く
(``hardware/daq.py``)。ここは仕分け後と形をそろえるため **1 グループ = 1 記録**
として開く。並びは :meth:`RawRecording.groups` で取る。

**途中で止めた記録は、取得できたぶんだけを使う。** バッファはシーケンス全体ぶん
確保されて 0 埋めなので、全長を読むと未取得の train が「平坦な実測トレース」と
区別できなくなる。``acquired_train_number`` / ``train_sample_number`` があれば
それに従う (slice-controller の h5 output の決まり)。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

#: 1 train の標本数の鍵。``train_train_interval`` は切り上げ前の設定値なので使わない。
KEY_TRAIN_SAMPLES = "train_sample_number"
#: 最後まで記録できた train の数。
KEY_ACQUIRED_TRAINS = "acquired_train_number"
KEY_TRAIN_NUMBER = "train_train_number"
KEY_SAMPLING_RATE = "sampling_rate_in_kHz"


def _scalar(value: Any) -> Any:
    """h5 のスカラーを Python の値にする。

    h5py は Python の ``str`` を **可変長 UTF-8** で書くので、読み戻すと
    ``np.array`` の dtype は ``object`` (kind ``O``)、``item()`` は ``bytes`` に
    なる。dtype の kind だけを見て ``S`` / ``U`` を判定すると素通りし、後段の
    ``str(...)`` が ``b'mEPSC.ini'`` という文字列を作る (実測)。**値の型で判る。**
    """
    item = np.array(value).item()
    if isinstance(item, bytes):
        return item.decode("utf-8", errors="replace")
    return item


@dataclass(frozen=True)
class RawRecording:
    """取得直後の h5 の、train グループ 1 つ。"""

    path: Path
    group: str
    data: np.ndarray            # (標本,) チャンネル 0
    param: dict[str, Any]

    @staticmethod
    def groups(path: str | Path) -> list[str]:
        """その h5 に入っている train グループの名前 (書かれた順)。"""
        with h5py.File(str(path), "r") as f:
            return list(f.keys())

    @classmethod
    def open(cls, path: str | Path, group: str | None = None) -> "RawRecording":
        """グループを 1 つ開く。``group`` を省くと先頭。

        Raises:
            FileNotFoundError: ファイルが無い。
            ValueError: グループが無い、値の並びが無い、サンプリングレートが
                読めない。**既定値に倒さない** —— レートを取り違えると時刻も
                頻度も全部ずれる。
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError("No acquisition h5 at %s." % p)
        with h5py.File(str(p), "r") as f:
            names = list(f.keys())
            if not names:
                raise ValueError("%s holds no train group." % p)
            name = names[0] if group is None else group
            if name not in names:
                raise ValueError("No train group %r in %s; it holds %s."
                                 % (name, p, names))
            if "data" not in f[name]:
                raise ValueError(
                    "%s/%s has no 'data'; this does not look like an acquisition h5 "
                    "(a sorter output is read with SortedRecording)." % (p, name))
            data = np.asarray(f[name + "/data"], dtype=np.float64)
            param = {k: _scalar(f[name + "/param/" + k])
                     for k in list(f[name + "/param"])} if "param" in f[name] else {}
        if data.ndim != 2 or data.shape[0] < 1:
            raise ValueError("%s/%s/data has shape %s; expected (channel, sample)."
                             % (p, name, data.shape))
        return cls(path=p, group=name, data=data[0], param=param)

    @property
    def sampling_rate_khz(self) -> float:
        try:
            rate = float(self.param[KEY_SAMPLING_RATE])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "%s/%s has no usable %s, so the time axis and the event frequency "
                "cannot be determined." % (self.path, self.group, KEY_SAMPLING_RATE)
            ) from None
        if not rate > 0:
            raise ValueError("%s in %s/%s is %r, which is not usable."
                             % (KEY_SAMPLING_RATE, self.path, self.group, rate))
        return rate

    @property
    def dt_s(self) -> float:
        return 1.0 / (self.sampling_rate_khz * 1000.0)

    @property
    def config_name(self) -> str:
        return str(self.param.get("config_name", ""))

    @property
    def samples_per_train(self) -> int:
        """1 train の標本数。無い古いファイルは全長を 1 train とみなす。"""
        try:
            n = int(self.param[KEY_TRAIN_SAMPLES])
        except (KeyError, TypeError, ValueError):
            return int(self.data.shape[0])
        return n if n > 0 else int(self.data.shape[0])

    @property
    def n_trains(self) -> int:
        """取得できた train の数。設定値ではなく **実際に入っている長さ** で決める。"""
        per = self.samples_per_train
        available = int(self.data.shape[0] // per) if per else 0
        for key in (KEY_ACQUIRED_TRAINS, KEY_TRAIN_NUMBER):
            try:
                declared = int(self.param[key])
            except (KeyError, TypeError, ValueError):
                continue
            if declared >= 0:
                available = min(available, declared)
            break
        return max(available, 0)

    @property
    def train_indices(self) -> list[int]:
        return list(range(self.n_trains))

    def values(self, train_idx: int | None = None) -> np.ndarray:
        """値の系列。``train_idx=None`` で **取得できた train を順に連結**。

        未取得ぶん (0 埋めのバッファ) は落とす。``train_idx`` を渡すとその train。
        """
        per, count = self.samples_per_train, self.n_trains
        if train_idx is None:
            return np.asarray(self.data[: per * count], dtype=np.float64)
        if not 0 <= train_idx < count:
            raise ValueError("No train %d in %s/%s; it holds %s."
                             % (train_idx, self.path, self.group, self.train_indices))
        return np.asarray(self.data[train_idx * per:(train_idx + 1) * per],
                          dtype=np.float64)

    def duration_s(self, train_idx: int | None = None) -> float:
        """その波形の長さ [s] = 点数 ÷ サンプリングレート。頻度の分母になる。"""
        return self.values(train_idx).shape[0] * self.dt_s

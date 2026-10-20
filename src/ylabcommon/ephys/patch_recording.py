"""patch-clamp mode の記録 (電流 + 動画の時刻 + seal test の表 + 先端の位置) の形と読み書き。

slice-controller のカメラアプリ (patch-clamp mode) が書き、slice-analysis が読む。
**形はここ 1 か所** に置き、書く側も読む側もこのモジュールの名前を使う (キーの綴りを
2 か所に持たない)。

ファイルの形 (h5)
-----------------
電流は **取得直後の h5 と同じ** ``train_param_000`` の形で入れる。viewer (``uv run v``)
と ``scli info`` と :class:`~ylabcommon.ephys.raw_trace.RawRecording` がそのまま開ける。

::

    /train_param_000/data                (1, N) float32  電流 [pA]
    /train_param_000/param/*             sampling_rate_in_kHz, config_name,
                                         train_sample_number, acquired_* ...
    /patch                               attrs: schema_version, created_*, camera_*,
                                         video_file, test_pulse_mv/hz, ai_* ...
    /patch/sync/ai_start_perf_s          AI の最初の標本の時刻 (perf_counter) [s]
    /patch/sync/ai_start_unix_s          同 (time.time) [s]
    /patch/sync/chunk_end_perf_s   (C,)  各チャンク読み終わりの時刻
    /patch/sync/chunk_end_sample   (C,)  そのときの累積標本数
    /patch/camera/frame_perf_s     (F,)  フレームを受け取った時刻 (perf_counter)
    /patch/camera/frame_unix_s     (F,)
    /patch/camera/frame_count      (F,)  カメラが付けた通し番号
    /patch/camera/frame_hw_ns      (F,)  カメラの時計 [ns] (無ければ -1)
    /patch/camera/frame_video_index(F,)  動画ファイルの中の番号
    /patch/test_pulse/<column>     (P,)  :data:`PULSE_COLUMNS` (None は NaN)
    /patch/tip/<column>            (T,)  :data:`TIP_COLUMNS` (推定無しは NaN)
    /patch/events/{perf_s,sample,label,note}  人が押した印 (phase の教師ラベル)
    /patch/target_cell             attrs: x_px, y_px, perf_s (未設定なら無い)

**時刻は 2 つ持つ。** ``perf_s`` (``time.perf_counter``) は同じプロセスの中で
AI とカメラを揃えるためのもの (単調、分解能が高い)。``unix_s`` (``time.time``) は
別プロセス (FML Controller の h5 の名前、ThorImage) と突き合わせるためのもの。
**動画のフレーム i の電流の位置** は ``frame_perf_s[i]`` と ``ai_start_perf_s`` から
出す (:meth:`PatchRecording.frame_sample_index`)。カメラの時計 (``frame_hw_ns``)
はフレーム間隔の真値を持つが、AI の時計とは独立なので、揃えるのは perf_s で行う。
ハードウェアで揃える (カメラの strobe を DAQ の DI で記録する) のはまだやっていない。

**書き終える前に止まった記録** は ``<name>.h5.writing`` のまま残る。中身は書けた
ところまで入っており、読める (``train_sample_number`` が無いので全長を 1 train と
みなす)。``.writing`` が残っていたら、そのセッションは正常に閉じていない。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from ylabcommon.ephys.raw_trace import RawRecording
from ylabcommon.ephys.test_pulse import PulseMetrics

SCHEMA_VERSION = 1
TRAIN_GROUP = "train_param_000"
PATCH_GROUP = "patch"
CONFIG_NAME = "patch-clamp-mode"
WRITING_SUFFIX = ".writing"

#: ``/patch/test_pulse`` の列。:class:`PulseMetrics` のフィールドと同じ名前。
PULSE_COLUMNS = ("onset_idx", "offset_idx", "holding_pa", "delta_i_ss_pa",
                 "delta_i_peak_pa", "r_total_mohm", "charge_fc", "transient_detected",
                 "rs_mohm", "rm_mohm", "cm_pf", "tau_ms", "rs_extrapolated_mohm",
                 "cm_from_tau_pf", "edge_resolved")
PULSE_INT_COLUMNS = ("onset_idx", "offset_idx")
PULSE_BOOL_COLUMNS = ("transient_detected", "edge_resolved")
#: ``/patch/tip`` の列。``frame_index`` は ``/patch/camera`` の行番号。
TIP_COLUMNS = ("frame_index", "x_px", "y_px", "angle_deg", "confidence")
CAMERA_COLUMNS = ("frame_perf_s", "frame_unix_s", "frame_count", "frame_hw_ns",
                  "frame_video_index")
SYNC_COLUMNS = ("chunk_end_perf_s", "chunk_end_sample")
EVENT_COLUMNS = ("perf_s", "sample", "label", "note")


def _dtype_of(column: str) -> Any:
    if column in PULSE_INT_COLUMNS or column in ("frame_index", "frame_count",
                                                  "frame_hw_ns", "frame_video_index",
                                                  "chunk_end_sample", "sample"):
        return np.int64
    if column in PULSE_BOOL_COLUMNS:
        return np.uint8
    if column in ("label", "note"):
        return h5py.string_dtype(encoding="utf-8")
    return np.float64


def _as_float(value: Any) -> float:
    return math.nan if value is None else float(value)


class PatchSessionWriter:
    """記録を **流しながら** 書く (途中で落ちても書けたところまで残る)。

    最終名 ``<path>`` ではなく ``<path>.writing`` へ書き、:meth:`close` で差し替える。
    1 本のスレッドから呼ぶこと (h5py は同時書き込みを守らない)。
    """

    def __init__(self, path: str | Path, sampling_rate_hz: float,
                 ai_coefficient_pa_per_v: float, attrs: dict[str, Any] | None = None,
                 chunk_samples: int = 5000) -> None:
        self.final_path = Path(path)
        self.path = Path(str(path) + WRITING_SUFFIX)
        self.sampling_rate_hz = float(sampling_rate_hz)
        self.__n_samples = 0
        self.__closed = False
        self.__f = h5py.File(str(self.path), "w")
        g = self.__f.create_group(TRAIN_GROUP)
        g.create_dataset("data", shape=(1, 0), maxshape=(1, None), dtype=np.float32,
                         chunks=(1, int(chunk_samples)))
        p = g.create_group("param")
        rate_khz = self.sampling_rate_hz / 1000.0
        p.create_dataset("ver", data=2026)
        p.create_dataset("config_name", data=CONFIG_NAME)
        p.create_dataset("sampling_rate_in_kHz",
                         data=int(rate_khz) if rate_khz.is_integer() else rate_khz)
        p.create_dataset("general_iclamp", data=0)
        p.create_dataset("general_image_sync", data=0)
        p.create_dataset("ai_coefficient_ch0", data=float(ai_coefficient_pa_per_v))
        p.create_dataset("train_train_number", data="1")

        patch = self.__f.create_group(PATCH_GROUP)
        now = _dt.datetime.now().astimezone()
        patch.attrs["schema_version"] = SCHEMA_VERSION
        patch.attrs["created_unix_s"] = now.timestamp()
        patch.attrs["created_iso"] = now.isoformat(timespec="seconds")
        patch.attrs["sampling_rate_hz"] = self.sampling_rate_hz
        patch.attrs["ai_coefficient_pa_per_v"] = float(ai_coefficient_pa_per_v)
        for k, v in (attrs or {}).items():
            patch.attrs[k] = v if not isinstance(v, (dict, list)) else json.dumps(v)
        self.__tables: dict[str, dict[str, h5py.Dataset]] = {}
        self.__make_table("sync", SYNC_COLUMNS)
        self.__make_table("camera", CAMERA_COLUMNS)
        self.__make_table("test_pulse", PULSE_COLUMNS)
        self.__make_table("tip", TIP_COLUMNS)
        self.__make_table("events", EVENT_COLUMNS)
        self.__f.flush()

    # ------------------------------------------------------------------ tables
    def __make_table(self, name: str, columns: tuple[str, ...]) -> None:
        g = self.__f[PATCH_GROUP].create_group(name)
        self.__tables[name] = {
            c: g.create_dataset(c, shape=(0,), maxshape=(None,), dtype=_dtype_of(c),
                                chunks=(256,)) for c in columns}

    def __append_rows(self, name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        table = self.__tables[name]
        n_old = table[next(iter(table))].shape[0]
        n_new = n_old + len(rows)
        for column, ds in table.items():
            ds.resize((n_new,))
            kind = _dtype_of(column)
            if kind is np.int64:
                values = [int(r[column]) for r in rows]
            elif kind is np.uint8:
                values = [1 if r[column] else 0 for r in rows]
            elif kind is np.float64:
                values = [_as_float(r[column]) for r in rows]
            else:
                values = [str(r.get(column, "")) for r in rows]
            ds[n_old:n_new] = values

    # ------------------------------------------------------------------ API
    @property
    def n_samples(self) -> int:
        return self.__n_samples

    def set_sync_origin(self, ai_start_perf_s: float, ai_start_unix_s: float) -> None:
        g = self.__f[PATCH_GROUP]["sync"]
        for key, value in (("ai_start_perf_s", ai_start_perf_s),
                           ("ai_start_unix_s", ai_start_unix_s)):
            if key in g:
                del g[key]
            g.create_dataset(key, data=float(value))

    def append_current(self, chunk_pa: np.ndarray, chunk_end_perf_s: float) -> None:
        chunk = np.asarray(chunk_pa, dtype=np.float32).ravel()
        ds = self.__f[TRAIN_GROUP]["data"]
        n_old = self.__n_samples
        n_new = n_old + chunk.shape[0]
        ds.resize((1, n_new))
        ds[0, n_old:n_new] = chunk
        self.__n_samples = n_new
        self.__append_rows("sync", [{"chunk_end_perf_s": chunk_end_perf_s,
                                     "chunk_end_sample": n_new}])

    def append_frame(self, perf_s: float, unix_s: float, frame_count: int,
                     hw_ns: int | None, video_index: int) -> int:
        """1 フレームの時刻を足し、その行番号 (``frame_index``) を返す。"""
        table = self.__tables["camera"]
        index = table["frame_perf_s"].shape[0]
        self.__append_rows("camera", [{
            "frame_perf_s": perf_s, "frame_unix_s": unix_s, "frame_count": frame_count,
            "frame_hw_ns": -1 if hw_ns is None else int(hw_ns),
            "frame_video_index": video_index}])
        return index

    def append_pulses(self, metrics: list[PulseMetrics]) -> None:
        self.__append_rows("test_pulse", [m.as_record() for m in metrics])

    def append_tip(self, frame_index: int, x_px: float | None, y_px: float | None,
                   angle_deg: float | None, confidence: float | None) -> None:
        self.__append_rows("tip", [{"frame_index": frame_index, "x_px": x_px, "y_px": y_px,
                                    "angle_deg": angle_deg, "confidence": confidence}])

    def add_event(self, perf_s: float, sample: int, label: str, note: str = "") -> None:
        self.__append_rows("events", [{"perf_s": perf_s, "sample": sample,
                                       "label": label, "note": note}])

    def set_target_cell(self, x_px: float, y_px: float, perf_s: float) -> None:
        g = self.__f[PATCH_GROUP]
        t = g["target_cell"] if "target_cell" in g else g.create_group("target_cell")
        t.attrs["x_px"] = float(x_px)
        t.attrs["y_px"] = float(y_px)
        t.attrs["perf_s"] = float(perf_s)

    def set_attr(self, key: str, value: Any) -> None:
        self.__f[PATCH_GROUP].attrs[key] = value

    def flush(self) -> None:
        self.__f.flush()

    def close(self) -> Path:
        """長さを ``param`` に書いて閉じ、最終名へ差し替える。"""
        if self.__closed:
            return self.final_path
        p = self.__f[TRAIN_GROUP]["param"]
        n = self.__n_samples
        for key, value in (("train_sample_number", str(n)),
                           ("acquired_sample_number", str(n)),
                           ("acquired_train_number", "1" if n > 0 else "0"),
                           ("train_train_interval",
                            str(n / self.sampling_rate_hz * 1000.0))):
            if key in p:
                del p[key]
            p.create_dataset(key, data=value)
        self.__f[PATCH_GROUP].attrs["n_samples"] = n
        self.__f.close()
        self.__closed = True
        os.replace(self.path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        """閉じるだけ。``.writing`` はそのまま残す (正常に閉じていない印)。"""
        if not self.__closed:
            self.__f.close()
            self.__closed = True

    def discard(self) -> None:
        """何も記録しないまま終わるとき: 閉じて ``.writing`` を消す。"""
        self.abort()
        try:
            os.remove(self.path)
        except OSError:
            pass


# ------------------------------------------------------------------ re-analysis record

#: 記録に後から掛け直した seal test の表と要約の名前 (h5 の隣に書く)。
TEST_PULSE_CSV_SUFFIX = "_test_pulse.csv"
TEST_PULSE_RESULT_SUFFIX = "_test_pulse_result.json"


def test_pulse_rows(metrics: list[PulseMetrics], sampling_rate_hz: float) -> list[dict[str, Any]]:
    """パルス 1 つ 1 行 (時刻つき)。"""
    rows = []
    for m in metrics:
        row = m.as_record()
        row["t_s"] = m.onset_idx / sampling_rate_hz
        rows.append(row)
    return rows


def save_test_pulse_result(out_dir: str | Path, source_file: str,
                           metrics: list[PulseMetrics], sampling_rate_hz: float,
                           params: dict[str, Any], summary: dict[str, Any],
                           phases: dict[str, Any] | None = None) -> Path:
    """``<記録名>_test_pulse.csv`` と ``<記録名>_test_pulse_result.json`` を書く。

    値だけでなく **どのパラメータで掛けたか** も残す (記録に無い条件で出した数字は
    後から確かめられない)。別名に書いてから差し替える。
    """
    import csv

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = Path(source_file).name
    for suffix in (WRITING_SUFFIX, ".h5"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    rows = test_pulse_rows(metrics, sampling_rate_hz)
    columns = ["t_s"] + list(PULSE_COLUMNS)
    csv_path = out_dir / (base + TEST_PULSE_CSV_SUFFIX)
    tmp = csv_path.with_name("_" + csv_path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    tmp.replace(csv_path)
    result = {
        "source_file": source_file, "sampling_rate_hz": float(sampling_rate_hz),
        "params": params, "n_pulses": len(metrics), "summary": summary,
        "phases": phases or {},
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "events_csv": csv_path.name,
    }
    out = out_dir / (base + TEST_PULSE_RESULT_SUFFIX)
    tmp = out.with_name("_" + out.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=4, ensure_ascii=False, default=_plain),
                   encoding="utf-8")
    tmp.replace(out)
    return out


# ------------------------------------------------------------------ reader

@dataclass(frozen=True)
class PatchRecording:
    """patch-clamp mode の記録 1 つ。表は列名 -> 配列の dict。"""

    path: Path
    current_pa: np.ndarray
    sampling_rate_hz: float
    attrs: dict[str, Any]
    sync: dict[str, Any]
    camera: dict[str, np.ndarray]
    test_pulse: dict[str, np.ndarray]
    tip: dict[str, np.ndarray]
    events: list[dict[str, Any]] = field(default_factory=list)
    target_cell: dict[str, float] | None = None

    @staticmethod
    def is_patch_file(path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        try:
            with h5py.File(str(p), "r") as f:
                return PATCH_GROUP in f
        except OSError:
            return False

    @classmethod
    def open(cls, path: str | Path) -> "PatchRecording":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError("No patch-clamp recording at %s." % p)
        with h5py.File(str(p), "r") as f:
            if PATCH_GROUP not in f:
                raise ValueError("%s has no '/%s' group; it is not a patch-clamp mode "
                                 "recording." % (p, PATCH_GROUP))
            g = f[PATCH_GROUP]
            attrs = {k: _plain(v) for k, v in g.attrs.items()}
            sync = {k: _plain(g["sync"][k][()]) if g["sync"][k].shape == () else
                    np.asarray(g["sync"][k]) for k in g["sync"]}
            camera = {k: np.asarray(g["camera"][k]) for k in g["camera"]}
            pulses = {k: np.asarray(g["test_pulse"][k]) for k in g["test_pulse"]}
            tip = {k: np.asarray(g["tip"][k]) for k in g["tip"]}
            events = []
            if "events" in g and g["events"]["label"].shape[0]:
                e = g["events"]
                for i in range(e["label"].shape[0]):
                    events.append({"perf_s": float(e["perf_s"][i]),
                                   "sample": int(e["sample"][i]),
                                   "label": _plain(e["label"][i]),
                                   "note": _plain(e["note"][i])})
            target = None
            if "target_cell" in g:
                target = {k: float(v) for k, v in g["target_cell"].attrs.items()}
        rec = RawRecording.open(p, TRAIN_GROUP)
        return cls(path=p, current_pa=np.asarray(rec.data, dtype=np.float64),
                   sampling_rate_hz=float(attrs.get("sampling_rate_hz",
                                                    rec.sampling_rate_khz * 1000.0)),
                   attrs=attrs, sync=sync, camera=camera, test_pulse=pulses, tip=tip,
                   events=events, target_cell=target)

    # ------------------------------------------------------------------ 便利
    @property
    def n_samples(self) -> int:
        return int(self.current_pa.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.sampling_rate_hz

    @property
    def n_frames(self) -> int:
        return int(self.camera["frame_perf_s"].shape[0]) if "frame_perf_s" in self.camera else 0

    @property
    def video_path(self) -> Path | None:
        name = self.attrs.get("video_file")
        return self.path.with_name(str(name)) if name else None

    def time_s(self) -> np.ndarray:
        return np.arange(self.n_samples, dtype=np.float64) / self.sampling_rate_hz

    def frame_sample_index(self) -> np.ndarray:
        """各フレームに対応する電流の標本番号 (perf_s で揃える)。範囲外は端に寄せる。"""
        if "ai_start_perf_s" not in self.sync or self.n_frames == 0:
            return np.zeros(0, dtype=np.int64)
        rel = self.camera["frame_perf_s"] - float(self.sync["ai_start_perf_s"])
        idx = np.rint(rel * self.sampling_rate_hz).astype(np.int64)
        return np.clip(idx, 0, max(self.n_samples - 1, 0))

    def frame_at_sample(self, sample: int) -> int | None:
        """その標本の時刻にいちばん近いフレームの行番号。フレームが無ければ ``None``。"""
        idx = self.frame_sample_index()
        if idx.shape[0] == 0:
            return None
        return int(np.argmin(np.abs(idx - int(sample))))

    def pulse_time_s(self) -> np.ndarray:
        if "onset_idx" not in self.test_pulse:
            return np.zeros(0)
        return self.test_pulse["onset_idx"] / self.sampling_rate_hz

    def describe(self) -> dict[str, Any]:
        """``--json`` にそのまま出せる要約。"""
        out: dict[str, Any] = {
            "path": str(self.path), "n_samples": self.n_samples,
            "sampling_rate_hz": self.sampling_rate_hz, "duration_s": self.duration_s,
            "n_frames": self.n_frames, "n_pulses": int(self.pulse_time_s().shape[0]),
            "n_tip_estimates": int(np.isfinite(self.tip["x_px"]).sum())
            if "x_px" in self.tip else 0,
            "n_events": len(self.events), "video_file": self.attrs.get("video_file"),
            "video_present": bool(self.video_path and self.video_path.exists()),
            "target_cell": self.target_cell,
        }
        for key in ("created_iso", "camera_id", "test_pulse_mv", "test_pulse_hz",
                    "fps", "width", "height", "stem"):
            if key in self.attrs:
                out[key] = self.attrs[key]
        if self.n_frames:
            t = self.camera["frame_perf_s"]
            if t.shape[0] > 1:
                out["frame_interval_ms_median"] = float(np.median(np.diff(t)) * 1000.0)
        return out


def _plain(value: Any) -> Any:
    """h5 の attr / スカラーを JSON に出せる Python の値にする。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value

"""電位固定のテストパルス (seal test) から、ピペット・膜・アクセスの抵抗と膜容量を出す。

取得側 (slice-controller のカメラアプリの patch-clamp mode) がリアルタイムに掛け、
解析側 (slice-analysis) が同じ記録に後から掛け直す。**両方がここを通る** ので、
画面に出た Rs と後で集計した Rs は同じ式から出る。画面 (PyQt6 / pyqtgraph) は
入れない。

何を見ているか
--------------
MultiClamp Commander の seal test は、指令電位に **周期的な方形のステップ**
(例: 5 mV @ 20 Hz。general の
``docs/protocol_n_manuals/slice/equipment_t1/patch-clamp-YYYYMMDD.md`` "Giga seal" 1.)
を足す。DAQ が読むのは **一次出力の電流だけ** で、指令電位そのものは記録に無い。
だから **ステップの大きさと周波数は Commander の設定値として受け取る**
(:class:`TestPulseParams` の ``delta_v_mv`` / ``frequency_hz``)。

ステップに対する電流は、ピペット抵抗 Rs と膜 (Rm ∥ Cm) の直列回路の応答:

    I(t) = ΔV/Rt + (ΔV/Rs - ΔV/Rt) exp(-t/τ),   Rt = Rs + Rm,   τ = Cm Rs Rm / Rt

ここから出すもの (ラボの手順書の定義に合わせる):

* **R_total = ΔV / ΔI_ss** —— ステップの定常電流から。bath では Rp
  (patch-clamp manual "Giga seal" 11.: ``MΩ = 5 mV / nA``)、seal 後は seal 抵抗、
  whole-cell では入力抵抗 (``zero-mg-enlargement-20160812.md`` "Whole cell" 4. の Ri)
* **Rs = ΔV / ΔI_peak** —— ステップ直後の **電流ピーク** から
  (``zero-mg-enlargement-20160812.md`` "Whole cell" 3.: 「initial current peak … for Rs」)
* **Rm = R_total − Rs**
* **Cm** —— 過渡電流の電荷 Q から。``Cm = Q Rt² / (ΔV Rm²)``
  (上の式を積分: ``Q = ∫(I − I_ss)dt = ΔV Cm Rm²/Rt²``)。
  **ピークではなく電荷で出す** のは、増幅器の低域通過 (一次出力は filtered) で
  ピークは削れても面積は保たれるから
* **τ** —— 過渡の減衰を片対数で直線あてはめ。``rs_extrapolated_mohm`` は
  その直線を立ち上がりの時刻へ外挿したピークで出した Rs (filter で削れたぶんを戻す)。
  手順書の定義 (生のピーク) と並べて記録に残し、**どちらを採るかは実験者が決める**
* **holding_pa** —— ステップの無い半周期の平均電流

**Rs・Rm・Cm・τ は過渡があるときだけ出す。** bath (純抵抗) と seal (GΩ、過渡は
Cp 補償で消える) ではピークが定常と同じで、そこから出る Rs は雑音の最大値を
割った数字にしかならない。``transient_detected`` が偽なら ``None`` で残す。

パルスの位置の決め方
--------------------
slice-analysis の ``utils/test_pulse.py`` は **波形からは探さない** (DAQ が出した指令の
配列が記録にあるから)。ここは逆で、**指令が Commander から出ていて記録に無い** ので
波形から探すしかない。探し方は 1 つに固定する:

1. 設定の周波数で 1 周期に折り畳み (phase fold)、duty の方形テンプレートと循環相関を
   取って位相を決める。折り畳むので **seal 中の数 pA のステップでも位相が取れる**
   (1 チャンクに周期が 5 つあれば、標本 2500 点ぶんの平均になる)
2. 相関の山が雑音に対して十分でなければ、そのチャンクには **パルスが無いとして何も
   返さない** (推測で位置を決めない)
3. 位相から決めた各立ち上がりを、その近傍の微分の最大で 1 標本単位に詰める
   (微分が雑音に埋もれていれば位相の位置をそのまま使う)

**設定の周波数と波形が合っているかは別に検査する。** 立ち上がりが微分で見えるときは
実測の周期 (``measured_period_ms``) を返すので、呼ぶ側は設定値と突き合わせて、
ずれていれば **止めて知らせる** (黙って実測値に寄せない)。

単位は名前に付ける: ``*_pa`` [pA]、``*_mohm`` [MΩ]、``*_pf`` [pF]、``*_ms`` [ms]、
``*_fc`` [fC]。``ΔV`` は mV、電流は pA なので ``R [MΩ] = ΔV [mV] / ΔI [pA] × 1000``。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

#: mV / pA -> MΩ。
MV_PER_PA_TO_MOHM = 1000.0


@dataclass(frozen=True)
class TestPulseParams:
    """seal test の設定 (Commander 側) と、位置決め・測り方のパラメータ。

    (名前が ``Test`` で始まるが pytest のテストではない: ``__test__ = False``。)

    ``delta_v_mv`` と ``frequency_hz`` は **実験者が Commander に設定した値**。
    記録には無いので受け取る。他は測り方の約束で、既定は実装者が決めたもの
    (ラボの文書に由来する値ではない)。結果と一緒に記録する。
    """

    __test__ = False

    #: ステップの大きさ [mV]。符号つき (+ なら指令が上がる半周期がパルス)。
    delta_v_mv: float
    #: seal test の周波数 [Hz]。
    frequency_hz: float
    #: 1 周期のうちパルスが占める割合。MultiClamp の seal test は対称の方形波。
    duty: float = 0.5
    #: 立ち上がりの前後で平均に入れない時間 [ms]。
    guard_ms: float = 0.3
    #: 半周期の末尾のどれだけを「定常」「ベースライン」の平均に使うか。
    window_fraction: float = 0.3
    #: 立ち上がりからピークを探す幅 [ms]。
    peak_window_ms: float = 3.0
    #: 立ち上がりを微分で詰めるときの閾値 (微分の頑健 SD の倍数)。
    edge_sigma: float = 6.0
    #: 位相決めの相関の山を受け入れる最小の SNR。これ未満は「パルス無し」。
    min_step_snr: float = 6.0
    #: 「過渡がある」とみなす、ピークと定常の差の下限 (定常区間の頑健 SD の倍数)。
    #: 過渡が無ければ Rs・Rm・Cm・τ は出さない (bath と seal では定義できない)。
    transient_sigma: float = 5.0
    #: τ のあてはめに使う点の下限 (雑音の頑健 SD の倍数) と最小点数。
    fit_floor_sigma: float = 3.0
    fit_min_points: int = 4

    def as_record(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class PhaseThresholds:
    """位相 (bath / sealed / whole-cell) を名付ける閾値。

    根拠があるのは 2 つだけ:

    * ``seal_min_mohm`` 1000 —— 「1 GΩ corresponds to about −65 pA; in practice
      −70 pA is accepted as sealed」(patch-clamp manual "Giga seal" 17.)
    * ``rs_max_mohm`` 35 —— 「Rs below 35 MΩ」(同 "Whole cell"。20–30 と 20–35 の
      2 通りが書かれているのは Known gaps のとおりで、広いほうを採った)

    残りは **実装者の既定で、ラボの文書に由来しない**。bath と whole-cell の境は
    過渡の有無 (膜容量) で分けるしかなく、その値はまだ誰も決めていない。
    """

    seal_min_mohm: float = 1000.0
    rs_max_mohm: float = 35.0
    #: これ未満の R_total は bath (ピペットが液に入っているだけ)。実装者の既定。
    bath_max_mohm: float = 50.0
    #: whole-cell とみなす膜容量の下限 [pF]。実装者の既定。
    whole_cell_min_cm_pf: float = 10.0

    def as_record(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items()}


PHASE_NONE = "none"          # パルスが無い / 測れない
PHASE_BATH = "bath"
PHASE_APPROACH = "approach"  # bath より高いが seal ではなく、過渡も無い
PHASE_SEALED = "sealed"
PHASE_WHOLE_CELL = "whole_cell"
PHASES = (PHASE_NONE, PHASE_BATH, PHASE_APPROACH, PHASE_SEALED, PHASE_WHOLE_CELL)


@dataclass(frozen=True)
class PulseMetrics:
    """パルス 1 つの測定。添字は **渡した配列の中** の位置 (呼ぶ側が絶対位置へ直す)。"""

    onset_idx: int
    offset_idx: int
    holding_pa: float
    delta_i_ss_pa: float
    delta_i_peak_pa: float
    r_total_mohm: float
    charge_fc: float
    #: ピークが定常より ``transient_sigma`` × 雑音だけ大きいか。これが偽なら
    #: 下の 6 つは ``None`` —— 過渡の無い応答 (bath / seal) に Rs や Cm は無い。
    transient_detected: bool = False
    rs_mohm: float | None = None
    rm_mohm: float | None = None
    cm_pf: float | None = None
    #: 減衰のあてはめから。あてはめられなければ ``None`` (0 で埋めない)。
    tau_ms: float | None = None
    rs_extrapolated_mohm: float | None = None
    cm_from_tau_pf: float | None = None
    #: 立ち上がりが微分で見えたか (位置を 1 標本単位に詰められたか)。
    edge_resolved: bool = False

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PulseWindows:
    """位置決めの結果。``onsets[k]`` から ``offsets[k]`` までがパルス k (半開区間)。"""

    onsets: np.ndarray
    offsets: np.ndarray
    #: 折り畳んだ波形でのステップの SNR。``min_step_snr`` 未満なら空になる。
    step_snr: float
    #: 微分で見えた立ち上がりから測った周期 [ms]。見えなければ ``None``。
    measured_period_ms: float | None
    #: 折り畳みで決めた位相 (周期に対する割合 0..1)。
    phase: float
    edge_resolved: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))

    def __len__(self) -> int:
        return int(self.onsets.shape[0])


# ------------------------------------------------------------------ helpers

def _robust_sd(x: np.ndarray) -> float:
    """1.4826 × MAD。外れ値 (ステップそのもの) に引きずられない雑音の目安。"""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return 0.0
    med = np.median(x)
    return float(1.4826 * np.median(np.abs(x - med)))


def _samples(ms: float, fs_hz: float) -> int:
    return int(round(ms * 1e-3 * fs_hz))


def fold_profile(y: np.ndarray, fs_hz: float, frequency_hz: float,
                 n_bins: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """1 周期に折り畳んだ平均波形と、各 bin の標本数。

    bin の数は既定で 1 周期の標本数 (丸め)。周期が整数標本でなくても、各標本を
    位相で bin に入れるので崩れない。
    """
    period = fs_hz / frequency_hz
    bins = int(round(period)) if n_bins is None else int(n_bins)
    if bins < 4:
        raise ValueError("frequency_hz=%g at %g Hz leaves %d bins per period; "
                         "the pulse cannot be resolved." % (frequency_hz, fs_hz, bins))
    i = np.arange(y.shape[0], dtype=np.float64)
    # 整数周期で k/period*bins が k±ulp になって隣の bin へ落ちないよう、僅かに足す。
    b = np.floor(((i % period) / period) * bins + 1e-9).astype(np.int64) % bins
    counts = np.bincount(b, minlength=bins).astype(np.float64)
    sums = np.bincount(b, weights=np.asarray(y, dtype=np.float64), minlength=bins)
    with np.errstate(invalid="ignore", divide="ignore"):
        profile = np.where(counts > 0, sums / np.maximum(counts, 1.0), np.nan)
    return profile, counts


def _square_template(bins: int, duty: float) -> np.ndarray:
    """平均 0 の方形テンプレート。bin 0 がパルスの先頭。"""
    high = int(round(bins * duty))
    t = -np.ones(bins, dtype=np.float64)
    t[:max(high, 1)] = 1.0
    return t - t.mean()


def locate_pulses(y: np.ndarray, fs_hz: float, params: TestPulseParams) -> PulseWindows:
    """チャンク ``y`` の中のパルスの位置を決める。

    位相は折り畳み + 循環相関で決め、立ち上がりは微分で詰める。相関の山が
    ``min_step_snr`` に届かなければ **空** を返す (パルスが無い、または測れない)。
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim != 1:
        raise ValueError("locate_pulses expects a 1-D trace; got shape %s" % (y.shape,))
    if not params.frequency_hz > 0:
        raise ValueError("frequency_hz must be positive; got %r" % (params.frequency_hz,))
    if params.delta_v_mv == 0:
        raise ValueError("delta_v_mv is 0; no resistance can be derived from a 0 mV step.")
    period = fs_hz / params.frequency_hz
    empty = PulseWindows(np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64),
                         0.0, None, 0.0)
    if y.shape[0] < 2 * period:
        return empty

    profile, counts = fold_profile(y, fs_hz, params.frequency_hz)
    bins = profile.shape[0]
    if np.any(counts == 0):
        return empty
    sign = 1.0 if params.delta_v_mv > 0 else -1.0
    template = _square_template(bins, params.duty) * sign
    centred = profile - profile.mean()
    # 循環相関: corr[k] = Σ_j centred[(j + k) mod bins] template[j]。
    # k が「パルスの先頭が profile のどの bin か」。
    spec = np.fft.rfft(centred) * np.conj(np.fft.rfft(template))
    corr = np.fft.irfft(spec, n=bins)
    k = int(np.argmax(corr))

    # 折り畳んだ波形で、パルス半周期とベースライン半周期の差を雑音と比べる。
    high = int(round(bins * params.duty))
    rolled = np.roll(profile, -k)          # bin 0 がパルスの先頭
    guard = max(_samples(params.guard_ms, fs_hz), 1)
    pulse_part = rolled[guard:max(high - guard, guard + 1)]
    base_part = rolled[high + guard:max(bins - guard, high + guard + 1)]
    if pulse_part.size < 2 or base_part.size < 2:
        return empty
    step = sign * (np.median(pulse_part) - np.median(base_part))
    residual = np.concatenate([pulse_part - np.median(pulse_part),
                               base_part - np.median(base_part)])
    noise = _robust_sd(residual)
    n_per_phase = min(pulse_part.size, base_part.size)
    if noise <= 0:
        snr = float("inf") if step > 0 else 0.0
    else:
        snr = float(step / (noise / np.sqrt(n_per_phase)))
    if not (snr >= params.min_step_snr):
        return PulseWindows(np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64),
                            float(snr), None, k / bins)

    # 位相 -> チャンク内の立ち上がり位置。標本 i の位相は (i / period) mod 1。
    phase0 = k / bins
    first = phase0 * period
    onsets_f = np.arange(first, y.shape[0], period)
    pulse_len = params.duty * period

    d = np.diff(y)
    d_sd = _robust_sd(d)
    onsets = []
    offsets = []
    resolved = []
    for on_f in onsets_f:
        on = int(round(on_f))
        off = int(round(on_f + pulse_len))
        if on < 1 or off > y.shape[0]:
            continue
        on_r, ok = _refine_edge(d, on, guard, params.edge_sigma * d_sd, sign)
        off_r, _ = _refine_edge(d, off, guard, params.edge_sigma * d_sd, -sign)
        if off_r <= on_r + 2 * guard:
            continue
        onsets.append(on_r)
        offsets.append(min(off_r, y.shape[0]))
        resolved.append(ok)
    onsets_a = np.asarray(onsets, dtype=np.int64)
    offsets_a = np.asarray(offsets, dtype=np.int64)
    resolved_a = np.asarray(resolved, dtype=bool)

    measured = None
    if resolved_a.sum() >= 2:
        good = onsets_a[resolved_a]
        gaps = np.diff(good)
        # 隣り合う検出が 1 周期ぶん離れているものだけ (抜けた周期を跨いだ差は使わない)。
        near = gaps[np.abs(gaps - period) < 0.25 * period]
        if near.size:
            measured = float(np.median(near) / fs_hz * 1000.0)
    return PulseWindows(onsets_a, offsets_a, float(snr), measured, phase0, resolved_a)


def _refine_edge(d: np.ndarray, at: int, radius: int, threshold: float,
                 sign: float) -> tuple[int, bool]:
    """``at`` の ±radius で、``sign`` 向きの微分が最大の標本へ詰める。

    閾値に届かなければ位置をそのまま返す (雑音の山に飛びつかない)。返す添字は
    ステップ後の最初の標本。
    """
    lo = max(at - radius - 1, 0)
    hi = min(at + radius, d.shape[0])
    if hi <= lo:
        return at, False
    seg = sign * d[lo:hi]
    j = int(np.argmax(seg))
    if threshold > 0 and seg[j] >= threshold:
        return lo + j + 1, True
    return at, False


# ------------------------------------------------------------------ per pulse

def analyse_pulse(y: np.ndarray, fs_hz: float, onset: int, offset: int,
                  params: TestPulseParams) -> PulseMetrics | None:
    """パルス 1 つ (``y[onset:offset]``) を測る。

    ベースラインは **直前の半周期の末尾**、定常は **このパルスの末尾** の平均
    (``window_fraction``、立ち上がりの前後 ``guard_ms`` を除く)。測れない
    (ベースラインの区間が配列の外、ΔI が 0) なら ``None``。
    """
    y = np.asarray(y, dtype=np.float64)
    length = int(offset - onset)
    guard = max(_samples(params.guard_ms, fs_hz), 1)
    win = max(int(round(length * params.window_fraction)), 2)
    base_lo, base_hi = onset - win, onset - guard
    ss_lo, ss_hi = offset - win, offset - guard
    if base_lo < 0 or base_hi <= base_lo or ss_hi <= ss_lo or offset > y.shape[0]:
        return None
    sign = 1.0 if params.delta_v_mv > 0 else -1.0
    dv = abs(params.delta_v_mv)

    i_base = float(np.mean(y[base_lo:base_hi]))
    i_ss = float(np.mean(y[ss_lo:ss_hi]))
    delta_ss = sign * (i_ss - i_base)
    if not delta_ss > 0:
        # 符号と逆向き、または 0: 抵抗が定義できない。推測で符号を直さない。
        return None

    peak_n = max(_samples(params.peak_window_ms, fs_hz), 1)
    seg = sign * (y[onset:min(onset + peak_n, offset)] - i_base)
    if seg.size == 0:
        return None
    p = int(np.argmax(seg))
    delta_peak = float(seg[p])
    if not delta_peak > 0:
        return None
    delta_peak = max(delta_peak, delta_ss)   # ピークは定常より小さくならない

    r_total = dv / delta_ss * MV_PER_PA_TO_MOHM

    # 電荷: パルス区間の (I − I_ss) を積分 (sign を掛けて正に)。
    dt = 1.0 / fs_hz
    q_pa_s = float(np.sum(sign * (y[onset:offset] - i_ss)) * dt)
    charge_fc = q_pa_s * 1e3                 # pA·s = 1e-12 C = 1e3 fC

    # 過渡があるか: ピークが定常より雑音の transient_sigma 倍以上大きいか。
    noise = _robust_sd(y[ss_lo:ss_hi])
    transient = (delta_peak - delta_ss) > params.transient_sigma * noise
    if not transient:
        return PulseMetrics(
            onset_idx=int(onset), offset_idx=int(offset), holding_pa=i_base,
            delta_i_ss_pa=delta_ss, delta_i_peak_pa=delta_peak, r_total_mohm=r_total,
            charge_fc=charge_fc, transient_detected=False)

    rs = dv / delta_peak * MV_PER_PA_TO_MOHM
    rm = r_total - rs
    cm_pf = _cm_from_charge(charge_fc, dv, r_total, rm)
    tau_ms, rs_ext, cm_tau = _fit_decay(y, fs_hz, onset, offset, onset + p, i_ss,
                                        delta_ss, sign, dv, r_total, rm, params)
    return PulseMetrics(
        onset_idx=int(onset), offset_idx=int(offset), holding_pa=i_base,
        delta_i_ss_pa=delta_ss, delta_i_peak_pa=delta_peak, r_total_mohm=r_total,
        charge_fc=charge_fc, transient_detected=True, rs_mohm=rs, rm_mohm=rm,
        cm_pf=cm_pf, tau_ms=tau_ms, rs_extrapolated_mohm=rs_ext, cm_from_tau_pf=cm_tau)


def _cm_from_charge(charge_fc: float, dv_mv: float, r_total_mohm: float,
                    rm_mohm: float) -> float | None:
    """``Cm = Q Rt² / (ΔV Rm²)``。fC / mV = pF なので単位はそのまま pF。

    Rm が 0 以下 (ピークが定常と同じ) か電荷が負なら定義できないので ``None``。
    """
    if rm_mohm <= 0 or dv_mv <= 0 or charge_fc <= 0:
        return None
    return charge_fc / dv_mv * (r_total_mohm / rm_mohm) ** 2


def _fit_decay(y: np.ndarray, fs_hz: float, onset: int, offset: int, peak_idx: int,
               i_ss: float, delta_ss: float, sign: float, dv: float,
               r_total: float, rm: float, params: TestPulseParams
               ) -> tuple[float | None, float | None, float | None]:
    """ピークの次の標本から、(I − I_ss) が雑音の床へ落ちるまでを片対数であてはめる。"""
    dt = 1.0 / fs_hz
    tail = sign * (y[peak_idx + 1:offset] - i_ss)
    if tail.size < params.fit_min_points:
        return None, None, None
    # 雑音の床: 定常区間の頑健 SD。
    win = max(int(round((offset - onset) * params.window_fraction)), 2)
    floor = params.fit_floor_sigma * _robust_sd(y[offset - win:offset])
    above = tail > max(floor, 0.0)
    # 最初に床を割ったところまで (そのあと雑音で戻っても使わない)。
    stop = int(np.argmin(above)) if not above.all() else tail.size
    if stop < params.fit_min_points:
        return None, None, None
    t = (np.arange(stop, dtype=np.float64) + (peak_idx + 1 - onset)) * dt
    ln = np.log(tail[:stop])
    slope, intercept = np.polyfit(t, ln, 1)
    if not slope < 0:
        return None, None, None
    tau_s = -1.0 / slope
    amp0 = float(np.exp(intercept))            # t = onset での過渡の振幅
    delta_peak_ext = delta_ss + amp0
    rs_ext = dv / delta_peak_ext * MV_PER_PA_TO_MOHM if delta_peak_ext > 0 else None
    rs_fit = rs_ext if rs_ext else None
    cm_tau = None
    if rs_fit and rm > 0:
        # τ = Cm Rs Rm / Rt  ->  Cm = τ Rt / (Rs Rm)。s·MΩ/MΩ² = s/MΩ = µF; ×1e6 で pF。
        rm_fit = r_total - rs_fit
        if rm_fit > 0:
            cm_tau = tau_s * r_total / (rs_fit * rm_fit) * 1e6
    return tau_s * 1e3, rs_fit, cm_tau


# ------------------------------------------------------------------ trace / chunk

def analyse_trace(y: np.ndarray, fs_hz: float, params: TestPulseParams,
                  index_offset: int = 0) -> tuple[list[PulseMetrics], PulseWindows]:
    """1 本の波形の中のパルスを全部測る。``index_offset`` を添字に足して返す。"""
    windows = locate_pulses(y, fs_hz, params)
    out: list[PulseMetrics] = []
    for k in range(len(windows)):
        m = analyse_pulse(y, fs_hz, int(windows.onsets[k]), int(windows.offsets[k]), params)
        if m is None:
            continue
        if index_offset:
            m = PulseMetrics(**dict(m.as_record(), onset_idx=m.onset_idx + index_offset,
                                    offset_idx=m.offset_idx + index_offset,
                                    edge_resolved=bool(windows.edge_resolved[k])))
        else:
            m = PulseMetrics(**dict(m.as_record(), edge_resolved=bool(windows.edge_resolved[k])))
        out.append(m)
    return out, windows


class TestPulseTracker:
    """チャンクを順に渡して、新しく入ったパルスの測定だけを受け取る (リアルタイム用)。

    直前のチャンクの末尾を 1 周期ぶん繋いで掛けるので、境界を跨いだパルスも測れる。
    返すのは **立ち上がりが新しいチャンクの中にある** パルスだけ (二重に数えない)。
    添字は記録の先頭からの絶対位置。
    """

    __test__ = False

    def __init__(self, fs_hz: float, params: TestPulseParams) -> None:
        self.fs_hz = float(fs_hz)
        self.params = params
        self.__carry = np.zeros(0, dtype=np.float64)
        self.__carry_start = 0   # carry[0] の絶対位置
        self.__next_abs = 0      # 次のチャンクの先頭の絶対位置
        self.__period = self.fs_hz / params.frequency_hz
        self.last_windows: PulseWindows | None = None

    @property
    def samples_seen(self) -> int:
        return self.__next_abs

    def feed(self, chunk: np.ndarray) -> list[PulseMetrics]:
        chunk = np.asarray(chunk, dtype=np.float64).ravel()
        start_abs = self.__next_abs
        self.__next_abs += chunk.shape[0]
        y = np.concatenate([self.__carry, chunk])
        y_start = self.__carry_start
        metrics, windows = analyse_trace(y, self.fs_hz, self.params, index_offset=y_start)
        self.last_windows = windows
        fresh = [m for m in metrics if m.onset_idx >= start_abs]
        # 次回のために末尾 1.5 周期ぶんを持ち越す (パルス + その前のベースライン)。
        keep = int(np.ceil(1.5 * self.__period))
        if y.shape[0] > keep:
            self.__carry = y[-keep:]
            self.__carry_start = y_start + y.shape[0] - keep
        else:
            self.__carry = y
            self.__carry_start = y_start
        return fresh


# ------------------------------------------------------------------ summary / phase

SUMMARY_KEYS = ("holding_pa", "delta_i_ss_pa", "delta_i_peak_pa", "r_total_mohm",
                "rs_mohm", "rm_mohm", "cm_pf", "tau_ms", "rs_extrapolated_mohm",
                "cm_from_tau_pf", "charge_fc")


def summarize(metrics: list[PulseMetrics]) -> dict[str, Any]:
    """パルス群の中央値。無いものは ``None`` (0 で埋めない)。"""
    out: dict[str, Any] = {"n_pulses": len(metrics)}
    for key in SUMMARY_KEYS:
        values = [getattr(m, key) for m in metrics if getattr(m, key) is not None]
        out[key] = float(np.median(values)) if values else None
    return out


def classify_phase(summary: dict[str, Any],
                   thresholds: PhaseThresholds | None = None) -> str:
    """要約から位相の名前を付ける。閾値は :class:`PhaseThresholds` (根拠はそこに)。"""
    th = thresholds or PhaseThresholds()
    rt = summary.get("r_total_mohm")
    cm = summary.get("cm_pf")
    if rt is None or not summary.get("n_pulses"):
        return PHASE_NONE
    if rt >= th.seal_min_mohm:
        return PHASE_SEALED
    if cm is not None and cm >= th.whole_cell_min_cm_pf and rt > th.bath_max_mohm:
        return PHASE_WHOLE_CELL
    if rt < th.bath_max_mohm:
        return PHASE_BATH
    return PHASE_APPROACH


def simulate_response(fs_hz: float, duration_s: float, params: TestPulseParams,
                      rs_mohm: float, rm_mohm: float, cm_pf: float,
                      holding_pa: float = 0.0, noise_pa: float = 0.0,
                      phase: float = 0.0, filter_tau_ms: float = 0.0,
                      rng: np.random.Generator | None = None) -> np.ndarray:
    """Rs–(Rm ∥ Cm) 回路の seal test 応答を合成する (テストとエミュレータ用)。

    ``filter_tau_ms`` > 0 なら一次の低域通過を通す (増幅器の出力フィルタの代わり)。
    ``cm_pf`` が 0 なら純抵抗 (bath / seal)。
    """
    n = int(round(duration_s * fs_hz))
    dt = 1.0 / fs_hz
    period = fs_hz / params.frequency_hz
    i = np.arange(n, dtype=np.float64)
    in_pulse = ((i / period + (1.0 - phase)) % 1.0) < params.duty
    dv = params.delta_v_mv                     # mV, signed
    rt = rs_mohm + rm_mohm
    i_ss = dv / rt * MV_PER_PA_TO_MOHM          # pA
    y = holding_pa + np.where(in_pulse, i_ss, 0.0)
    if cm_pf > 0 and rm_mohm > 0:
        tau_s = cm_pf * 1e-12 * (rs_mohm * 1e6) * (rm_mohm * 1e6) / (rt * 1e6)
        amp = (dv / rs_mohm - dv / rt) * MV_PER_PA_TO_MOHM
        edges = np.flatnonzero(np.diff(in_pulse.astype(np.int8)))
        kernel_n = int(min(max(round(10 * tau_s * fs_hz), 2), n))
        kernel = amp * np.exp(-np.arange(kernel_n) * dt / tau_s)
        for e in edges:
            s = 1.0 if in_pulse[e + 1] else -1.0
            stop = min(e + 1 + kernel_n, n)
            y[e + 1:stop] += s * kernel[: stop - e - 1]
    if filter_tau_ms > 0:
        from scipy.signal import lfilter
        a = dt / (filter_tau_ms * 1e-3 + dt)
        # 一次 IIR: out[j] = out[j-1] + a (y[j] - out[j-1])。初期値は y[0]。
        y = lfilter([a], [1.0, -(1.0 - a)], y, zi=[(1.0 - a) * y[0]])[0]
    if noise_pa > 0:
        rng = rng or np.random.default_rng(0)
        y = y + rng.normal(0.0, noise_pa, n)
    return y

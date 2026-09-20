"""Keyence のモザイク (``XY##`` 1 組) を 1 枚の ``TCZYX`` に並べる。

``keyenceutils.stich.StichedImage`` の移植。**あちらのリポジトリ
(ylabjp/keyence-microscopy) は archived** で push できないので、手を入れられる
場所へ持ってくる。histology のタイリングはここを使う。

配置の規則は**そのまま写してある**——変えると既存の出力と画素が合わなくなる:

* 各タイルの位置は Keyence の XML の ``XyStageRegion`` (nm) を画素に直したもの。
  最小値を原点にして相対座標にする
* **タイルは 1 枚ずつ 180 度回す** (``flipud`` + ``fliplr``)。ステージの座標系が
  画像の向きと反転しているため。**根拠は移植元の実装だけ**で、Keyence の仕様を
  確認したわけではない (`未確認`)。外すと全画像が反転するので触らない
* 重なりは**後から置いたタイルが勝つ**。混ぜない・合わせ直さない —— 継ぎ目の
  補正は別の課題で、ここでやると「いつ直したか」が出力から追えなくなる

移植元から直したもの。**画素は 1 つも動かない** —— 直したのは無駄と、
メタデータの欠落・取り違えである:

* **1 タイルを 2 回読んでいた。** 移植元は同じ DataFrame を 2 回 append していて
  (`stich.py:101` と `:113`)、``pd.concat`` が全行を重複させ、読み込みと貼り付けが
  倍になっていた。貼る内容は同じなので絵は変わらないが、ネットワーク越しでは
  読み込みが実行時間のほとんどを占める
* **ゲインが 1 タイル目の値しか残らなかった。** 露光はチャネルごとに畳んでいたのに
  ゲインは ``.values[0]`` だった (`stich.py:194-195`)。チャネルごとに揃える
  (:mod:`ylabcommon.bioio.core.ome_acquisition` が OME-XML へ運ぶ)
* **Z 間隔の欄に XY の画素密度が入っていた。** 移植元は Z 間隔を正しく計算して
  (`stich.py:187`) ``Properties["z_interval(um)"]`` という**文字列**に入れる一方、
  ImageJ の ``spacing`` には ``res = 1.0 / umPerPixel`` を書いていた
  (`stich.py:184` と `:203`)。``spacing`` は読む側が **Z のボクセル長**として
  読む欄である (実測: ``spacing=3.0`` で書いた ImageJ TIFF を bioio は
  ``PhysicalPixelSizes(Z=3.0, Y=7.5, X=7.5)`` と読む)。つまり**これまでに
  マージした出力の Z 間隔は、撮った値ではない** —— test/data の 4x では
  ``1/7.5488 = 0.1325`` µm が入る。ここでは測った間隔だけを ``z_interval_um`` に
  入れ、測れていなければ ``None`` のままにする
* **Z のサイズを範囲で決めていた** (`stich.py:107`: ``max - min + 1``)。Z が 1 から
  始まらない取得では添字が画布の外へ出る。ここでは**最大の添字 + 1**。1 始まりの
  取得では同じ値になるので、既存の出力は変わらない

まだ直していないこと: **画布を丸ごと RAM に持つ**。1 枚の切片の大きさが
メモリで決まる。面ごとに流す形にできるが、重なりの扱い (上の 3 点目) を決めて
からのほうがよい。
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import tifffile

from ylabcommon.bioio.core.mosaic_seam import (
    OVERLAP_POLICIES,
    PairShift,
    Registration,
    blend_into,
    edge_ratio,
    measure_pair,
    solve_offsets,
)
from ylabcommon.bioio.core.ome_acquisition import AcquisitionConditions
from ylabcommon.bioio.keyence.keyence_metadata_extractor import channel_of
from ylabcommon.bioio.keyence.keyence_metainfo import ImageMetadata

#: 並べる対象。``Overlay`` を含む名前は Keyence が作る合成画像なので外す。
TILE_GLOB = "Image_*.tif"
OVERLAY_MARKER = "Overlay"

#: 重なりがこれより狭いと、位相相関の当てにならない。画素。
_MIN_BAND = 8

#: ``..._Z00012_CH1.tif`` の Z。**1 始まり**なので添字にするとき 1 引く。
_Z_PATTERN = re.compile(r"_Z(\d+)_")


@dataclass
class Mosaic:
    """並べ終わったモザイクと、それを作ったときの条件。"""

    #: ``TCZYX`` の uint16。Keyence のモザイクに時間軸は無いので ``T`` は 1。
    data: np.ndarray
    #: 画素の大きさ [µm]。全タイルで同じであることを確かめてある。
    #: **既定値を置かない** —— 0.0 を書くと、測った大きさと見分けがつかなくなる。
    pixel_size_um: float
    #: ``C`` の順に並んだチャネル名 (``["CH1", "CH3"]``)。
    channel_names: list[str] = field(default_factory=list)
    #: OME-XML へ書くための撮像条件。
    conditions: AcquisitionConditions = field(default_factory=AcquisitionConditions)
    #: Z 間隔 [µm]。Z 位置が 1 つしか無ければ None。
    z_interval_um: float | None = None
    #: 重なりから測った置き直し。``register=False`` なら None。
    registration: Registration | None = None
    #: 重なりの扱い (``last`` / ``mean``)。
    overlap_policy: str = "last"

    @property
    def physical_pixel_sizes(self) -> tuple[float | None, float, float]:
        """``BioIOWriter.write`` に渡す ``(Z, Y, X)`` [µm]。

        **Z が測れていなければ ``None`` のまま渡す。**
        :class:`~ylabcommon.bioio.keyence.keyence_metadata_extractor.KeyenceMetadataExtractor`
        は Z 位置が 1 つのとき XY の画素サイズを Z に入れているが
        (``keyence_metadata_extractor.py`` の ``z_step = first["umPerPixel"]``)、
        それは測った間隔と区別がつかない値になる。``BioIOWriter`` は ``Z=None`` を
        受けて OME の ``PhysicalSizeZ`` を**空のまま**書くので
        (tests/test_keyence_mosaic.py の
        ``test_an_unknown_z_size_reaches_the_file_as_an_empty_field``)、埋めずに済む。
        """
        return (self.z_interval_um, self.pixel_size_um, self.pixel_size_um)


def list_tiles(folder: str | os.PathLike) -> list[str]:
    """``folder`` の中の、並べる対象のファイル名 (パスではなく名前)。"""
    found = sorted(glob.glob(os.path.join(os.fspath(folder), TILE_GLOB)))
    return [os.path.basename(f) for f in found if OVERLAY_MARKER not in os.path.basename(f)]


def _z_index(name: str) -> int:
    """ファイル名の Z を 0 始まりの添字に。Z が無ければ 0。"""
    m = _Z_PATTERN.search(name)
    return int(m.group(1)) - 1 if m else 0


def _channel_order(names: list[str]) -> list[str]:
    """``CH`` の**番号順**。

    移植元は文字列として並べていたので、``CH10`` が ``CH2`` より前に来る。
    いまの取得は 4 チャネルまでなので出力は変わらないが、番号で並べておく。
    """
    labels = {channel_of(n) for n in names}
    return sorted((c for c in labels if c), key=lambda c: int(c[2:]))


def stitch_mosaic(
    folder: str | os.PathLike,
    *,
    register: bool = True,
    overlap: str = "last",
) -> Mosaic:
    """``XY##`` 1 組を並べて 1 枚にする。

    Args:
        register: 重なり帯からタイルの置き直し量を推定して当てる。**既定で入れる**
            —— ステージ座標は画像の中身とぴったりではなく、ずれは取得ごとに
            測れる (:mod:`ylabcommon.bioio.core.mosaic_seam` に実測)。推定が
            通らなかったペアは**当てない**ので、テクスチャの無い重なりで
            でたらめに動くことはない。``False`` にすると移植元と同じ、
            ステージ座標そのままの配置になる。
        overlap: ``"last"`` (既定、後に置いたタイルが勝つ) か ``"mean"``。
            **``mean`` は継ぎ目を消さない。** 実測では最大段差が典型の 2.35 倍
            から 2.54 倍になった —— 視野内に左右の傾きがあるので、平均しても
            段差が重なりの端へ移るだけである。そのうえ重なりぶん (ここでは
            30% の面積) の画素の値が変わる。平均が情報を増やすのは視野内の
            傾きを取り除いたあと、つまりキャリブレーションのあとである
            (:mod:`ylabcommon.bioio.core.mosaic_seam` に実測)。

    Raises:
        ValueError: タイルが無い、16 bit でない、画素の大きさやレンズがタイル間で
            食い違う、``overlap`` が知らない値、など。**食い違いは黙って片方を
            採らない** —— どちらが正しいかはファイルからは決められない。
    """
    if overlap not in OVERLAP_POLICIES:
        raise ValueError("unknown overlap policy %r; expected one of %s"
                         % (overlap, ", ".join(OVERLAP_POLICIES)))

    folder = os.fspath(folder)
    names = list_tiles(folder)
    if not names:
        raise ValueError("No %s tiles in %s" % (TILE_GLOB, folder))

    channels = _channel_order(names)
    if not channels:
        raise ValueError("No CH.. label on any tile in %s" % folder)
    channel_index = {name: i for i, name in enumerate(channels)}

    # --- 1 タイル 1 回だけ読む (移植元はここで重複していた) ---
    tiles: list[dict[str, Any]] = []
    for name in names:
        channel = channel_of(name)
        if channel is None:
            continue
        meta = ImageMetadata(os.path.join(folder, name)).get_dict()
        meta["name"] = name
        meta["channel"] = channel
        meta["c_index"] = channel_index[channel]
        meta["z_index"] = _z_index(name)
        tiles.append(meta)

    _check_uniform(tiles, "umPerPixel", folder)
    _check_uniform(tiles, "LensName", folder)

    x0 = min(t["X"] for t in tiles)
    y0 = min(t["Y"] for t in tiles)
    for t in tiles:
        t["x_rel"] = t["X"] - x0
        t["y_rel"] = t["Y"] - y0
        t["position"] = _position_key(t)

    registration = _register(folder, tiles) if register else None
    if registration is not None:
        for t in tiles:
            dy, dx = registration.offsets.get(t["position"], (0, 0))
            t["x_rel"] += dx
            t["y_rel"] += dy
        # 置き直しで負にふれることがある。原点を取り直す —— 全体の平行移動は
        # 継ぎ目を変えない。
        x_shift = min(t["x_rel"] for t in tiles)
        y_shift = min(t["y_rel"] for t in tiles)
        for t in tiles:
            t["x_rel"] -= x_shift
            t["y_rel"] -= y_shift

    width = max(t["x_rel"] + t["W"] for t in tiles)
    height = max(t["y_rel"] + t["H"] for t in tiles)
    z_size = max(t["z_index"] for t in tiles) + 1

    canvas = np.zeros((1, len(channels), z_size, height, width), np.uint16)
    counts = (np.zeros(canvas.shape, np.uint16) if overlap == "mean" else None)
    for t in tiles:
        image = _read_tile(folder, t["name"])
        selection = (
            0,
            t["c_index"],
            t["z_index"],
            slice(t["y_rel"], t["y_rel"] + t["H"]),
            slice(t["x_rel"], t["x_rel"] + t["W"]),
        )
        blend_into(canvas, counts, image, selection, overlap)

    return Mosaic(
        data=canvas,
        channel_names=channels,
        conditions=_conditions(tiles, channels, registration, overlap),
        pixel_size_um=tiles[0]["umPerPixel"],
        z_interval_um=_z_interval_um(tiles),
        registration=registration,
        overlap_policy=overlap,
    )


def _position_key(tile: dict) -> str:
    """ステージ位置の名前。**チャネルにも Z にもよらない** —— ずれはステージと
    光学の性質なので、同じ位置のタイルは同じだけ動かす。"""
    return "x%d_y%d" % (tile["x_rel"], tile["y_rel"])


def _read_tile(folder: str, name: str) -> np.ndarray:
    """1 枚読んで、移植元と同じ向きに直す。"""
    image = tifffile.imread(os.path.join(folder, name))
    if image.ndim > 2:
        # 取得設定によっては 1 枚に複数成分が入る。移植元と同じく先頭だけ使う。
        image = image[:, :, 0]
    if image.dtype != np.uint16:
        raise ValueError(
            "%s is %s, not uint16. The canvas is uint16 and a narrower or "
            "wider type would change the pixel values." % (name, image.dtype))
    # **180 度回す。** module docstring を参照 (移植元の挙動)。
    return np.flipud(np.fliplr(image))


def _overlap_bands(a: dict, b: dict) -> tuple[tuple, tuple] | None:
    """2 つのタイルが重なっている部分を、それぞれのタイル内の slice で返す。"""
    x_from = max(a["x_rel"], b["x_rel"])
    x_to = min(a["x_rel"] + a["W"], b["x_rel"] + b["W"])
    y_from = max(a["y_rel"], b["y_rel"])
    y_to = min(a["y_rel"] + a["H"], b["y_rel"] + b["H"])
    if x_to - x_from < _MIN_BAND or y_to - y_from < _MIN_BAND:
        return None
    return (
        (slice(y_from - a["y_rel"], y_to - a["y_rel"]),
         slice(x_from - a["x_rel"], x_to - a["x_rel"])),
        (slice(y_from - b["y_rel"], y_to - b["y_rel"]),
         slice(x_from - b["x_rel"], x_to - b["x_rel"])),
    )


def _register(folder: str, tiles: list[dict]) -> Registration:
    """重なり帯からタイルの置き直し量を測る。

    読むのは **Z 1 面ぶん、全チャネル**である。ずれはステージと光学の性質なので
    Z ごとには変わらず、チャネルをまたいで測ると中央値で外れ値を落とせる
    (実測では CH1 と CH3 が同じ ``dx=+5`` を出す)。C=2, Z=40 の取得なら
    読み込みは 80 枚中 2 枚ぶん増えるだけで済む。
    """
    by_position: dict[str, list[dict]] = {}
    for t in tiles:
        by_position.setdefault(t["position"], []).append(t)
    positions = sorted(by_position, key=lambda k: (by_position[k][0]["y_rel"],
                                                   by_position[k][0]["x_rel"]))
    if len(positions) < 2:
        return Registration(offsets={p: (0, 0) for p in positions}, pairs=())

    plane_cache: dict[str, np.ndarray] = {}

    def plane(tile: dict) -> np.ndarray:
        if tile["name"] not in plane_cache:
            plane_cache[tile["name"]] = _read_tile(folder, tile["name"])
        return plane_cache[tile["name"]]

    def representatives(key: str) -> dict[str, dict]:
        """位置 ``key`` の、チャネルごとの代表タイル (いちばん小さい Z)。"""
        chosen: dict[str, dict] = {}
        for tile in sorted(by_position[key], key=lambda t: t["z_index"]):
            chosen.setdefault(tile["channel"], tile)
        return chosen

    ratios: dict[str, float] = {}
    pairs: list[PairShift] = []
    for i, a_key in enumerate(positions):
        for b_key in positions[i + 1:]:
            a_reps = representatives(a_key)
            b_reps = representatives(b_key)
            measured: list[PairShift] = []
            for channel in sorted(set(a_reps) & set(b_reps)):
                a_tile, b_tile = a_reps[channel], b_reps[channel]
                bands = _overlap_bands(a_tile, b_tile)
                if bands is None:
                    continue
                a_band, b_band = bands
                measured.append(measure_pair(
                    a_key, b_key, plane(a_tile)[a_band], plane(b_tile)[b_band]))
            if measured:
                pairs.append(_combine_channels(a_key, b_key, measured))

    # 視野内の傾きの実測。**補正はしない** —— 模型が決まっていないので
    # (mosaic_seam の docstring)、次の段のために記録だけ残す。
    # **読み直さない。** ペアの推定で既に読んだ面だけから測る。記録のための
    # おまけに読み込みを増やすと、ネットワーク越しでは実行時間が倍になる。
    band = max(1, min(t["W"] for t in tiles) // 4)
    for key in positions:
        seen = [edge_ratio(plane_cache[t["name"]], band)
                for t in representatives(key).values()
                if t["name"] in plane_cache]
        usable = [r for r in seen if r is not None]
        if usable:
            ratios[key] = float(np.median(usable))

    plane_cache.clear()
    return Registration(offsets=solve_offsets(positions, pairs),
                        pairs=tuple(pairs), edge_ratios=ratios)


def _combine_channels(a: str, b: str, measured: list[PairShift]) -> PairShift:
    """同じペアをチャネルごとに測った結果を 1 つにする。

    採れたものの**中央値**を採る。1 チャネルが外れても残りで決まるようにする
    ため。どれも採れなければ、いちばん当てになりそうな 1 つを理由ごと返す。
    """
    accepted = [p for p in measured if p.accepted]
    if not accepted:
        return max(measured, key=lambda p: p.correlation_after)
    return PairShift(
        a=a, b=b,
        dy=int(np.median([p.dy for p in accepted])),
        dx=int(np.median([p.dx for p in accepted])),
        peak=float(np.median([p.peak for p in accepted])),
        correlation_before=float(np.median([p.correlation_before for p in accepted])),
        correlation_after=float(np.median([p.correlation_after for p in accepted])),
        intensity_ratio=float(np.median([p.intensity_ratio for p in accepted
                                         if p.intensity_ratio is not None]))
        if any(p.intensity_ratio is not None for p in accepted) else None,
        accepted=True,
    )


def _check_uniform(tiles: list[dict], key: str, folder: str) -> None:
    """全タイルで揃っているべき値。割れていたら**選ばずに落とす**。"""
    values = {t[key] for t in tiles}
    if len(values) > 1:
        raise ValueError(
            "%s differs between tiles in %s: %s. One mosaic cannot be laid out "
            "from tiles taken at different settings, and which of them is the "
            "intended one is not in the files."
            % (key, folder, ", ".join(sorted(map(str, values)))))


def _z_interval_um(tiles: list[dict]) -> float | None:
    """Z 面の間隔 [µm]。Z が 1 枚なら None (``0`` ではない —— 無いので)。"""
    positions = sorted({t["Z_position"] for t in tiles})
    if len(positions) < 2:
        return None
    return float(np.median(np.diff(positions)) / 1000)  # nm -> µm


def _conditions(tiles: list[dict], channels: list[str],
                registration: Registration | None = None,
                overlap: str = "last") -> AcquisitionConditions:
    """チャネルごとの露光とゲイン。**割れている項目は埋めない。**

    継ぎ目に何をしたかも一緒に入れる。出力を見ただけで、どう並べたものか
    分かるようにするため —— 置き直しは画素の位置を変えるので、記録が無いと
    別々の日に作った出力を突き合わせられない。
    """
    exposure: dict[str, Any] = {}
    gains: dict[str, dict[str, Any]] = {}
    for channel in channels:
        same = [t for t in tiles if t["channel"] == channel]
        exposure[channel] = _one(same, "ExposureTimeInS")
        gains[channel] = {
            "CameraGain": _one(same, "CameraGain"),
            "CameraHardwareGain": _one(same, "CameraHardwareGain"),
        }
    sectioning = _one(tiles, "Sectioning")
    extra: dict[str, Any] = {"Seam/Overlap": overlap}
    if sectioning is not None:
        extra["Sectioning"] = sectioning
    if registration is not None:
        extra.update(registration.as_metadata())
    return AcquisitionConditions(
        channel_names=list(channels),
        exposure_by_channel=exposure,
        gain_by_channel=gains,
        lens=tiles[0]["LensName"],
        extra=extra,
    )


def _one(tiles: list[dict], key: str) -> Any:
    """同じ値なら返す。割れていたら ``None``。

    どのタイルが正しいかはファイルからは分からない。選べば測った値と区別の
    つかない値になるので、**無いことにする**。
    """
    values = {t[key] for t in tiles if t.get(key) is not None}
    return next(iter(values)) if len(values) == 1 else None

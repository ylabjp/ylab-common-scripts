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

from ylabcommon.bioio.core.ome_acquisition import AcquisitionConditions
from ylabcommon.bioio.keyence.keyence_metadata_extractor import channel_of
from ylabcommon.bioio.keyence.keyence_metainfo import ImageMetadata

#: 並べる対象。``Overlay`` を含む名前は Keyence が作る合成画像なので外す。
TILE_GLOB = "Image_*.tif"
OVERLAY_MARKER = "Overlay"

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


def stitch_mosaic(folder: str | os.PathLike) -> Mosaic:
    """``XY##`` 1 組を並べて 1 枚にする。

    Raises:
        ValueError: タイルが無い、16 bit でない、画素の大きさやレンズがタイル間で
            食い違う、など。**食い違いは黙って片方を採らない** —— どちらが正しいか
            はファイルからは決められない。
    """
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

    width = max(t["x_rel"] for t in tiles) + max(t["W"] for t in tiles)
    height = max(t["y_rel"] for t in tiles) + max(t["H"] for t in tiles)
    z_size = max(t["z_index"] for t in tiles) + 1

    canvas = np.zeros((1, len(channels), z_size, height, width), np.uint16)
    for t in tiles:
        image = tifffile.imread(os.path.join(folder, t["name"]))
        if image.ndim > 2:
            # 取得設定によっては 1 枚に複数成分が入る。移植元と同じく先頭だけ使う。
            image = image[:, :, 0]
        if image.dtype != np.uint16:
            raise ValueError(
                "%s is %s, not uint16. The canvas is uint16 and a narrower or "
                "wider type would change the pixel values." % (t["name"], image.dtype))
        # **180 度回す。** 上の module docstring を参照 (移植元の挙動)。
        image = np.flipud(np.fliplr(image))
        canvas[
            0,
            t["c_index"],
            t["z_index"],
            t["y_rel"]:t["y_rel"] + t["H"],
            t["x_rel"]:t["x_rel"] + t["W"],
        ] = image

    return Mosaic(
        data=canvas,
        channel_names=channels,
        conditions=_conditions(tiles, channels),
        pixel_size_um=tiles[0]["umPerPixel"],
        z_interval_um=_z_interval_um(tiles),
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


def _conditions(tiles: list[dict], channels: list[str]) -> AcquisitionConditions:
    """チャネルごとの露光とゲイン。**割れている項目は埋めない。**"""
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
    return AcquisitionConditions(
        channel_names=list(channels),
        exposure_by_channel=exposure,
        gain_by_channel=gains,
        lens=tiles[0]["LensName"],
        extra={"Sectioning": sectioning} if sectioning is not None else {},
    )


def _one(tiles: list[dict], key: str) -> Any:
    """同じ値なら返す。割れていたら ``None``。

    どのタイルが正しいかはファイルからは分からない。選べば測った値と区別の
    つかない値になるので、**無いことにする**。
    """
    values = {t[key] for t in tiles if t.get(key) is not None}
    return next(iter(values)) if len(values) == 1 else None

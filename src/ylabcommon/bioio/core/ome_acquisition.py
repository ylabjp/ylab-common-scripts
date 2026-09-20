"""撮像条件 (露光時間・検出器ゲイン・対物レンズ) を OME-XML に書く。

なぜ「書いたあとで足す」のか
---------------------------
bioio は OME-XML の書き込みに対応している —— ``OmeTiffWriter.save()`` は
``ome_xml`` 引数で ``ome_types.OME`` をそのまま受け取る。だが **自分で組んだ OME
を渡すと次元のラベルが壊れる**。実測 (bioio 3.5.1 / bioio-ome-tiff 1.4.0):

    書き方                                 読み戻した series[0].axes
    ------------------------------------   ------------------------
    OmeTiffWriter.save(...)  (bioio 任せ)   CYX     <- 正しい
    OmeTiffWriter.save(..., ome_xml=自作)   IYX     <- C が消える
    TiffWriter(ome=True) + metadata dict    CYX     <- 正しい
    TiffWriter(ome=False) + description     QQQYX   <- 全部 Q

``axes`` を見て分岐している下流 (histology の plot_image_n_histogram は
``ZCYX`` / ``CYX`` / ``ZYX`` で分けている) は、``IYX`` や ``QQQYX`` を受け取ると
「未対応の軸」で落ちる。**画素を書く経路は触らない**のが安全なので、ここでは
書き終わったファイルの OME-XML を読み直し、条件を足して書き戻す。
``tifffile.tiffcomment`` がそのための API で、通常の書き出しと流し書きの
どちらにも同じ実装が効く (両方で実測済み)。

OME のどこに入れるか
--------------------
* ``Plane.ExposureTime`` (単位 s) —— 露光時間。``ExposureTimeInS`` は
  Numerator/Denominator の秒なので、意味がそのまま一致する
* ``Channel.DetectorSettings.Gain`` —— ``CameraGain``。「この撮像で
  カメラに与えたゲイン」で、DetectorSettings.Gain の定義と名前が一致する
* ``Objective.Model`` —— ``LensName`` を文字列のまま。倍率や NA へ**分解しない**
  (``PlanApo 4x 0.20/20.00mm`` を数値に割るのは解釈であって、記録ではない)
* ``MapAnnotation`` —— 上に当てはまらないもの全部を **元のキー名のまま**。
  ``CameraHardwareGain`` がここに入る: OME のどれに当たるか
  (``Detector.AmplificationGain`` か否か) は Keyence の仕様を確認していない。
  当てずっぽうで型付きの枠に入れると、確かめた値と見分けがつかなくなる。
  **ゲインは 2 つとも MapAnnotation にも入れる**ので、型付き側の解釈が後で
  変わっても元の値は失われない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import tifffile
from ome_types import from_xml, to_xml
from ome_types.model import (
    Detector,
    DetectorSettings,
    Instrument,
    Map,
    MapAnnotation,
    Objective,
    Plane,
    StructuredAnnotations,
)
from ome_types.model.simple_types import UnitsTime

#: MapAnnotation の namespace。読む側がこの注釈を見分けるための印。
ACQUISITION_NAMESPACE = "ylab.acquisition.conditions"

_DETECTOR_ID = "Detector:0"
_OBJECTIVE_ID = "Objective:0"
_INSTRUMENT_ID = "Instrument:0"


@dataclass
class AcquisitionConditions:
    """1 つの出力ファイルに付ける撮像条件。

    チャネル名は **書き出した C の順** に並んだ列として渡す (``channel_names``)。
    値の辞書はそのチャネル名で引く。名前が揃っていないと、どのチャネルの条件か
    決められないので、そのチャネルは**黙って既定値で埋めずに飛ばす**。
    """

    #: C 軸の順に並んだチャネル名。例 ``["CH1", "CH3"]``
    channel_names: list[str] = field(default_factory=list)
    #: チャネル名 -> 露光時間 [s]。None の項目は書かない (割れていた、の意)
    exposure_by_channel: Mapping[str, Optional[float]] = field(default_factory=dict)
    #: チャネル名 -> {"CameraGain": ..., "CameraHardwareGain": ...}
    gain_by_channel: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: 対物レンズ名。文字列のまま Objective.Model へ
    lens: Optional[str] = None
    #: 型付きの枠に入れないものを元のキー名のまま (Sectioning など)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (
            self.exposure_by_channel or self.gain_by_channel or self.lens or self.extra
        )


def _map_entries(conditions: AcquisitionConditions) -> list[tuple[str, str]]:
    """MapAnnotation に入れる (key, value)。**ゲインは型付き側にも入れるが、
    ここにも必ず残す** —— 型付き側の解釈が後で変わっても元の値が消えないように。"""
    entries: list[tuple[str, str]] = []
    for channel in conditions.channel_names:
        for key, value in sorted(conditions.gain_by_channel.get(channel, {}).items()):
            if value is not None:
                entries.append(("%s/%s" % (channel, key), str(value)))
        exposure = conditions.exposure_by_channel.get(channel)
        if exposure is not None:
            entries.append(("%s/ExposureTimeInS" % channel, repr(float(exposure))))
    for key, value in sorted(conditions.extra.items()):
        if value is not None:
            entries.append((str(key), str(value)))
    return entries


def build_enriched_ome(ome: Any, conditions: AcquisitionConditions) -> Any:
    """既にある OME に条件を足して返す。**次元の記述には触らない。**"""
    image = ome.images[0]
    pixels = image.pixels

    detector = Detector(id=_DETECTOR_ID)
    objective = Objective(id=_OBJECTIVE_ID, model=conditions.lens) if conditions.lens else None
    ome.instruments = [
        Instrument(
            id=_INSTRUMENT_ID,
            detectors=[detector],
            objectives=[objective] if objective else [],
        )
    ]
    image.instrument_ref = {"id": _INSTRUMENT_ID}

    # --- ゲイン: チャネルごとに DetectorSettings へ -------------------------
    for index, channel in enumerate(pixels.channels):
        name = _channel_name(conditions, index, channel)
        gains = conditions.gain_by_channel.get(name) if name else None
        camera_gain = gains.get("CameraGain") if gains else None
        if camera_gain is not None:
            channel.detector_settings = DetectorSettings(
                id=_DETECTOR_ID, gain=float(camera_gain)
            )

    # --- 露光: Plane ごとに ------------------------------------------------
    planes = []
    for t in range(pixels.size_t):
        for c in range(pixels.size_c):
            for z in range(pixels.size_z):
                name = _channel_name(conditions, c, None)
                exposure = conditions.exposure_by_channel.get(name) if name else None
                plane = Plane(the_c=c, the_z=z, the_t=t)
                if exposure is not None:
                    plane.exposure_time = float(exposure)
                    plane.exposure_time_unit = UnitsTime.SECOND
                planes.append(plane)
    if any(p.exposure_time is not None for p in planes):
        pixels.planes = planes

    # --- 型付きの枠に収まらないものは元のキー名のまま ----------------------
    entries = _map_entries(conditions)
    if entries:
        ome.structured_annotations = StructuredAnnotations(
            map_annotations=[
                MapAnnotation(
                    id="Annotation:0",
                    namespace=ACQUISITION_NAMESPACE,
                    value=Map(ms=[Map.M(k=k, value=v) for k, v in entries]),
                )
            ]
        )
    return ome


def _channel_name(conditions: AcquisitionConditions, index: int, channel: Any) -> Optional[str]:
    """C の位置 ``index`` のチャネル名。決められなければ None (推測しない)。"""
    if index < len(conditions.channel_names):
        return conditions.channel_names[index]
    name = getattr(channel, "name", None) if channel is not None else None
    return name


def write_acquisition_metadata(path: Path | str, conditions: AcquisitionConditions) -> None:
    """書き終わった OME-TIFF に撮像条件を足す。

    画素は読み書きしない —— 先頭ページの OME-XML だけを差し替える。
    """
    if conditions.is_empty():
        return
    target = str(path)
    existing = tifffile.tiffcomment(target)
    if existing is None:
        raise ValueError(
            "No OME-XML in %s. Conditions are attached to an OME-TIFF that a "
            "writer already produced; there is nothing to enrich here." % target
        )
    ome = from_xml(existing)
    if not ome.images:
        raise ValueError("No OME Image in %s; nothing to attach conditions to." % target)
    tifffile.tiffcomment(target, to_xml(build_enriched_ome(ome, conditions)))

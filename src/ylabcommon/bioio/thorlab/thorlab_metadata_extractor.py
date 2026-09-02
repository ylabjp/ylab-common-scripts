from dataclasses import dataclass
from typing import Any, Optional, Tuple, Dict, List
from datetime import datetime, timedelta
import warnings

from ylabcommon.bioio.core.metadata_extractor_base import MicroscopeMetadataExtractor

#: ThorImage の Date 属性の書式 (例: ``11/20/2025 14:03:22``)。
_THORIMAGE_DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S")


@dataclass
class ImagePhysicalMetadata:

    dimension_order: Optional[str]
    shape: Optional[Tuple[int, int, int, int, int]]

    size_t: Optional[int]
    size_c: Optional[int]
    size_z: Optional[int]
    size_y: Optional[int]
    size_x: Optional[int]

    pixel_size: Optional[Tuple[float, float, float]]  # Z,Y,X
    scale: Optional[Tuple]

    imaging_datetime: Optional[datetime]
    timelapse_interval: Optional[timedelta]
    objective: Optional[str]

    channel_names_index: Optional[List[str]]

    @property
    def dim_order(self) -> Any:
        """``dimension_order`` の別名 (writer が dim_order という名前で受け取るため)。"""
        return self.dimension_order

    def to_dict(self) -> dict:
        # 呼び出し側が返り値にキーを足すこと (sorter の PhysicalSizeXUnit など) が
        # あるので、インスタンスの __dict__ そのものではなく複製を返す。
        return dict(self.__dict__)


def _parse_imaging_datetime(raw: Any) -> Optional[datetime]:
    """Experiment.xml の Date 属性を datetime にする。読めなければ None。"""
    if not raw:
        return None
    for fmt in _THORIMAGE_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _clean_channel_names(names: Any, size_c: int) -> List[str]:
    """チャンネル名を OME に書ける形に整える。

    ThorImage の Wavelength 名は ``ChanA: 略称`` のように接尾辞が付くことがあるので
    先頭部分だけを使う。名前が足りない/多いときは C 軸の長さに合わせる
    (OME writer は名前の数と C が一致しないと例外を出すため)。
    """
    cleaned = [str(c).split(":")[0].strip() for c in (names or [])]
    cleaned = [c for c in cleaned if c]

    if len(cleaned) < size_c:
        cleaned += [f"Channel {i}" for i in range(len(cleaned), size_c)]
    elif len(cleaned) > size_c:
        warnings.warn(
            "[thorlab] XML declares %d channel name(s) but the stack has C=%d; "
            "using the first %d." % (len(cleaned), size_c, size_c),
            stacklevel=2,
        )
        cleaned = cleaned[:size_c]

    # 分解して "Channel" だけになったものは番号を戻す。
    return [f"Channel {i}" if c == "Channel" else c for i, c in enumerate(cleaned)]


class ThorlabMetadataExtractor(MicroscopeMetadataExtractor):
    """遅延スタックと Experiment.xml から :class:`ImagePhysicalMetadata` を作る。

    以前はここで ``BioImage`` を1つ作り、その ``dims`` / ``shape`` を読んでいた。しかし
    渡していたのは自分たちで組み立てた TCZYX の dask 配列なので、shape は元から分かって
    いて BioImage を経由する意味が無く、``standard_metadata`` から取ろうとしていた
    撮影日時・タイムラプス間隔・対物レンズは (生の配列由来なので) 常に None になっていた。

    それらは Experiment.xml に書いてあるので、XML から直接埋める。
    """

    def __init__(self, stack: Any, params: Optional[Dict] = None) -> None:
        self._stack = stack
        self._params = params or {}

    def extract(self) -> ImagePhysicalMetadata:

        shape = tuple(int(n) for n in self._stack.shape)
        if len(shape) != 5:
            raise ValueError(
                "Expected a 5D TCZYX stack, got shape %s" % (shape,)
            )
        size_t, size_c, size_z, size_y, size_x = shape

        p = self._params
        dx = p.get("PixelSizeX") or 1.0
        dy = p.get("PixelSizeY") or dx
        dz = p.get("PixelSizeZ") or 1.0
        pixel_size = (dz, dy, dx)

        interval_sec = p.get("TimeIntervalSec")
        timelapse_interval = (
            timedelta(seconds=float(interval_sec)) if interval_sec else None
        )

        channel_names_index = _clean_channel_names(p.get("ChannelNames"), size_c)

        return ImagePhysicalMetadata(
            dimension_order="TCZYX",
            shape=shape,
            size_t=size_t,
            size_c=size_c,
            size_z=size_z,
            size_y=size_y,
            size_x=size_x,
            pixel_size=pixel_size,
            # bioio の Scale と同じ TCZYX 並び。C にスケールは無いので 1.0。
            scale=(float(interval_sec) if interval_sec else None, 1.0, dz, dy, dx),
            imaging_datetime=_parse_imaging_datetime(p.get("TimeStamp")),
            timelapse_interval=timelapse_interval,
            objective=p.get("Objective"),
            channel_names_index=channel_names_index,
        )

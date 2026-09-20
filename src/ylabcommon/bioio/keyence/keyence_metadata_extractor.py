from typing import Any
from collections import defaultdict
from pathlib import Path
import re
import numpy as np
from ylabcommon.bioio.keyence.keyence_metainfo import ImageMetadata
from ylabcommon.bioio.core.metadata_extractor_base import MicroscopeMetadataExtractor

"""
"X": int(self.image_positions[0]/self.nm_per_pixel_values),
            "Y": int(self.image_positions[1]/self.nm_per_pixel_values),
            "W": self.dimensions[0],
            "H": self.dimensions[1],
            "Z_position": int(self.z_position) if self.z_position is not None else 0,
            "LensName": self.lens_name,
            "ExposureTimeInS": self.exposure_time,
            "umPerPixel": self.nm_per_pixel_values / 1000,  # Convert nm to um,
            "Sectioning": self.sectioning,
            "CameraHardwareGain": int(self.gain[1]),
            "CameraGain": int(self.gain[0]),
        }

"""

#: ``CH`` ラベルを取り出す。``keyence_bioio_stack_builder.get_channel_names`` が
#: 作る名前 (``CH1``, ``CH3`` — ゼロ埋めしない) に合わせて正規化する。
_CH_PATTERN = re.compile(r"CH(?P<ch>\d+)", re.IGNORECASE)

#: **チャネルごとに違ってよい**撮像条件。露光時間と検出器ゲインは
#: チャネルごとに変えて撮るのが普通なので、1 つに畳んではいけない。
PER_CHANNEL_FIELDS = ("ExposureTimeInS", "CameraGain", "CameraHardwareGain")


def channel_of(path: Any) -> str | None:
    """ファイル名の ``CH`` ラベル。持っていなければ None。"""
    m = _CH_PATTERN.search(Path(str(path)).name)
    return None if m is None else "CH%d" % int(m.group("ch"))


def _collapse(metas: list[dict], field: str) -> tuple[Any, set]:
    """同じチャネルの全タイルから 1 つの値を出す。割れていたら ``None``。

    返すのは ``(値, 観測された値の集合)``。**割れたときに片方を選ばない**のが
    ここの要点である。どのタイルが正しいかはファイルからは分からないので、
    選べば「測った値」と区別がつかない嘘になる。値は無し (``None``) にして、
    観測された集合を呼び出し側へ渡し、拒否できるようにする。
    """
    observed = {m[field] for m in metas if m.get(field) is not None}
    if len(observed) == 1:
        return next(iter(observed)), observed
    return None, observed


class KeyenceMetadataExtractor(MicroscopeMetadataExtractor):

    def extract(self) -> Any:
        # ファイルと meta の対応を保つ。チャネルごとの値を出すのに
        # 「どのファイルの値か」が要るので、meta だけにしてはいけない。
        pairs = [(f, ImageMetadata(f).get_dict()) for f in self.files]
        metas = [m for _, m in pairs]

        first = metas[0]

        # ----------------------------
        # Build metadata object
        # ----------------------------

        class ImageMeta:
            # 書き出し側へ渡すだけの入れ物。持たせる項目をここに並べておく
            # (書いていないと、綴りを間違えた属性が黙って増える)。
            dim_order: str
            shape: Any
            pixel_size: tuple
            lens: Any
            #: **1 ファイル目の値**。チャネルごとに露光を変えていると、
            #: ここはそのうちの 1 つしか表していない。撮像条件として使うなら
            #: ``exposure_by_channel`` を見ること (この属性は後方互換のために残す)。
            exposure: Any
            sectioning: Any
            stage: dict
            image: dict
            #: チャネル名 -> 値。チャネル間で違ってよい条件はここに入る。
            #: 同じチャネルのタイル間で値が割れていた項目は **None** になり、
            #: 何が観測されたかは ``inconsistent`` に残る。
            exposure_by_channel: dict
            gain_by_channel: dict
            #: "<CH>/<field>" -> 観測された値の集合。空なら矛盾なし。
            inconsistent: dict
            #: 以下は検証用 —— 全ファイルで揃っているべき値の集合。
            pixel_sizes: set
            dimensions: set
            lens_names: set
            z_positions: list

        # ----------------------------
        # Compute Z spacing
        # ----------------------------

        z_positions = sorted({m["Z_position"] for m in metas})

        if len(z_positions) > 1:
            diffs = np.diff(z_positions)
            z_step = np.median(diffs) / 1000  # nm → µm
        else:
            z_step = first["umPerPixel"]

        image_meta = ImageMeta()

        image_meta.dim_order = "TCZYX"
        image_meta.shape = self.data_shape

        px = first["umPerPixel"]

        image_meta.pixel_size = (z_step, px, px)
        image_meta.lens = first["LensName"]
        image_meta.exposure = first["ExposureTimeInS"]
        image_meta.sectioning = first["Sectioning"]


        image_meta.stage = {
             "X": first["X"],
             "Y": first["Y"],
             "Z_position": first["Z_position"]
        }

        image_meta.image = {
             "width": first["W"],
             "height": first["H"]
         }

        # ----------------------------
        # Per-channel acquisition conditions
        # ----------------------------
        # 露光時間と検出器ゲインは **チャネルごとに違ってよい**。1 ファイル目の値を
        # 代表にすると、残りのチャネルの条件が黙って消える。チャネルで分けて持つ。

        by_channel: dict[str, list[dict]] = defaultdict(list)
        for f, m in pairs:
            ch = channel_of(f)
            if ch is not None:
                by_channel[ch].append(m)

        exposure_by_channel: dict[str, Any] = {}
        gain_by_channel: dict[str, dict] = {}
        inconsistent: dict[str, set] = {}

        for ch in sorted(by_channel):
            ch_metas = by_channel[ch]
            values: dict[str, Any] = {}
            for field in PER_CHANNEL_FIELDS:
                value, observed = _collapse(ch_metas, field)
                values[field] = value
                if value is None and len(observed) > 1:
                    inconsistent["%s/%s" % (ch, field)] = observed
            exposure_by_channel[ch] = values["ExposureTimeInS"]
            gain_by_channel[ch] = {
                "CameraGain": values["CameraGain"],
                "CameraHardwareGain": values["CameraHardwareGain"],
            }

        image_meta.exposure_by_channel = exposure_by_channel
        image_meta.gain_by_channel = gain_by_channel
        image_meta.inconsistent = inconsistent

        # ----------------------------
        # Validation checks
        # ----------------------------

        image_meta.pixel_sizes = {m["umPerPixel"] for m in metas}
        image_meta.dimensions = {(m["W"], m["H"]) for m in metas}
        image_meta.lens_names = {m["LensName"] for m in metas}

        # ----------------------------
        # Compute Z spacing
        # ----------------------------

        image_meta.z_positions = sorted({m["Z_position"] for m in metas})

        return image_meta

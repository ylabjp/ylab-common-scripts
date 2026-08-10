# src/thorlab_loader/xml_parser.py

from pathlib import Path
from lxml import etree
from typing import Dict


class ExperimentXMLParser:
    """Experiment.xml (ThorImage) の唯一の読み取り口。

    以前は同じ XML を3箇所が別々に開いていた (本クラス / ThorlabParamsAdapter /
    get_channel_names_index)。取り出すフィールドは大半が重複しており、しかも
    アダプタ側だけが LSM の ``width`` / ``height`` という存在しない属性を見ていて
    SizeX/SizeY が常に 512 になっていた。XML の知識をここ1箇所に集約して、
    取得ごとのパースを1回に減らす。

    2つの見方を提供する:

    - :meth:`extract_metadata` — 検証用。実データと突き合わせる値をそのまま返し、
      XML に無い項目は None にする (「無い」と「既定値」を区別する)。
    - :meth:`as_params` — 取り込み用。スタック構築に必要な値を、欠けていても
      処理を進められるよう既定値で埋めて返す。
    """

    def __init__(self, xml_path: str):

        self.xml_path = Path(xml_path)

        if not self.xml_path.exists():
            raise FileNotFoundError(f"Experiment.xml missing: {xml_path}")

        self.tree = etree.parse(str(self.xml_path))
        self.root = self.tree.getroot()

    def extract_metadata(self) -> Dict:

        meta = {
            "SizeX": None,
            "SizeY": None,
            "SizeZ": None,
            "SizeT": None,
            "Channels": [],
            "PixelSizeX": None,
            "PixelSizeY": None,
            "PixelSizeZ": None,
            "TimeIntervalSec": None,
            "Objective": None,
            "FrameRate": None,
            "DwellTime": None,
            "ZStackEnabled": False,
            "TimeStamp": None,
            # 取得の種類。Z/T の読み方がこれで変わる。
            "Streaming": False,
            "ZFastEnabled": False,
        }

        # -------------------------
        # LSM block (main imaging parameters)
        # -------------------------

        lsm = self.root.find(".//LSM")

        if lsm is not None:

            meta["SizeX"] = self._safe_int(lsm.get("pixelX"))
            meta["SizeY"] = self._safe_int(lsm.get("pixelY"))

            meta["PixelSizeX"] = self._safe_float(lsm.get("pixelWidthUM"))
            meta["PixelSizeY"] = self._safe_float(lsm.get("pixelHeightUM"))

            meta["FrameRate"] = self._safe_float(lsm.get("frameRate"))
            meta["DwellTime"] = self._safe_float(lsm.get("dwellTime"))

        # -------------------------
        # Z / T の枠
        #
        # ここが最も間違えやすい。ZStage と Timelapse の値は「その画面で設定された
        # もの」であって、その取得で実際に使われたかどうかは Streaming が決める。
        # 詳しくは docs/thorlabs_experiment_xml.md。
        # -------------------------

        zstage = self.root.find(".//ZStage")
        tl = self.root.find(".//Timelapse")
        streaming = self.root.find(".//Streaming")

        z_steps = self._safe_int(zstage.get("steps")) if zstage is not None else None
        z_enable = zstage is not None and zstage.get("enable") == "1"

        meta["Streaming"] = streaming is not None and streaming.get("enable") == "1"
        meta["ZFastEnabled"] = (
            streaming is not None and streaming.get("zFastEnable") == "1"
        )

        if meta["Streaming"]:
            # 連続取得。ZStage の段数は fast-Z (zFastEnable) のときだけ使われる。
            # 使われないのに steps を信じると、時系列が丸ごと Z 軸へ潰れる。
            frames = self._safe_int(streaming.get("frames"))
            if meta["ZFastEnabled"] and z_steps:
                meta["SizeZ"] = z_steps
                meta["ZStackEnabled"] = True
                # frames は面数なので、ボリューム数は段数で割る。
                meta["SizeT"] = (frames // z_steps) if frames else None
            else:
                meta["SizeZ"] = 1
                meta["ZStackEnabled"] = False
                meta["SizeT"] = frames
        else:
            meta["SizeZ"] = z_steps
            meta["ZStackEnabled"] = z_enable
            if tl is not None:
                meta["SizeT"] = self._safe_int(tl.get("timepoints"))

        if zstage is not None:
            step = self._safe_float(zstage.get("stepSizeUM"))
            if step is not None:
                meta["PixelSizeZ"] = abs(step)

        if tl is not None:
            meta["TimeIntervalSec"] = self._safe_float(tl.get("intervalSec"))

        # -------------------------
        # Channels
        #
        # <Wavelength> は「設定された」波長を並べるだけで、その取得で有効だったかは
        # <ChannelEnable Set> のビットマスクが持つ (Set=3 なら 1番目と2番目)。
        # 片方だけ有効にした取得で全波長を数えると、実データと食い違って見える。
        # -------------------------

        names = [w.get("name") for w in self.root.findall(".//Wavelength")
                 if w.get("name")]
        enable = self.root.find(".//ChannelEnable")
        mask = self._safe_int(enable.get("Set")) if enable is not None else None

        if mask:
            meta["Channels"] = [n for i, n in enumerate(names) if mask & (1 << i)]
        else:
            meta["Channels"] = names

        # -------------------------
        # Objective
        # -------------------------

        mag = self.root.find(".//Magnification")

        if mag is not None:
            meta["Objective"] = mag.get("name")

        # -------------------------
        # Acquisition timestamp
        # -------------------------

        date_node = self.root.find(".//Date")

        if date_node is not None and date_node.get("date"):
            meta["TimeStamp"] = date_node.get("date")

        return meta

    # ------------------------------------------------------------------
    # 取り込み用の見方
    # ------------------------------------------------------------------

    def as_params(self) -> Dict:
        """スタック構築が使う params 辞書を返す。

        :meth:`extract_metadata` と違い、XML に無い値は既定値で埋める
        (呼び出し側が ``params.get("PixelSizeZ", 1.0)`` のように書いても None が
        返ってこないようにするため)。
        """
        meta = self.extract_metadata()

        size_z = meta["SizeZ"] if meta["SizeZ"] is not None else 1
        size_t = meta["SizeT"] if meta["SizeT"] is not None else 1
        z_enabled = bool(meta["ZStackEnabled"])

        pixel_x = meta["PixelSizeX"] if meta["PixelSizeX"] is not None else 1.0
        pixel_y = meta["PixelSizeY"] if meta["PixelSizeY"] is not None else pixel_x
        pixel_z = meta["PixelSizeZ"] if meta["PixelSizeZ"] is not None else 1.0

        channel_names = list(meta["Channels"]) or ["Force: ChanA"]

        raw_stamp = meta["TimeStamp"]
        timestamp = (
            raw_stamp.replace("/", "").replace(" ", "_").replace(":", "")
            if raw_stamp else "0000"
        )

        return {
            # Z スタックとして撮ったのか、タイムラプスとして撮ったのか。
            # 面の配置はファイル名の連番が決めるので (thorlab_bioio_stack_builder の
            # _fill_frame)、これは表示と互換のために残しているだけ。
            "mode": "Z" if (z_enabled and size_z > 1) else "T",
            "SizeX": meta["SizeX"] if meta["SizeX"] is not None else 512,
            "SizeY": meta["SizeY"] if meta["SizeY"] is not None else 512,
            "SizeZ": size_z,
            "SizeT": size_t,
            "PixelSizeX": pixel_x,
            "PixelSizeY": pixel_y,
            "PixelSizeZ": pixel_z,
            "ChannelNames": channel_names,
            "TimeIntervalSec": meta["TimeIntervalSec"],
            "Objective": meta["Objective"],
            "TimeStamp": raw_stamp,
            # 綴りが不揃いだが、既存の呼び出し側との互換のため残す。
            "TimesTamp": timestamp,
            "ZStackEnabled": z_enabled,
            "Streaming": bool(meta["Streaming"]),
            "ZFastEnabled": bool(meta["ZFastEnabled"]),
        }

    def _safe_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

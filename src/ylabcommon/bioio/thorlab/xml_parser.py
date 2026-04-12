# src/thorlab_loader/xml_parser.py

from pathlib import Path
from lxml import etree
from typing import Dict


class ExperimentXMLParser:

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
        # Z Stage
        # -------------------------

        zstage = self.root.find(".//ZStage")

        if zstage is not None:

            meta["SizeZ"] = self._safe_int(zstage.get("steps"))

            step = self._safe_float(zstage.get("stepSizeUM"))
            if step is not None:
                meta["PixelSizeZ"] = abs(step)

        # -------------------------
        # Timelapse
        # -------------------------

        tl = self.root.find(".//Timelapse")

        if tl is not None:

            meta["SizeT"] = self._safe_int(tl.get("timepoints"))
            meta["TimeIntervalSec"] = self._safe_float(tl.get("intervalSec"))

        # -------------------------
        # Channels
        # -------------------------

        for w in self.root.findall(".//Wavelength"):

            name = w.get("name")

            if name:
                meta["Channels"].append(name)

        # -------------------------
        # Objective
        # -------------------------

        mag = self.root.find(".//Magnification")

        if mag is not None:
            meta["Objective"] = mag.get("name")

        return meta

    def _safe_int(self, value):
        try:
            return int(value)
        except:
            return None

    def _safe_float(self, value):
        try:
            return float(value)
        except:
            return None

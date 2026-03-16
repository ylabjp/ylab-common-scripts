import xml.etree.ElementTree as ET
from ylabcommon.bioio.core.base_params_adapter import BaseParamsAdapter

class ThorlabParamsAdapter(BaseParamsAdapter):
    def __init__(self, xml_path: str):
        self.xml_path = xml_path 

    def extract(self):
        tree = ET.parse(self.xml_path)
        root = tree.getroot()
   
        # Thorlabs specific paths
        z_stage = root.find(".//ZStage")
        lsm = root.find(".//LSM")
        timelapse = root.find(".//Timelapse")
        wavelengths = root.find(".//Wavelengths")
        date_node = root.find(".//Date")

        # Get the target counts
        # Thorlabs calls Z-slices 'steps'
        size_z = int(z_stage.get("steps", 1)) if z_stage is not None else 1

        # Thorlabs calls Time-slices 'timepoints'
        size_t = int(timelapse.get("timepoints", 1)) if timelapse is not None else 1
   
        # Determine Mode
        z_enabled = z_stage.get("enable") == "1" if z_stage is not None else False
        mode = "Z" if (z_enabled and size_z > 1) else "T"
   
        # Get Physical Calibrations: pixelSizeUM is usually in the LSM tag
        pixel_x = float(lsm.get("pixelSizeUM", 1.0)) if lsm is not None else 1.0

        # stepSizeUM is in the ZStage tag
        pixel_z = abs(float(z_stage.get("stepSizeUM", 1.0))) if z_stage is not None else 1.0

        channel_names = [w.get("name") for w in wavelengths.findall("Wavelength")] if wavelengths is not None else ["Force: ChanA"],
        timestamp = date_node.get("date").replace('/', '').replace(' ', '_').replace(':', '') if date_node is not None else "0000"

        return {
            "mode": mode,
            "SizeX": int(lsm.get("width", 512)),
            "SizeY": int(lsm.get("height", 512)),
            "SizeZ": size_z,
            "SizeT": size_t,
            "PixelSizeX": pixel_x,
            "PixelSizeZ": pixel_z,
            "ChannelNames": channel_names,
            "TimesTamp": timestamp,
            "ZStackEnabled": z_enabled
        }


import xml.etree.ElementTree as ET
import re
# obtain XML data from a TIFF file
# grep -a -e"Data" -B0 -A1000 Test_001.tif


class ImageMetadata:
    """
    A class to extract XML data from a TIFF file and parse coordinates (X and Y) and dimensions (width & height).
    Attributes:
        image_positions (tuple): A tuple containing the X and Y coordinates.
        dimensions (tuple): A tuple containing the width and height of the image.
        nm_per_pixel_values (float): The conversion factor from nanometers to pixels.
    Methods:
        __init__(tif):
            Initializes the ImageMetadata object by extracting and parsing XML data from the given TIFF file.
    """

    def __init__(self, tif: str, save_xml: bool = False):
        self.__xml_file = tif.replace('.tif', '.xml')
        self.image_positions: tuple[float] = None
        self.dimensions: tuple[int] = None
        self.z_position: int = None
        self.nm_per_pixel_values: float = None
        self.lens_name = None
        self.exposure_time = None
        self.sectioning = None
        self.gain: tuple[int] = None
        # Read TIFF file as binary
        with open(tif, "rb") as file:
            content = file.read().decode(errors="ignore")   # decode as string
        # print(content)
        # Extract XML content from TIFF file (file starts with <Data> and ends with <\Data>)
        match = re.search(r"<Data>.*?</Data>", content, re.DOTALL)

        if not match:
            raise ValueError(f"Could not extract XML data from {tif}")
        xml_content = match.group(0)  # extract matched xml content

        if save_xml:
            with open(self.__xml_file, "w", encoding="utf-8") as xml_out:
                xml_out.write(xml_content)

        # Parse the XML content directly and extract coordinates and dimensions
        tree = ET.ElementTree(ET.fromstring(xml_content))
        region = tree.find('.//XyStageRegion')

        if region is None:
            raise ValueError(f"File: {self.__xml_file} | Attributes not found")

        # Extract X and Y coordinates
        self.image_positions = (
            int(region.find('X').text), int(region.find('Y').text))

        # Extract width and height from the SavingImageSize section
        self.dimensions = (
            int(tree.find('.//SavingImageSize/Width').text),
            int(tree.find('.//SavingImageSize/Height').text)
        )

        # Extract width from XyStageRegion and SavingImageSize to calculate nm_per_pixel
        # Width in nm from XyStageRegion
        # Width in pixels from SavingImageSize
        self.nm_per_pixel_values = int(region.find(
            'Width').text) / self.dimensions[0]  # Conversion factor

        # Extract Z position if available
        self.z_position = tree.find(
            './/StageLocationZ').text if tree.find('.//StageLocationZ') is not None else None

        self.gain = (
            tree.find(
                './/CameraGain').text if tree.find('.//CameraGain') is not None else 0,
            tree.find(
                './/CameraHardwareGain').text if tree.find('.//CameraHardwareGain') is not None else 0
        )
        # Extract LensName
        # <Lens Type="Keyence.Micro.Bio.Common.Data.Metadata.Conditions.LensCondition, Keyence.Micro.Bio.Common.Data.Metadata, Version=1.1.2.14, Culture=neutral, PublicKeyToken=null">
        # <LensName Type="System.String">PlanApo 4x 0.20/20.00mm :Default</LensName>
        lens_name = tree.find('.//LensName')
        if lens_name is not None:
            self.lens_name = lens_name.text
        else:
            self.lens_name = "LensName not found"

        # Extract ExposureTime from the XML file
        # <ExposureTime Type="Keyence.Micro.Bio.Common.Utility.KeyValueContainer.ExposureTimeKeyValueContainer, Keyence.Micro.Bio.Common.Utility.KeyValueContainer, Version=1.1.2.14, Culture=neutral, PublicKeyToken=null">
        # <Numerator Type="System.Int32">1</Numerator>
        # <Denominator Type="System.Int32">30</Denominator>
        # <Line Type="System.Int32">761</Line>
         # </ExposureTime>
        exposure_time = tree.find('.//ExposureTime')
        if exposure_time is not None:
            numerator = exposure_time.find('Numerator')
            denominator = exposure_time.find('Denominator')
            if numerator is not None and denominator is not None:
                self.exposure_time = float(
                    numerator.text) / float(denominator.text)
        # Extract Sectioning information
        #     <Sectioning Type="Keyence.Micro.Bio.Common.Data.Metadata.Conditions.SectioningCondition, Keyence.Micro.Bio.Common.Data.Metadata, Version=1.1.2.14, Culture=neutral, PublicKeyToken=null">
        #   <Enabled Type="System.Boolean">True</Enabled>
        #   <SettingType Type="Keyence.Micro.Bio.Common.Data.Types.SectioningSettingType">Custom</SettingType>
        #   <SlitType Type="Keyence.Micro.Bio.Common.Data.Types.SectioningSlitType">Slit</SlitType>
        #   <SlitSize Type="System.Int32">1</SlitSize>
        #   <SlitPitch Type="System.Int32">10</SlitPitch>
        #   <SlitScanPitch Type="System.Int32">1</SlitScanPitch>
        # < / Sectioning >
        sectioning_info = tree.find('.//Sectioning')
        if sectioning_info is not None:
            if sectioning_info.find('Enabled').text == "True":
                self.sectioning = (
                    "SettingType: " + sectioning_info.find('SettingType').text +
                    "SlitType: "+sectioning_info.find('SlitType').text +
                    "SlitSize: " + sectioning_info.find('SlitSize').text +
                    "SlitPitch: " + sectioning_info.find('SlitPitch').text +
                    "SlitScanPitch: " +
                    sectioning_info.find('SlitScanPitch').text
                )

            else:
                self.sectioning = "None"

    def __str__(self):
        return f"X: {self.image_positions[0]}, Y: {self.image_positions[1]}, Width: {self.dimensions[0]}, Height: {self.dimensions[1]}, nm_per_pixel: {self.nm_per_pixel_values}, lens_name: {self.lens_name}, Exposure_Time: {self.exposure_time}"

    def get_dict(self):
        return {
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

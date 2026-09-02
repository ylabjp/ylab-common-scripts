from ylabcommon.bioio.core.base_params_adapter import BaseParamsAdapter
from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser


class ThorlabParamsAdapter(BaseParamsAdapter):
    """Experiment.xml をスタック構築用の params 辞書にするアダプタ。

    XML の読み取りそのものは :class:`ExperimentXMLParser` に委ねる。以前はこのクラスが
    独自に XML を開いており、その過程で LSM の ``width`` / ``height`` という存在しない
    属性を見ていたため SizeX/SizeY が常に既定値の 512 になっていた
    (ThorImage が持つのは ``pixelX`` / ``pixelY``)。
    """

    def __init__(self, xml_path: str) -> None:
        self.xml_path = xml_path

    def extract(self) -> dict:
        return ExperimentXMLParser(self.xml_path).as_params()

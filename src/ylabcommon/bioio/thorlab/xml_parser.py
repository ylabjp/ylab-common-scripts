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

            # 走査中心のずらし量。0 が既定で、0 でなければ視野が中心からずれている。
            # 値そのものを持つだけで、警告は :func:`warn_if_scan_offset` が出す
            # (extract_metadata は取得ごとに何度も呼ばれるので、ここで出すと重複する)。
            #
            # **書かれていなければ None のままにする。** 以前は ``or 0`` で 0 に
            # 倒していたので、「0 と記録された取得」と「そもそも記録の無い取得」が
            # 区別できなかった。オフセットが 0 であることは合否の判定に使われる
            # (slice-analysis の QC はこれだけで通す) ため、確かめられなかった
            # ものが黙って合格する。読めなかったことは値ではなく None で表す。
            # 数値が要るだけの呼び出し側は :func:`scan_offset` を使えばよい。
            meta["ScanOffsetX"] = self._safe_int(lsm.get("offsetX"))
            meta["ScanOffsetY"] = self._safe_int(lsm.get("offsetY"))

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

        # 時間軸は XML からは決まらない (docs/thorlabs_experiment_xml.md)。
        # Timelapse/@intervalSec=60 だと 3000 時点で 50 時間、LSM/@frameRate=45.638
        # だと 66 秒。3 桁違ううえ、triggerMode=1 (外部トリガ) なら実時刻は外部装置が
        # 決めるので XML のどこにも書かれていない。
        # ここに入るのは「正しい時間軸」ではなく後方互換のための値であって、
        # 正確な時間軸はトリガー記録から別途再構成する。この値を根拠にした解析を
        # 書かないこと。
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


def scan_offset(meta: dict) -> tuple:
    """``extract_metadata()`` の結果から走査オフセット ``(x, y)`` を取り出す。

    **記録が無いときも ``(0, 0)`` を返す。** 数値がほしいだけの呼び出し側を
    ``None`` で煩わせないためだが、そのぶん「0 と記録された」と「記録が無い」が
    ここでは区別できない。**区別が要るなら :func:`scan_offset_known` を見ること。**
    合否の判定に使うなら、まずそちらを見てからにする。
    """
    return int(meta.get("ScanOffsetX") or 0), int(meta.get("ScanOffsetY") or 0)


def scan_offset_known(meta: dict) -> bool:
    """走査オフセットが実際に記録されていたかどうか。

    ``<LSM>` が無い / ``offsetX`` ``offsetY`` が書かれていない / 数として読めない、
    のいずれでも False。**「オフセットは 0 だった」と「オフセットは分からない」を
    混ぜないための関数。** 0 であることを取り込みの合否に使う道具 (slice-analysis の
    QC) では、確かめられなかったものが黙って合格するのがいちばん困る。
    """
    return (meta.get("ScanOffsetX") is not None
            and meta.get("ScanOffsetY") is not None)


def warn_if_scan_offset(meta: dict, xml_path) -> bool:
    """走査中心がずれていたら警告する。**ずれていると分かったとき** True。

    ``<LSM offsetX="-52" offsetY="36" .../>`` は走査の中心を視野の中心から
    ずらす設定で、既定は 0 である。0 でない取得は視野がステージ座標の中心と
    一致しないので、他の取得と並べたときに位置が合わない。取り込み自体は
    そのまま通す (画素も画素サイズも変わらない) が、黙って通すと、あとで
    「位置が合わない」とだけ言われて原因を追えなくなる。

    記録が無い取得 (古い ThorImage など) でも黙って通さず、別の文面で知らせる。
    ただし戻り値は False —— **ずれていると分かったわけではない** ので、
    戻り値で分岐している呼び出し側に「ずれている」と言ってはいけない。
    分からなかったことまで見たい呼び出し側は :func:`scan_offset_known` を使う。

    警告は端末では色付き、Better Stack へは値付きの構造化ログとして残す
    (どの取得が・いくつずれていたかを後から集計できるように)。
    """
    from ylabcommon.utils.betterstack_log import log_warning

    raw_x, raw_y = meta.get("ScanOffsetX"), meta.get("ScanOffsetY")

    # **ずれていると分かるほうを先に見る。** 片方だけ記録されていて、その片方が
    # 0 でないなら、視野がずれていることは確定している。「記録が無い」を先に
    # 見ると、その確定した事実が「確認できません」に隠れてしまう。
    if raw_x or raw_y:
        log_warning(
            "[thorlab] Scan offset is not centred: offsetX=%s, offsetY=%s "
            "(expected 0, 0). The field of view is shifted from the centre of the "
            "scan area, so stage coordinates will not line up with acquisitions "
            "taken at offset 0. Pixels and pixel size are unaffected. Source: %s"
            % (_shown(raw_x), _shown(raw_y), xml_path),
            stage="thorlab.xml",
            target_file=str(xml_path),
            scan_offset_x=raw_x,
            scan_offset_y=raw_y,
        )
        return True

    if not scan_offset_known(meta):
        log_warning(
            "[thorlab] Scan offset is not recorded: this Experiment.xml has no "
            "readable offsetX/offsetY, so whether the field of view is centred "
            "cannot be checked. Do not read this as 'offset 0'. Source: %s"
            % xml_path,
            stage="thorlab.xml",
            target_file=str(xml_path),
            scan_offset_x=raw_x,
            scan_offset_y=raw_y,
        )
        return False

    return False


def _shown(value):
    """記録の無い値を 0 と書かない。文面の中で「0 だった」と読ませないため。"""
    return "not recorded" if value is None else value

from pathlib import Path

import json
import warnings
from datetime import datetime, UTC, timezone

from ylabcommon.utils.file_selection import collect_valid_tiffs
from ylabcommon.utils.outfile_name import extract_dimensions
from ylabcommon.utils.summary_metadata_helper import get_enhanced_metadata, generate_file_sha256
from ylabcommon.utils.utils import hybrid, style_print
from ylabcommon.bioio.core.bioio_reader import BioIOReader
from ylabcommon.bioio.core.bioio_writer import BioIOWriter
from ylabcommon.bioio.thorlab.thorlab_metadata_extractor import ThorlabMetadataExtractor
from ylabcommon.bioio.thorlab.thorlab_params_adapter import ThorlabParamsAdapter
from ylabcommon.bioio.thorlab.thorlab_bioio_stack_builder import stack_thorlab_with_bioio_calibrated, get_channel_names_index

from ylabcommon.bioio.thorlab.xml_parser import ExperimentXMLParser

## Main script for loading the files


def _check_size_t_tolerant(xml_size_t, image_size_t):
    """XML が宣言する SizeT と実データの T(時点数)を比較する。

    タイムラプス取得は途中で停止されることがあり、その場合 XML の指定より少ない時点数
    しか保存されない。この「短い T」は構造の破綻ではなく取得の打ち切りなので許容する
    (Warning を出して OK 扱い)。一方、実データの T が XML より多い場合は XML と構造が
    一致しない異常なので NG とする。

    Args:
        xml_size_t: XML(Experiment.xml)が示す時点数。None なら検証しない。
        image_size_t: 実際に読み込めた時点数。

    Returns:
        tuple[bool, str | None, str]:
            (ok, warning_message, detail)
            ok: 検証を通す(True)か否か。短いTなら True。
            warning_message: 短いTのとき出す警告文(なければ None)。
            detail: レポート用の内訳文字列。
    """
    if xml_size_t is None or image_size_t is None:
        return True, None, f"xml={xml_size_t} img={image_size_t} (skipped)"
    if image_size_t == xml_size_t:
        return True, None, f"xml={xml_size_t} img={image_size_t}"
    if image_size_t < xml_size_t:
        msg = (
            f"[thorlab] 取得された時点数 T={image_size_t} が XML 指定の SizeT={xml_size_t} より"
            f"少ないです。タイムラプス取得が途中で終了した可能性があります。"
            f"短い T を許容し、実データの T={image_size_t} で続行します。"
        )
        return True, msg, f"xml={xml_size_t} img={image_size_t} (short T tolerated)"
    # 実データの方が多い = XML と構造が一致しない
    return False, None, f"xml={xml_size_t} img={image_size_t} (more timepoints than XML)"


class ThorlabBioioBuilder:
    """
    Full reconstruction pipeline:

    TIFF discovery → Ultra stacking → BioIOReader →
    Metadata extraction → XML validation → Output naming → Write OME
    """

    def __init__(
        self,
        tiff_dir: Path,
        *,
        compression: str = "zlib",
        compression_level: int = 6,
        validate_metadata: bool = True,
        dry_run: str = False,
    ):

        self.tiff_dir = Path(tiff_dir)
        self.xml_file = self.tiff_dir/"Experiment.xml"
        self.dry_run = dry_run

        self.compression = compression
        self.compression_level = compression_level
        self.validate_metadata = validate_metadata

        self.stacked_data=None
        self.image_meta=None

    # -------------------------------------------------
    # TIFF DISCOVERY + STACK
    # -------------------------------------------------

    def _get_params(self):
        params_adapter = ThorlabParamsAdapter(self.xml_file)
        get_thorlabs_params = params_adapter.extract()
        return get_thorlabs_params

    def _discover_and_stack(self):

        print("[Builder] Discovering valid TIFF files...")

        tiff_files = collect_valid_tiffs(self.tiff_dir)

        if not tiff_files:
            raise RuntimeError("No valid TIFF files found.")

        print(f"[Builder] Found {len(tiff_files)} usable TIFF files")
        print("[Builder] Ultra stacking images...")
        
        get_thorlabs_params = self._get_params()
        stacked_data, tiff_files = stack_thorlab_with_bioio_calibrated(tiff_files, self.xml_file, get_thorlabs_params)

        total_depth_um = stacked_data.Z.max().values
        print(f"Total volume depth: {total_depth_um} microns")

        data_to_process = stacked_data.data
        return data_to_process, tiff_files

    # -------------------------------------------------
    # BioIO Processing Reader
    # -------------------------------------------------

    def _load_with_bioio(self, stacked_data):

        print("[Builder] Loading stacked data via BioIOReader...")

        reader = BioIOReader(stacked_data)

        data = reader.read()
        params = self._get_params()
        #params = get_thorlabs_params(self.xml_file)
        dx = params.get("PixelSizeX", 1.0)
        dy = params.get("PixelSizeY", dx)
        dz = params.get("PixelSizeZ", 1.0)
        channel_names_str = params.get("ChannelNames")
        current_pixel_size = (dz, dy, dx) # The (Z, Y, X) tuple
        channel_names_index = get_channel_names_index(self.xml_file)

        extractor = ThorlabMetadataExtractor(
            reader, 
            pixel_size_tuple = current_pixel_size,
            channel_names_index = channel_names_index
        )
        hybrid_channel_name = hybrid(channel_names_index, channel_names_str)
        image_meta = extractor.extract()
        image_meta.dim_order = "TCZYX"

        print(f"Data shape: {data.shape}")
        print(f"Dimension order from reader: {reader.get_dim_order()}")
        print(f"Shape from image meta: {image_meta.shape}")

        return data, image_meta, hybrid_channel_name 

    # -------------------------------------------------
    # XML Validation
    # -------------------------------------------------

    def _validate(self, xml_meta, image_meta):

        print("[Builder] Validating XML <-> BioIO metadata...")

        report = {"status": "PASS", "checks": []}

        def record(name, ok, detail):
            report["checks"].append(
                {"check": name, "ok": ok, "detail": detail}
            )
            if not ok:
                report["status"] = "NOT VALIDATED"

        if xml_meta:

            # spatial size validation
            record("SizeX",
                   xml_meta["SizeX"] == image_meta.shape[-1],
                   f"xml={xml_meta['SizeX']} image={image_meta.shape[-1]}")

            record("SizeY",
                   xml_meta["SizeY"] == image_meta.shape[-2],
                   f"xml={xml_meta['SizeY']} image={image_meta.shape[-2]}")

            # Z depth
            record("SizeZ",
                   xml_meta["SizeZ"] == image_meta.shape[2],
                   f"xml={xml_meta['SizeZ']} image={image_meta.shape[2]}")
        #Pixel calibratio
        if xml_meta["PixelSizeX"] and image_meta.pixel_size:

            diff = abs(xml_meta["PixelSizeX"] - image_meta.pixel_size[2])

            record("PixelSizeX",
                   diff < 1e-3,
                   f"Δ={diff}")
        #Channel count
        record("Channels",
               len(xml_meta["Channels"]) == image_meta.shape[1],
               "channel count")

        print(f"[Builder] Validation status: {report['status']}")
        return report

    def _validate_thorlab_stack(self, xml_meta, image_meta):
        """
        image_meta: The BioImage object (or a custom wrapper) of the stacked data
        xml_meta: Dictionary parsed from Experiment.xml
        """
        report = {"status": "VALIDATED", "checks": []}

        def record(name, ok, msg):
            report["checks"].append({"name": name, "ok": ok, "msg": msg})
            if not ok:
                report["status"] = "NOT VALIDATED"

        if xml_meta:
            #spatial size validation - use size_x, size_y, size_z
            record("SizeX",
               xml_meta["SizeX"] == image_meta.size_x,
               f"xml={xml_meta['SizeX']} image={image_meta.size_x}")

            record("SizeY",
               xml_meta["SizeY"] == image_meta.size_y,
               f"xml={xml_meta['SizeY']} image={image_meta.size_y}")

            #Z depth
            record("SizeZ",
               xml_meta["SizeZ"] == image_meta.size_z,
               f"xml={xml_meta['SizeZ']} image={image_meta.size_z}")

            # Pixel calibration (存在する場合のみ)
            if xml_meta.get("pixel_size") and image_meta.pixel_size:
                # index 2 is X in your (Z, Y, X) tuple
                diff_x = abs(xml_meta["PixelSizeX"] - image_meta.pixel_size[2])
                record("PixelSizeX", diff_x < 1e-4, f"Δ={diff_x:.6f}")

                #Volume/Time Depth Validation
                if xml_meta.get("ZStackEnabled") and xml_meta.get("PixelSizeZ"):
                    diff_z = abs(xml_meta["PixelSizeZ"] - image_meta.pixel_size[0])
                    record("PixelSizeZ", diff_z < 1e-4, f"Δ={diff_z:.6f}")

            # SizeT: タイムラプス取得が途中終了して XML 指定より少ない T は許容(Warning + OK)し、
            # 多い場合のみ構造不一致として NG にする(詳細は _check_size_t_tolerant を参照)。
            ok_t, warn_t, detail_t = _check_size_t_tolerant(
                xml_meta.get("SizeT"), image_meta.size_t
            )
            if warn_t:
                warnings.warn(warn_t, stacklevel=2)
            record("SizeT", ok_t, detail_t)

            #Channel Count
            xml_chan_count = len(xml_meta.get("Channels", []))
            record("Channels", 
                xml_chan_count == image_meta.size_c, 
                f"xml={xml_chan_count} img={image_meta.size_c}"), 

        style_print("\n========== Validation Results ================", "header")

        for check in report["checks"]:
            status = "PASS" if check["ok"] else "Some Parameters Not validated"
            print(f"[{status}] {check['name']}: {check['msg']}")
        print(f"Final Status: {report['status']}")
        print("=============================================\n")
        return report

    # -------------------------------------------------
    # WRITE OUTPUT
    # ---------------------------------------   ----------
    
    def write(self,output_path:Path):

        print("[Builder] Writing OME output...")

        writer = BioIOWriter(
            output_path,
            compression=self.compression,
            compression_level=self.compression_level,
        )

        writer.write(
            self.stacked_data,
            dim_order=self.image_meta.dim_order,
            channel_names=None,
            #physical_pixel_sizes=phys_sizes,
            physical_pixel_sizes=self.image_meta.pixel_size,
        )
    # -------------------------------------------------
    # Validation report
    # -------------------------------------------------

    def _write_report(self, report, image_meta, output_path, hybrid_channel_name, tiff_files):

        report_path = output_path.with_suffix(".validation.json")
        extra_meta_summary = get_enhanced_metadata(image_meta, tiff_files)

        payload = {
            #"timestamp": datetime.datetime.now(datetime.UTC),
            #"timestamp": datetime.utcnow().isoformat(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra_meta_summary,
            "source_tiff_dir": str(self.tiff_dir),
            "experiment_xml": str(self.xml_file) if self.xml_file else None,
            "Channel_name_hybrid_index_str": hybrid_channel_name, 
            "image_metadata": image_meta.to_dict(),
            "validation": report,
            "software": "ylabcommon + thorlab_loader + bioio backend",
        }

        #payload.update(extra_meta_summary)

        for i in (tiff_files): 
            if i == 0:  # Only do this for the first file to save time
                first_file_hash = generate_file_sha256(output_path)
                payload["integrity_check"] = {"first_file_sha256": first_file_hash}

        with open(report_path, "w") as f:
            json.dump(payload, f, indent=2)

        print(f"[Builder] Validation report → {report_path}")

    # -------------------------------------------------
    # MAIN PIPELINE
    # -------------------------------------------------

    def build(self):
        print("=============================================================================")
        print("[Builder] Starting BioIO reconstruction pipeline")

        stacked_data, tiff_files = self._discover_and_stack()

        data, image_meta, hybrid_channel_name  = self._load_with_bioio(stacked_data)

        xml_meta = None

        if self.validate_metadata and self.xml_file:
            xml = ExperimentXMLParser(self.xml_file)
            xml_meta = xml.extract_metadata()

        report = self._validate_thorlab_stack(xml_meta, image_meta)
        
        image_name, dims = extract_dimensions(tiff_files)
        
        if self.dry_run:
            style_print("[DRY RUN ENABLED]", "info")
            print("[Validating] TIFF discovery successful")
            print("[Validating] BioIO stacking successful")
            print("[Validating]  Metadata extraction successful")
            print(f"[Validating] Validation status: {report['status']}")
            print("[Skipping] file writing")
            print("[Skipping] summary JSON writing")
            print("\n    EXECUTION SUMMARY    \n")
            print(f"Input TIFF count : {len(tiff_files)}")
            print(f"Stack shape      : {data.shape}")
            print(f"Pixel size (µm)  : {image_meta.pixel_size}")

            print("\nDry run completed successfully.\n")
            return

        self.stacked_data=stacked_data
        self.image_meta=image_meta

        #===============================================================
        #Write summary report 
        #===============================================================

        # summary_report = ReportBuilder()

        # # dataset information
        # summary_report.collect_dataset(
        #     str(self.tiff_dir),
        #     "Thorlab",
        #     len(tiff_files)
        # )

        # # experiment XML
        # summary_report.add_section(
        #     "experiment_files",
        #     {
        #         "experiment_xml": str(self.xml_file)
        #      }
        # )

        # # hybrid channel names
        # summary_report.add_section(
        #     "thorlab_channels",
        #     {
        #          "Channel_name_hybrid_index_str": hybrid_channel_name
        #     }
        # )

        # # metadata from stacked TIFF
        # summary_report.add_section(
        #     "image_metadata",
        #     image_meta
        # )

        # # dimensions detected from filenames
        # summary_report.set_dimensions(dims)

        # # stack metadata (shape, dtype, pixel sizes etc.)
        # summary_report.collect_metadata(image_meta, stacked_data)

        # # output information
        # summary_report.set_output(self.output_dir, output_filename)

        # # validation
        # summary_report.finalize_validation()

        # # write report
        # summary_report.write(self.output_dir, output_filename)

        print("[Builder] DONE.")
